// Voice: microphone -> /api/stt (local Whisper), replies -> /api/tts (local Piper), wake word via /api/wake.
// Loaded after app.js and shares its globals (state, setState, send, controller, $).

const prefs = {
  get(key, fallback) { try { return JSON.parse(localStorage.getItem('jarvis.' + key)) ?? fallback; } catch { return fallback; } },
  set(key, value) { try { localStorage.setItem('jarvis.' + key, JSON.stringify(value)); } catch { /* private window */ } },
};
const micSupported = !!(navigator.mediaDevices?.getUserMedia && window.MediaRecorder && window.AudioWorkletNode);
const sleep = ms => new Promise(r => setTimeout(r, ms));
const micConstraints = () => {
  const deviceId = prefs.get('mic', '');
  return { audio: { deviceId: deviceId ? { ideal: deviceId } : undefined, echoCancellation: true, noiseSuppression: true, channelCount: 1 } };
};
let installedVoices = [];
let audioCtx = null;
let micLevel = 0;      // live microphone level while recording
let recording = null;  // { stop, cancel } while JARVIS is actively listening to a request

const audioContext = () => {
  audioCtx ??= new AudioContext();
  if (audioCtx.state === 'suspended') audioCtx.resume();
  return audioCtx;
};
function meter(node) { // returns a function giving the node's current loudness (RMS)
  const analyser = audioContext().createAnalyser();
  analyser.fftSize = 1024;
  node.connect(analyser);
  const data = new Float32Array(analyser.fftSize);
  return () => {
    analyser.getFloatTimeDomainData(data);
    let sum = 0;
    for (const v of data) sum += v * v;
    return Math.sqrt(sum / data.length);
  };
}

// ---- Speaking -------------------------------------------------------------------------------
const speaker = {
  audio: new Audio(), level: null, session: 0,
  queue: [], buffer: '', spokenChars: 0, playing: false, active: false, done: false, capped: false, inCode: false, after: null,

  feed(delta) { // called with reply text as it streams; speaks each sentence as soon as it is complete
    this.buffer += delta;
    let m;
    while ((m = this.buffer.match(/^[\s\S]*?(?:[.!?:](?=\s)|\n)/))) {
      this.buffer = this.buffer.slice(m[0].length);
      this.enqueue(m[0]);
    }
  },
  finish(after) { // no more text is coming; `after` runs once everything has been spoken
    this.enqueue(this.buffer);
    this.buffer = '';
    this.done = true;
    this.after = after;
    if (!this.playing && !this.queue.length) this.idle();
  },
  enqueue(raw) {
    if (raw.includes('```')) { this.inCode = !this.inCode; return; } // never read code aloud
    if (this.inCode || this.capped) return;
    let text = raw.replace(/\[([^\]]*)\]\([^)]*\)/g, '$1').replace(/\[\d+\]/g, '').replace(/https?:\/\/\S+/g, '')
      .replace(/[*_#`>|•]/g, '').replace(/\s+([.,!?])/g, '$1').trim(); // no link syntax, citations or addresses aloud
    if (!/[a-z0-9]/i.test(text)) return;
    if (this.spokenChars + text.length > 600) { this.capped = true; text = 'The rest is on screen.'; } // don't read long replies aloud
    this.spokenChars += text.length;
    this.queue.push(fetch('/api/tts', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text: text.slice(0, 1000), voice: currentVoice(), speed: prefs.get('speed', 1) }),
    }).then(r => r.ok ? r.blob() : null).catch(() => null));
    this.active = true;
    if (!this.playing) this.next();
  },
  async next() {
    const session = this.session, pending = this.queue.shift();
    if (!pending) { this.playing = false; if (this.done) this.idle(); return; }
    this.playing = true;
    const blob = await pending;
    if (session !== this.session) return; // interrupted while synthesizing
    if (!blob) return this.next();
    this.level ??= (() => { // route playback through an analyser so the visualizer follows the real voice
      const source = audioContext().createMediaElementSource(this.audio);
      source.connect(audioContext().destination);
      return meter(source);
    })();
    audioContext();
    URL.revokeObjectURL(this.audio.src);
    this.audio.src = URL.createObjectURL(blob);
    this.audio.volume = prefs.get('volume', 1);
    this.audio.onended = () => this.next();
    if (!controller) setState('speaking');
    this.audio.play().catch(() => this.next());
  },
  reset() {
    this.session++;
    this.audio.pause();
    Object.assign(this, { queue: [], buffer: '', spokenChars: 0, playing: false, active: false, done: false, capped: false, inCode: false, after: null });
  },
  stop() { // interrupt: used by the Stop button, the mic button, the wake word, and any new request
    const was = this.active;
    this.reset();
    if (was && state === 'speaking') setState('idle');
  },
  idle() {
    const after = this.after;
    this.reset();
    if (state === 'speaking') setState('idle');
    after?.();
  },
  say(text) { return new Promise(resolve => { this.stop(); this.enqueue(text); this.finish(resolve); }); },
};

