const $ = id => document.getElementById(id);
const STATUS_TEXT = { idle: 'Idle', listening: 'Listening', processing: 'Processing', thinking: 'Thinking',
  executing: 'Executing', waiting: 'Waiting for approval', responding: 'Responding', speaking: 'Speaking', error: 'Error' };
const VIEWS = ['orb', 'home', 'graph', 'settings'];

let state = 'idle';
let view = 'orb';
let conversationId = null;
let controller = null; // set while a reply is streaming
let energy = 0;        // visualizer energy, bumped by each arriving token
let pendingConfirm = null; // { id, card } while JARVIS waits for the user to approve an action

async function api(path, options) {
  const r = await fetch(path, options);
  if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText);
  return r.status === 204 ? null : r.json();
}
const putSettings = values => api('/api/settings', {
  method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(values),
});

function setState(next, text) {
  state = next;
  $('status').dataset.state = document.body.dataset.state = next;
  const label = text || (next === 'idle' && wake.mode === 'waiting' ? wake.hint() : STATUS_TEXT[next]);
  $('status-text').textContent = label;
  $('orb-status').textContent = next === 'idle' ? 'Online' : text || STATUS_TEXT[next];
  $('send').textContent = controller || speaker.active ? 'Stop' : 'Send';
}

// Activity log: real events from this session, shown on the orb screen and the homescreen feed. Not saved.
function log(text, kind = 'info') {
  const time = new Date().toLocaleTimeString('en-GB');
  for (const id of ['activity', 'feed']) {
    const box = $(id), row = document.createElement('div'), stamp = document.createElement('time');
    row.className = `log-row ${kind}`;
    stamp.textContent = time;
    row.append(stamp, text);
    box.append(row);
    while (box.children.length > 80) box.firstChild.remove();
    box.scrollTop = box.scrollHeight;
  }
}