const currentVoice = () => {
  const saved = prefs.get('voice', '');
  return installedVoices.includes(saved) ? saved
    : installedVoices.find(v => v.includes('alan')) || installedVoices[0] || '';
};
// Should this reply be read aloud? (called by send in app.js)
function shouldSpeak(viaVoice) {
  const mode = prefs.get('speak', 'voice');
  return !!currentVoice() && (mode === 'always' || (mode === 'voice' && viaVoice));
}
// Live level 0..1 for the visualizer (called by draw in app.js)
function audioLevel() {
  if (recording) return micLevel;
  return speaker.playing && speaker.level ? Math.min(1, speaker.level() * 6) : 0;
}

async function transcribe(blob) {
  const r = await fetch('/api/stt', { method: 'POST', body: blob });
  if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || 'Transcription failed');
  return (await r.json()).text;
}
function micError(e) {
  const denied = e.name === 'NotAllowedError', missing = e.name === 'NotFoundError';
  const text = denied ? 'Microphone blocked' : missing ? 'No microphone found' : e.message || 'Microphone error';
  setState('error', text);
  log(text, 'error');
}

// ---- Click-to-talk (used when the wake word is off) ---------------------------------------------
// Opens the microphone and resolves with a recording once the speaker pauses, or null if nothing was said.
async function record(startTimeout) {
  const stream = await navigator.mediaDevices.getUserMedia(micConstraints());
  const loudness = meter(audioContext().createMediaStreamSource(stream));
  const recorder = new MediaRecorder(stream), chunks = [];
  recorder.ondataavailable = e => chunks.push(e.data);
  const threshold = prefs.get('sensitivity', 0.02);
  return new Promise(resolve => {
    const started = performance.now();
    let spoke = false, keep = false, lastVoice = started;
    const end = keepIt => { keep = keepIt; if (recorder.state !== 'inactive') recorder.stop(); };
    const mine = recording = { stop: () => end(true), cancel: () => end(false) };
    $('mic').classList.add('live');
    const timer = setInterval(() => {
      const now = performance.now(), v = loudness();
      micLevel = Math.min(1, v * 8);
      if (v > threshold) { spoke = true; lastVoice = now; }
      if (spoke ? (now - lastVoice > 1200 || now - started > 30000) : now - started > startTimeout) end(spoke);
    }, 50);
    recorder.onstop = () => {
      clearInterval(timer);
      stream.getTracks().forEach(t => t.stop()); // releases the microphone
      if (recording === mine) {
        recording = null;
        micLevel = 0;
        $('mic').classList.remove('live');
      }
      resolve(keep ? new Blob(chunks, { type: recorder.mimeType }) : null);
    };
    recorder.start();
  });
}
async function talk(startTimeout = 8000) {
  speaker.stop();
  recording?.cancel();
  if (controller) { controller.abort(); await sleep(100); } // talking over a reply interrupts it
  setState('listening');
  try {
    const blob = await record(startTimeout);
    if (!blob) { if (state === 'listening') setState('idle'); return; }
    setState('processing', 'Transcribing');
    const text = await transcribe(blob);
    setState('idle');
    if (text) send(text, true);
  } catch (e) { micError(e); }
}

// ---- Wake word ------------------------------------------------------------------------------
// While on, the microphone stays open and 80 ms audio frames are streamed to the local server, which runs a
// dedicated "hey jarvis" detector. After it fires, the frames that follow are kept as your request (so nothing
// is lost between the wake word and the request) until you pause, then transcribed. Audio is never stored.
const WAKE_PREFIX = /^\W*(?:(?:hey|hi|hay|ok|okay|hello|yo)\W+)?(?:[jgdty]|ch|tr|sh)[aeiou]+(?:r?[vwb]|r)[aeiou]+[sz]+h?\b\W*/i;
const WORKLET = `registerProcessor('frames', class extends AudioWorkletProcessor {
  constructor() { super(); this.buf = new Int16Array(1280); this.n = 0; }
  process(inputs) {
    const ch = inputs[0][0];
    if (ch) for (let i = 0; i < ch.length; i++) {
      this.buf[this.n++] = Math.max(-1, Math.min(1, ch[i])) * 32767;
      if (this.n === 1280) { this.port.postMessage(this.buf.slice()); this.n = 0; }
    }
    return true;
  }
});`;
function wavBlob(frames) { // 16 kHz 16-bit mono
  const bytes = frames.length * 2560, view = new DataView(new ArrayBuffer(44));
  const text = (at, s) => [...s].forEach((c, i) => view.setUint8(at + i, c.charCodeAt(0)));
  text(0, 'RIFF'); view.setUint32(4, 36 + bytes, true); text(8, 'WAVEfmt ');
  view.setUint32(16, 16, true); view.setUint16(20, 1, true); view.setUint16(22, 1, true);
  view.setUint32(24, 16000, true); view.setUint32(28, 32000, true); view.setUint16(32, 2, true); view.setUint16(34, 16, true);
  text(36, 'data'); view.setUint32(40, bytes, true);
  return new Blob([view, ...frames], { type: 'audio/wav' });
}
const wake = {
  mode: 'off', // off | waiting (for the wake word) | request (recording what you say next)
  stream: null, ctx: null, ws: null, closest: 0,
  frames: [], spoke: false, started: 0, lastVoice: 0, startTimeout: 0, prompted: false,

  async start() {
    if (this.mode !== 'off' || !micSupported) return;
    this.mode = 'waiting';
    try {
      this.stream = await navigator.mediaDevices.getUserMedia(micConstraints());
      this.ctx = new AudioContext({ sampleRate: 16000 }); // the browser resamples the microphone for us
      await this.ctx.audioWorklet.addModule(URL.createObjectURL(new Blob([WORKLET], { type: 'application/javascript' })));
      const node = new AudioWorkletNode(this.ctx, 'frames');
      this.ctx.createMediaStreamSource(this.stream).connect(node);
      node.port.onmessage = e => this.onFrame(e.data);
      this.ws = new WebSocket(location.origin.replace(/^http/, 'ws') + '/api/wake');
      this.ws.onmessage = e => this.onServer(e.data);
      $('mic').classList.add('wake');
      if (state === 'idle') setState('idle');
    } catch (e) {
      this.stop();
      micError(e);
      prefs.set('wake', false);
      $('pref-wake').checked = false;
    }
  },
  stop() {
    if (this.mode === 'request') this.end('cancel');
    this.stream?.getTracks().forEach(t => t.stop()); // releases the microphone
    this.ctx?.close();
    this.ws?.close();
    Object.assign(this, { mode: 'off', stream: null, ctx: null, ws: null, closest: 0 });
    $('mic').classList.remove('wake');
    if (state === 'idle') setState('idle');
  },
  hint() { return 'Say "Hey JARVIS"' + (this.closest ? ` · closest match ${Math.round(this.closest * 100)}%` : ''); },
  onServer(message) {
    if (this.mode !== 'waiting') return;
    if (message === 'wake') { this.closest = 0; log('Wake word detected'); this.begin(3000, false); }
    else if (message.startsWith('score:')) { // a near miss: show how close, so the threshold can be tuned
      this.closest = Math.max(this.closest, +message.slice(6));
      if (state === 'idle') setState('idle');
    }
  },
  onFrame(frame) { // one 80 ms Int16Array
    if (this.mode === 'waiting') { if (this.ws?.readyState === 1) this.ws.send(frame.buffer); return; }
    if (this.mode !== 'request' || speaker.playing) return; // don't record JARVIS saying "Yes?"
    let sum = 0;
    for (const v of frame) sum += v * v;
    const rms = Math.sqrt(sum / frame.length) / 32768, now = performance.now();
    micLevel = Math.min(1, rms * 8);
    this.frames.push(frame);
    if (rms > prefs.get('sensitivity', 0.02)) { this.spoke = true; this.lastVoice = now; }
    if (this.spoke ? (now - this.lastVoice > 1200 || now - this.started > 30000) : now - this.started > this.startTimeout)
      this.end(this.spoke ? 'send' : 'silence');
  },
  // Start recording a request. `prompted` = JARVIS has already asked, so silence just ends it.
  async begin(startTimeout, prompted) {
    if (this.mode !== 'waiting') return;
    this.mode = 'request';
    speaker.stop();
    if (controller) { controller.abort(); await sleep(100); } // the wake word interrupts a reply
    Object.assign(this, { frames: [], spoke: false, started: performance.now(), lastVoice: 0, startTimeout, prompted });
    recording = { stop: () => this.end('send'), cancel: () => this.end('cancel') };
    $('mic').classList.add('live');
    setState('listening');
  },
  async end(how) { // 'send' | 'silence' | 'cancel'
    if (this.mode !== 'request') return;
    const frames = this.frames, prompted = this.prompted;
    Object.assign(this, { mode: 'waiting', frames: [] });
    recording = null;
    micLevel = 0;
    $('mic').classList.remove('live');
    if (how === 'send' && frames.length) {
      setState('processing', 'Transcribing');
      try {
        const text = (await transcribe(wavBlob(frames))).replace(WAKE_PREFIX, '').trim(); // drop an echoed "Hey Jarvis"
        setState('idle');
        if (text) send(text, true);
      } catch (e) { setState('error', e.message); }
    } else if (how === 'silence' && !prompted) { // "Hey Jarvis" and then nothing: ask, then listen again
      setState('idle');
      await speaker.say('Yes?');
      this.begin(8000, true);
    } else if (state === 'listening') setState('idle');
  },
};