// Minimal Markdown: fenced code, bullets, inline code, bold, http(s) links. Everything is HTML-escaped first, so the
// only tags that can appear are the ones built here; links are limited to http and https addresses.
const escapeHtml = s => s.replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const link = (label, url) => `<a href="${url}" target="_blank" rel="noopener noreferrer">${label}</a>`;
function renderMarkdown(text) {
  return text.split('```').map((part, i) => i % 2
    ? `<pre><code>${escapeHtml(part.replace(/^[\w-]*\n/, ''))}</code></pre>`
    : escapeHtml(part)
      .replace(/^(\s*)[*-]\s+/gm, '$1• ')
      .replace(/`([^`\n]+)`/g, '<code>$1</code>')
      .replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>')
      .replace(/\[([^\]\n]+)\]\((https?:\/\/[^\s)]+)\)|(^|[\s(])(https?:\/\/[^\s<)]+)/g,
        (m, label, url, before, bare) => url ? link(label, url) : before + link(bare, bare))
  ).join('');
}

function addMessage(role, text) {
  const el = document.createElement('div');
  el.className = `msg ${role}`;
  if (role === 'user') el.textContent = text; else el.innerHTML = renderMarkdown(text);
  $('messages').append(el);
  $('messages').scrollTop = $('messages').scrollHeight;
  return el;
}
function addMeta(el, text) {
  const meta = document.createElement('span');
  meta.className = 'meta';
  meta.textContent = text;
  el.append(meta);
}

// ---- Screens ----------------------------------------------------------------------------------
function showView(name) {
  view = document.body.dataset.view = name;
  for (const v of VIEWS) $('view-' + v).hidden = v !== name;
  document.querySelectorAll('[data-go]').forEach(b => b.classList.toggle('active', b.dataset.go === name));
  $('bubble').hidden = true;
  if (name === 'graph') showGraph();
  if (name === 'home') { loadConversations(); loadMemories(); }
  if (name === 'settings') loadGeneral();
  if (name !== 'graph') refreshStatus();
}
// ponytail: screen switching is matched with regexes on short commands. Phase 3 replaces this with real
// tool calls chosen by the model, which will also handle looser phrasing.
const VIEW_INTENTS = [
  [/\b(graph|brain|knowledge map|network|screen (2|two))\b/i, 'graph', 'The graph, on screen.'],
  [/\b(home ?screen|home|dashboard|command cent(er|re))\b/i, 'home', 'Homescreen, on screen.'],
  [/\bsettings\b/i, 'settings', 'Settings, on screen.'],
  [/\b(orb|main screen|screen (1|one))\b/i, 'orb', 'Back to the main screen.'],
];
// ---- Approvals ----------------------------------------------------------------------------------
// The server pauses before any consequential action and sends its exact effect. Nothing runs until answered.
function askConfirm(ev, speak, viaVoice) {
  const card = document.createElement('div'), title = document.createElement('strong'), preview = document.createElement('pre');
  const buttons = document.createElement('div'), yes = document.createElement('button'), no = document.createElement('button');
  card.className = 'msg confirm';
  title.textContent = 'Approval needed';
  preview.textContent = ev.preview;
  buttons.className = 'confirm-buttons';
  yes.className = 'chip approve';
  yes.textContent = 'Approve';
  yes.onclick = () => answerConfirm(true);
  no.className = 'chip';
  no.textContent = 'Deny';
  no.onclick = () => answerConfirm(false);
  buttons.append(yes, no);
  card.append(title, preview, buttons);
  $('messages').append(card);
  $('messages').scrollTop = $('messages').scrollHeight;
  pendingConfirm = { id: ev.id, card };
  if (view !== 'orb') showView('orb'); // the card lives in the transcript
  setState('waiting');
  log(`Approval needed: ${ev.label}`, 'warn');
  if (speak) speaker.say('Shall I go ahead?').then(() => { // then listen for a spoken yes or no
    if (pendingConfirm && viaVoice && wake.mode === 'waiting') wake.begin(8000, true);
  });
}
function closeConfirm(text) {
  if (!pendingConfirm) return;
  const note = document.createElement('span');
  note.className = 'meta';
  note.textContent = text;
  pendingConfirm.card.querySelector('.confirm-buttons').replaceWith(note);
  pendingConfirm = null;
}
async function answerConfirm(approve) {
  if (!pendingConfirm) return;
  const { id } = pendingConfirm;
  closeConfirm(approve ? 'Approved' : 'Declined');
  speaker.stop();
  setState(approve ? 'executing' : 'processing');
  log(approve ? 'Approved' : 'Declined', approve ? 'info' : 'warn');
  await api(`/api/confirm/${id}`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ approve }) })
    .catch(e => log(e.message, 'error'));
}

function localCommand(text, viaVoice) {
  const t = text.trim();
  if (pendingConfirm) { // while an approval is waiting, a yes or no (typed or spoken) answers it
    if (/^(yes|yeah|yep|yup|sure|ok(ay)?|go ahead|do it|confirm|approve|send it|proceed)\b/i.test(t)) { answerConfirm(true); return true; }
    if (/^(no|nope|don'?t|do not|cancel|stop|deny|decline|never ?mind)\b/i.test(t)) { answerConfirm(false); return true; }
  }
  if (t.length > 60) return false;
  if (/^(good morning|((give me|what'?s|what is) )?(my )?((daily|morning) )?briefing|brief me)\b/i.test(t)) { giveBriefing(viaVoice); return true; }
  let hit;
  if (/^(please )?((go|take me) )?back\b|^(close|hide|exit) (it|this|that)\b/i.test(t)) hit = [null, 'orb', 'Back to the main screen.'];
  else if (/^(please )?(show|open|bring up|pull up|go to|take me to|switch to|display)\b/i.test(t)) hit = VIEW_INTENTS.find(([re]) => re.test(t));
  if (!hit) return false;
  log(`Command: ${t}`);
  showView(hit[1]);
  if (shouldSpeak(viaVoice)) speaker.say(hit[2]);
  return true;
}

// ---- Chat -------------------------------------------------------------------------------------
async function send(text, viaVoice = false, briefing = false) {
  speaker.stop();
  recording?.cancel();
  if (!briefing && localCommand(text, viaVoice)) return;
  if (controller) return; // one request at a time (reached by voice while a reply or an approval is in progress)
  // A briefing is meant to be heard, so it is spoken unless speech is switched off altogether.
  const speak = briefing ? !!currentVoice() && prefs.get('speak', 'voice') !== 'never' : shouldSpeak(viaVoice);
  log(`${viaVoice ? 'Heard' : 'You'}: ${text.length > 70 ? text.slice(0, 70) + '...' : text}`);
  addMessage('user', text);
  const el = addMessage('assistant', '');
  const bubble = $('bubble');
  let full = '', model = '', failed = false;
  controller = new AbortController();
  setState('processing');
  const onEvent = ev => {
    if (ev.type === 'start') { conversationId = ev.conversation_id; model = ev.model; }
    else if (ev.type === 'thinking') setState('thinking');
    else if (ev.type === 'token') {
      full += ev.text;
      el.innerHTML = renderMarkdown(full); // ponytail: re-renders the whole reply per token; fine at chat lengths
      if (view !== 'orb') { bubble.hidden = false; bubble.innerHTML = el.innerHTML; }
      energy = Math.min(1, energy + 0.35);
      if (speak) speaker.feed(ev.text);
      if (state === 'processing' || state === 'thinking' || state === 'executing') setState('responding');
      $('messages').scrollTop = $('messages').scrollHeight;
    }
    else if (ev.type === 'tool') { setState('executing', ev.label); log(ev.label); }
    else if (ev.type === 'tool_result') { log(ev.summary, ev.ok ? 'info' : 'warn'); setState('processing'); }
    else if (ev.type === 'confirm') askConfirm(ev, speak, viaVoice);
    else if (ev.type === 'sources') { // pages the server really fetched; shown, not spoken
      full += ev.text;
      el.innerHTML = renderMarkdown(full);
      if (view !== 'orb') bubble.innerHTML = el.innerHTML;
    }
    else if (ev.type === 'done') {
      addMeta(el, `${model}${ev.tokens_per_s ? ` · ${ev.tokens_per_s} tokens/s` : ''}`);
      log(`Replied with ${model}${ev.tokens_per_s ? ` at ${ev.tokens_per_s} tokens/s` : ''}`);
    }
    else if (ev.type === 'error') throw new Error(ev.message);
  };
  try {
    const r = await fetch('/api/chat', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, signal: controller.signal,
      body: JSON.stringify({ message: text, conversation_id: conversationId, mode: $('mode').value, voice: speak, briefing }),
    });
    if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText);
    const reader = r.body.getReader(), decoder = new TextDecoder();
    let buffer = '';
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let nl;
      while ((nl = buffer.indexOf('\n')) >= 0) {
        onEvent(JSON.parse(buffer.slice(0, nl)));
        buffer = buffer.slice(nl + 1);
      }
    }
  } catch (e) {
    speaker.stop();
    if (e.name === 'AbortError') { addMeta(el, 'Stopped'); log('Reply stopped', 'warn'); }
    else {
      failed = true;
      el.classList.add('error');
      el.textContent = (full ? full + '\n\n' : '') + e.message;
      if (view !== 'orb') { bubble.hidden = false; bubble.textContent = e.message; }
      log(e.message, 'error');
    }
  } finally {
    controller = null;
    closeConfirm('Cancelled'); // only if one was still waiting: Stop cancels it, and nothing runs
    if (!full && !failed) el.remove(); // nothing was said (for example, stopped during a tool)
    setState(failed ? 'error' : speaker.playing ? 'speaking' : 'idle');
    if (speak && !failed) speaker.finish(viaVoice ? listenForFollowUp : null);
    loadConversations();
  }
}

async function loadConversations() {
  const list = await api('/api/conversations');
  const box = $('conversations');
  box.replaceChildren();
  if (!list.length) {
    const empty = document.createElement('div');
    empty.className = 'dim';
    empty.textContent = 'No conversations yet.';
    box.append(empty);
  }
  for (const c of list) {
    const row = document.createElement('div');
    row.className = 'conv' + (c.id === conversationId ? ' active' : '');
    const title = document.createElement('button');
    title.className = 'title-btn';
    title.textContent = c.title;
    title.onclick = () => openConversation(c.id);
    const del = document.createElement('button');
    del.className = 'del';
    del.textContent = '×';
    del.setAttribute('aria-label', `Delete conversation: ${c.title}`);
    del.onclick = async () => {
      if (!confirm(`Delete "${c.title}"? This cannot be undone.`)) return;
      await api(`/api/conversations/${c.id}`, { method: 'DELETE' });
      if (c.id === conversationId) { conversationId = null; $('messages').replaceChildren(); }
      loadConversations();
    };
    row.append(title, del);
    box.append(row);
  }
}
function newChat() {
  controller?.abort();
  speaker.stop();
  conversationId = null;
  $('messages').replaceChildren();
  showView('orb');
  $('input').focus();
}
async function openConversation(id) {
  controller?.abort();
  speaker.stop();
  conversationId = id;
  $('messages').replaceChildren();
  for (const m of await api(`/api/conversations/${id}/messages`)) addMessage(m.role, m.content);
  showView('orb');
}

// ---- Status panels ------------------------------------------------------------------------------
const row = (label, value, tone = '') => {
  const el = document.createElement('div'), name = document.createElement('span'), val = document.createElement('em');
  el.className = 'row';
  name.textContent = label;
  val.textContent = value;
  val.className = tone;
  el.append(name, val);
  return el;
};
async function refreshStatus() {
  const [status, settings] = await Promise.all([api('/api/status'), api('/api/settings')]);
  $('mode').value = settings.mode;
  const modelNow = settings[`model_${settings.mode}`];
  const wakeOn = wake.mode !== 'off';
  const voiceName = currentVoice() ? currentVoice().split('-')[1] : 'none installed';

  // Orb screen: AI Core facts
  const facts = [['Model', modelNow], ['In memory', status.loaded.join(', ') || 'nothing loaded'], ['Mode', settings.mode],
    ['Voice', voiceName], ['Wake word', wakeOn ? 'on' : 'off']];
  $('core-facts').replaceChildren(...facts.flatMap(([k, v]) => {
    const dt = document.createElement('dt'), dd = document.createElement('dd');
    dt.textContent = k;
    dd.textContent = v;
    return [dt, dd];
  }));

  // Homescreen: overview and model list
  $('overview').replaceChildren(
    row('AI core', status.ollama ? 'Active' : 'Offline', status.ollama ? '' : 'warn'),
    row('Language models', `${status.models.length} installed`),
    row('Voice', currentVoice() ? `Online · ${voiceName}` : 'No voice installed', currentVoice() ? '' : 'warn'),
    row('Wake word', wakeOn ? 'Listening' : 'Off', wakeOn ? '' : 'off'),
    row('Speech recognition', micSupported ? 'Ready' : 'Unavailable', micSupported ? '' : 'warn'),
    row('Memory', settings.memory_enabled === '1' ? 'On' : 'Off', settings.memory_enabled === '1' ? '' : 'off'),
    row('WhatsApp sending', 'Needs testing', 'off'),
    row('Calendar, GitHub, Telegram', 'Not connected', 'off'),
  );
  $('memory-enabled').checked = settings.memory_enabled === '1';
  const usedBy = name => ['fast', 'balanced', 'advanced'].filter(m => settings[`model_${m}`] === name).join(', ');
  $('llm').replaceChildren(...(status.models.length ? status.models : [modelNow]).map(name =>
    row(`${name}${usedBy(name) ? ` · ${usedBy(name)}` : ''}`,
      !status.models.includes(name) ? 'Not installed' : status.loaded.includes(name) ? 'In memory' : 'Installed',
      !status.models.includes(name) ? 'warn' : status.loaded.includes(name) ? '' : 'off')));

  // Settings: models
  const line = $('ollama-status');
  line.className = status.ollama ? 'ok' : 'bad';
  line.textContent = status.ollama
    ? `Ollama is running with ${status.models.length} model(s) installed.`
    : 'Ollama is not reachable. Start Ollama, then reopen Settings.';
  for (const key of ['model_fast', 'model_balanced', 'model_advanced']) {
    const select = $(key);
    const names = status.models.includes(settings[key]) ? status.models : [settings[key], ...status.models];
    select.replaceChildren(...names.map(name => new Option(status.models.includes(name) ? name : `${name} (not installed)`, name)));
    select.value = settings[key];
    select.onchange = () => putSettings({ [key]: select.value }).then(refreshStatus);
  }
  if (!status.ollama && !controller && state !== 'error') { setState('error', 'Ollama offline'); log('Ollama is not reachable', 'error'); }
  else if (state === 'error' && status.ollama && $('status-text').textContent === 'Ollama offline') setState('idle');
}
async function refreshSystem() {
  if (document.hidden || (view !== 'orb' && view !== 'home')) return;
  const s = await api('/api/system').catch(() => null);
  if (!s) return;
  const values = { cpu: s.cpu, ram: s.ram, disk: s.disk, vram: s.gpu?.vram, battery: s.battery?.percent };
  for (const [key, v] of Object.entries(values)) {
    const text = v == null ? 'n/a' : `${Math.round(v)}%`;
    document.querySelectorAll(`[data-stat="${key}"], [data-ring="${key}"]`).forEach(el => el.style.setProperty('--v', v ?? 0));
    document.querySelectorAll(`[data-stat-text="${key}"], [data-ring="${key}"] b`).forEach(el => { el.textContent = text; });
  }
}
// ---- Daily briefing and general settings --------------------------------------------------------------
// The server gathers today's reminders and unread mail; the model only has to say it.
function giveBriefing(viaVoice = false) {
  log('Daily briefing');
  if (view !== 'orb') showView('orb');
  send('Give me my daily briefing.', viaVoice, true);
}
// ponytail: the scheduled briefing fires from this window, so it needs the window open at that time.
// Moving it into the tray process is the upgrade once briefings matter while the window is closed.
async function checkBriefing() {
  const today = new Date().toDateString(), now = new Date().toTimeString().slice(0, 5);
  const at = (await api('/api/settings').catch(() => ({}))).briefing_time;
  if (!at || now < at || prefs.get('lastBriefing', '') === today || controller || recording || state !== 'idle') return;
  prefs.set('lastBriefing', today);
  giveBriefing();
}
async function loadGeneral() {
  const [settings, autostart] = await Promise.all([api('/api/settings'), api('/api/autostart')]);
  $('briefing-time').value = settings.briefing_time;
  $('autostart').checked = autostart.enabled;
}
$('briefing-time').onchange = e => putSettings({ briefing_time: e.target.value });
$('autostart').onchange = async e => {
  const want = e.target.checked;
  const got = await api('/api/autostart', { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ enabled: want }) })
    .catch(err => { log(err.message, 'error'); return { enabled: !want }; });
  e.target.checked = got.enabled;
  log(got.enabled ? 'JARVIS will start with Windows' : 'JARVIS will not start with Windows');
};

// ---- Memory: everything JARVIS has saved about you, to inspect, correct or delete ---------------------
async function loadMemories() {
  const list = await api('/api/memories').catch(() => []);
  const box = $('memories');
  if (!list.length) { box.textContent = 'Nothing saved yet. Tell JARVIS something to remember.'; return; }
  box.replaceChildren(...list.map(m => {
    const el = row(m.text, '×', 'off'), [text, del] = el.children;
    el.title = `${m.category}, saved ${m.created_at.slice(0, 10)}. Click the text to edit.`;
    text.style.cursor = del.style.cursor = 'pointer';
    text.style.whiteSpace = 'normal';
    text.onclick = async () => {
      const next = prompt('Edit this memory:', m.text);
      if (!next || next.trim() === m.text) return;
      await api(`/api/memories/${m.id}`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ text: next.trim() }) })
        .catch(e => alert(e.message));
      loadMemories();
    };
    del.setAttribute('role', 'button');
    del.setAttribute('aria-label', `Delete memory: ${m.text}`);
    del.onclick = async () => {
      if (!confirm(`Delete this memory? This cannot be undone.\n\n${m.text}`)) return;
      await api(`/api/memories/${m.id}`, { method: 'DELETE' });
      loadMemories();
    };
    return el;
  }));
}
$('memory-enabled').onchange = e => putSettings({ memory_enabled: e.target.checked ? '1' : '0' }).then(refreshStatus);

// ---- Gmail ------------------------------------------------------------------------------------
async function refreshMail() {
  const m = await api('/api/mail').catch(() => null);
  if (!m) return;
  const box = $('inbox');
  $('mail-form').hidden = m.connected;
  $('mail-disconnect').hidden = !m.connected;
  $('mail-status').textContent = m.connected ? `Connected as ${m.address}. JARVIS can read mail but cannot send, delete or change it.` : 'Not connected.';
  if (!m.connected) box.textContent = 'Not connected. Add Gmail in Settings.';
  else if (m.error) box.textContent = `Could not reach Gmail: ${m.error}`;
  else box.replaceChildren(row('Unread', String(m.unread), m.unread ? '' : 'off'),
    ...m.latest.map(mail => row(mail.subject, mail.from.replace(/\s*<.*>/, '').slice(0, 18), 'off')));
}
$('mail-form').onsubmit = async e => {
  e.preventDefault();
  const body = JSON.stringify({ address: $('mail-address').value.trim(), app_password: $('mail-password').value });
  $('mail-password').value = ''; // never keep it in the page
  $('mail-status').textContent = 'Connecting...';
  try {
    await api('/api/mail', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body });
    log('Gmail connected');
  } catch (err) { log(err.message, 'error'); }
  await refreshMail().catch(() => {});
  if (!$('mail-form').hidden) $('mail-status').textContent = 'Gmail refused that address and app password. Check both and try again.';
};
$('mail-disconnect').onclick = async () => {
  if (!confirm('Disconnect Gmail? The stored app password is removed from this laptop.')) return;
  await api('/api/mail', { method: 'DELETE' });
  log('Gmail disconnected');
  refreshMail();
};

// ---- Calendar and GitHub ------------------------------------------------------------------------
async function refreshAccounts() {
  const a = await api('/api/accounts').catch(() => null);
  if (!a) return;
  const text = { calendar: a.calendar ? 'Connected. JARVIS can read your events but cannot change them.' : 'Not connected.',
    github: a.github ? `Connected as ${a.github}. Read-only.` : 'Not connected.' };
  for (const name of ['calendar', 'github']) {
    $(`${name}-status`).textContent = text[name];
    document.querySelector(`.account-form[data-account=${name}]`).hidden = !!a[name];
    document.querySelector(`.account-disconnect[data-account=${name}]`).hidden = !a[name];
  }
}
document.querySelectorAll('.account-form').forEach(form => form.onsubmit = async e => {
  e.preventDefault();
  const name = form.dataset.account, input = $(`${name}-value`);
  const body = JSON.stringify({ value: input.value.trim() });
  input.value = ''; // never keep it in the page
  $(`${name}-status`).textContent = 'Connecting...';
  try {
    await api(`/api/accounts/${name}`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body });
    log(`${name === 'calendar' ? 'Calendar' : 'GitHub'} connected`);
    await refreshAccounts();
  } catch (err) { $(`${name}-status`).textContent = err.message; }
});
document.querySelectorAll('.account-disconnect').forEach(btn => btn.onclick = async () => {
  if (!confirm('Disconnect? The stored secret is removed from this laptop.')) return;
  await api(`/api/accounts/${btn.dataset.account}`, { method: 'DELETE' });
  refreshAccounts();
});

// Reminders are delivered by this window: shown, logged, and spoken unless speech is turned off.
// ponytail: only fires while the window is open; the always-on background service (Phase 7) takes this over.
async function checkReminders() {
  for (const r of await api('/api/reminders/due').catch(() => [])) {
    log(`Reminder: ${r.text}`, 'warn');
    addMessage('assistant', `**Reminder:** ${r.text}`);
    if (view !== 'orb') { $('bubble').hidden = false; $('bubble').textContent = `Reminder: ${r.text}`; }
    if (prefs.get('speak', 'voice') !== 'never' && currentVoice() && !controller && !recording) await speaker.say(`Reminder. ${r.text}`);
  }
}
function tickClock() {
  const now = new Date();
  document.querySelectorAll('[data-clock]').forEach(el => { el.textContent = now.toLocaleTimeString('en-GB'); });
  document.querySelectorAll('[data-date]').forEach(el => {
    el.textContent = now.toLocaleDateString('en-GB', { weekday: 'short', day: '2-digit', month: 'short', year: 'numeric' }).toUpperCase();
  });
}

// ---- The orb: driven by the assistant's real state, tokens arriving, and the live audio level ----------
const still = matchMedia('(prefers-reduced-motion: reduce)').matches;
let t = 0;
function draw() {
  requestAnimationFrame(draw);
  if (!still) t += 0.016;
  energy *= 0.93;
  const canvas = view === 'orb' ? $('viz') : view === 'home' ? $('viz-home') : null;
  if (!canvas) return;
  const c = canvas.getContext('2d'), w = canvas.width, mid = w / 2, R = w * 0.3;
  const busy = state === 'processing' || state === 'thinking' || state === 'executing' || state === 'waiting';
  const level = state === 'idle' || state === 'error' || busy ? 0 : Math.max(energy, audioLevel());
  const rgb = state === 'error' ? '255,90,90' : '60,220,255';
  const ring = (radius, width, alpha, dash, turn = 0) => {
    c.setLineDash(dash || []);
    c.lineDashOffset = turn;
    c.lineWidth = width;
    c.strokeStyle = `rgba(${rgb},${alpha})`;
    c.beginPath();
    c.arc(mid, mid, radius, 0, Math.PI * 2);
    c.stroke();
  };
  c.clearRect(0, 0, w, w);
  c.lineCap = 'butt';
  c.shadowColor = `rgb(${rgb})`;
  c.shadowBlur = 10;

  // Outer bars: breathe when idle, sweep while working, follow the voice while listening or speaking
  const N = 96;
  for (let i = 0; i < N; i++) {
    const a = i / N * Math.PI * 2;
    let amp = 0.14 + 0.06 * Math.sin(t * 1.5 + i * 0.35);
    if (busy) amp = 0.1 + 0.8 * Math.max(0, Math.cos(a - t * (state === 'thinking' ? 5 : 2.5))) ** 6;
    else if (level) amp = 0.14 + level * (0.3 + 0.7 * Math.abs(Math.sin(i * 1.7 + t * 9)));
    const from = R * 1.22, to = from + R * 0.34 * amp + 2;
    c.strokeStyle = `rgba(${rgb},${Math.min(1, 0.3 + amp)})`;
    c.lineWidth = 2;
    c.setLineDash([]);
    c.beginPath();
    c.moveTo(mid + Math.cos(a) * from, mid + Math.sin(a) * from);
    c.lineTo(mid + Math.cos(a) * to, mid + Math.sin(a) * to);
    c.stroke();
  }
  const spin = t * (busy ? 60 : 8);
  ring(R * 1.08, w * 0.022, 0.75, [w * 0.05, w * 0.045], -spin);          // thick segmented ring
  ring(R * 0.9, 1.5, 0.45, [3, 9], spin * 0.6);                           // fine tick ring
  ring(R * 0.72, 1, 0.3);
  ring(R * 0.52, 1, 0.2);

  // Amber accent arc
  c.setLineDash([]);
  c.shadowColor = '#f5b942';
  c.strokeStyle = 'rgba(245,185,66,.85)';
  c.lineWidth = 3;
  c.beginPath();
  c.arc(mid, mid, R * 1.62, -1.0 + t * 0.05, 0.35 + t * 0.05);
  c.stroke();
  c.shadowColor = `rgb(${rgb})`;

  // Rotating hexagon with spokes
  const hex = R * 0.5 * (1 + level * 0.12);
  c.strokeStyle = `rgba(${rgb},.8)`;
  c.lineWidth = 1.5;
  c.beginPath();
  for (let i = 0; i <= 6; i++) {
    const a = t * 0.3 + i * Math.PI / 3;
    c.lineTo(mid + Math.cos(a) * hex, mid + Math.sin(a) * hex);
  }
  c.stroke();
  c.strokeStyle = `rgba(${rgb},.35)`;
  c.beginPath();
  for (let i = 0; i < 6; i++) {
    const a = t * 0.3 + i * Math.PI / 3;
    c.moveTo(mid, mid);
    c.lineTo(mid + Math.cos(a) * hex, mid + Math.sin(a) * hex);
  }
  c.stroke();

  // Glowing core
  const core = R * 0.24 * (1 + level * 0.6 + (busy ? 0.15 * Math.sin(t * 8) : 0.05 * Math.sin(t * 2)));
  const glow = c.createRadialGradient(mid, mid, 0, mid, mid, core * 2.2);
  glow.addColorStop(0, 'rgba(255,255,255,.95)');
  glow.addColorStop(0.25, `rgba(${rgb},.85)`);
  glow.addColorStop(1, `rgba(${rgb},0)`);
  c.fillStyle = glow;
  c.beginPath();
  c.arc(mid, mid, core * 2.2, 0, Math.PI * 2);
  c.fill();
}

// ---- Wiring -----------------------------------------------------------------------------------
$('composer').onsubmit = e => {
  e.preventDefault();
  const text = $('input').value.trim();
  if (!text) return;
  if (controller) { // a reply is streaming: screen commands still work, anything else stays as a draft
    if (localCommand(text, false)) $('input').value = '';
    return;
  }
  $('input').value = '';
  send(text);
};
$('input').onkeydown = e => {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); $('composer').requestSubmit(); }
};
$('send').onclick = e => { // doubles as Stop while a reply is streaming or being spoken
  if (!controller && !speaker.active) return;
  e.preventDefault();
  controller?.abort();
  speaker.stop();
};
$('mode').onchange = e => putSettings({ mode: e.target.value }).then(refreshStatus);
$('new-chat').onclick = newChat;
$('quick-talk').onclick = () => { showView('orb'); $('mic').click(); };
$('quick-briefing').onclick = () => giveBriefing();
document.querySelectorAll('[data-go]').forEach(b => { b.onclick = () => showView(b.dataset.go); });
$('bubble').onclick = () => { $('bubble').hidden = true; };

addEventListener('DOMContentLoaded', () => { // after voice.js and graph.js have loaded too
  const hour = new Date().getHours();
  $('greeting').textContent = `» Good ${hour < 12 ? 'morning' : hour < 18 ? 'afternoon' : 'evening'}. All systems are online. How may I help?`;
  log('JARVIS core online');
  showView('orb');
  draw();
  tickClock();
  refreshSystem();
  setInterval(tickClock, 1000);
  setInterval(refreshSystem, 3000);
  setInterval(checkReminders, 20000);
  setInterval(checkBriefing, 30000);
  refreshMail();
  refreshAccounts();
  setInterval(() => { if (!document.hidden) refreshMail(); }, 120000);
  setInterval(() => { if (!document.hidden && view !== 'graph') refreshStatus(); }, 30000);
});