// After a spoken reply to a spoken request, keep the conversation going without the wake word or a click.
function listenForFollowUp() {
  if (state !== 'idle' || recording) return;
  if (wake.mode === 'waiting') wake.begin(5000, true); else if (wake.mode === 'off') talk(5000);
}

// ---- Wiring and settings ----------------------------------------------------------------------
$('mic').onclick = () => {
  if (recording) return recording.stop(); // second click: done talking
  if (wake.mode === 'waiting') wake.begin(8000, true); else if (wake.mode === 'off') talk();
};

async function initVoice() {
  if (!micSupported) {
    $('mic').disabled = true;
    $('mic').title = 'This browser cannot record audio';
  }
  try { installedVoices = (await api('/api/voice')).voices; } catch { installedVoices = []; }
  const label = v => { // "en_GB-alan-medium" -> "Alan (British)"
    const [locale, name = v] = v.split('-');
    const accent = { en_GB: 'British', en_US: 'American' }[locale] || locale;
    return `${name[0].toUpperCase()}${name.slice(1).replace(/_/g, ' ')} (${accent})`;
  };
  $('pref-voice').replaceChildren(...installedVoices.map(v => new Option(label(v), v)));
  $('pref-voice').value = currentVoice();
  $('voice-missing').hidden = installedVoices.length > 0;

  const bind = (id, key, fallback, read = el => el.value, changed) => {
    const el = $(id);
    if (el.type === 'checkbox') el.checked = prefs.get(key, fallback); else el.value = prefs.get(key, fallback);
    el.onchange = () => { prefs.set(key, read(el)); changed?.(); };
  };
  bind('pref-speak', 'speak', 'voice');
  bind('pref-voice', 'voice', currentVoice());
  bind('pref-speed', 'speed', 1, el => +el.value);
  bind('pref-volume', 'volume', 1, el => +el.value);
  bind('pref-mic', 'mic', '', el => el.value, () => { if (wake.mode !== 'off') { wake.stop(); wake.start(); } });
  bind('pref-sensitivity', 'sensitivity', 0.02, el => +el.value);
  bind('pref-wake', 'wake', true, el => el.checked, () => prefs.get('wake', true) ? wake.start() : wake.stop());
  $('test-voice').onclick = () => speaker.say('Good evening. All systems are online.');
  $('pref-wake').disabled = !micSupported;
  if (prefs.get('wake', true)) await wake.start();
  if (new URLSearchParams(location.search).has('wake')) { // opened by "Hey JARVIS" said while the window was closed
    history.replaceState(null, '', '/');
    log('Wake word detected');
    await speaker.say('Yes?');
    if (wake.mode === 'waiting') wake.begin(8000, true); else talk(8000);
  }
}
// Microphone names are only visible once permission has been granted, so refresh when Settings opens.
async function refreshMics() {
  if (!micSupported) return;
  const mics = (await navigator.mediaDevices.enumerateDevices()).filter(d => d.kind === 'audioinput' && d.deviceId && d.deviceId !== 'default');
  $('pref-mic').replaceChildren(new Option('System default', ''), ...mics.map((d, i) => new Option(d.label || `Microphone ${i + 1}`, d.deviceId)));
  $('pref-mic').value = prefs.get('mic', '');
}
$('open-settings').addEventListener('click', refreshMics);
initVoice();
