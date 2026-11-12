// Screen 2: a 3D map of everything JARVIS knows about (data from /api/graph).
// Loaded after app.js and shares its globals ($, api, log, openConversation).

const TYPES = { // display order, label and color
  core: ['Core', '#3cdcff'], hub: ['Hubs', '#f5b942'], conversation: ['Conversations', '#4f8cff'],
  model: ['Models', '#34d399'], mode: ['Modes', '#ff7ab6'], voice: ['Voices', '#b07cff'], speech: ['Speech', '#9aa7b8'],
};
const SIZES = { core: 30, hub: 12 };
let graph3d = null;
let graphData = { nodes: [], links: [] };
const hiddenTypes = new Set();

const endId = end => typeof end === 'object' ? end.id : end; // the library swaps link ends for node objects

async function showGraph() {
  if (!window.ForceGraph3D) { $('inspector').textContent = 'The graph library is missing (web/vendor/3d-force-graph.min.js).'; return; }
  graphData = await api('/api/graph');
  const box = $('graph');
  if (!graph3d) {
    graph3d = new ForceGraph3D(box)
      .backgroundColor('rgba(0,0,0,0)')
      .nodeLabel(n => n.label)
      .nodeColor(n => TYPES[n.type]?.[1] || '#888')
      .nodeVal(n => SIZES[n.type] || 3)
      .nodeOpacity(0.95)
      .linkColor(() => 'rgba(110,200,255,0.35)')
      .linkWidth(0.4)
      .onNodeClick(inspect);
    new ResizeObserver(() => graph3d.width(box.clientWidth).height(box.clientHeight)).observe(box);
  }
  graph3d.width(box.clientWidth).height(box.clientHeight);
  applyFilter();
  $('graph-count').textContent = `${graphData.nodes.length} nodes · ${graphData.links.length} connections`;
  renderLegend();
  renderHubs();
}

function applyFilter() {
  const nodes = graphData.nodes.filter(n => !hiddenTypes.has(n.type)).map(n => ({ ...n }));
  const ids = new Set(nodes.map(n => n.id));
  const links = graphData.links
    .map(l => ({ source: endId(l.source), target: endId(l.target) }))
    .filter(l => ids.has(l.source) && ids.has(l.target));
  graph3d.graphData({ nodes, links });
}

function listRow(className, color, label, value, onclick) {
  const el = document.createElement('div'), dot = document.createElement('i'), name = document.createElement('span'), val = document.createElement('em');
  el.className = `row ${className}`;
  el.tabIndex = 0;
  el.setAttribute('role', 'button');
  dot.className = 'swatch';
  dot.style.background = color;
  name.textContent = label;
  val.textContent = value;
  el.append(dot, name, val);
  el.onclick = onclick;
  el.onkeydown = e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onclick(); } };
  return el;
}
function renderLegend() {
  $('legend').replaceChildren(...Object.entries(TYPES).map(([type, [label, color]]) => {
    const count = graphData.nodes.filter(n => n.type === type).length;
    return listRow(`legend-row${hiddenTypes.has(type) ? ' hidden-type' : ''}`, color, label, count, () => {
      hiddenTypes.has(type) ? hiddenTypes.delete(type) : hiddenTypes.add(type);
      applyFilter();
      renderLegend();
    });
  }));
}
function degree(id) {
  return graphData.links.filter(l => endId(l.source) === id || endId(l.target) === id).length;
}
function renderHubs() {
  const top = graphData.nodes.map(n => [n, degree(n.id)]).sort((a, b) => b[1] - a[1]).slice(0, 6);
  $('hubs').replaceChildren(...top.map(([n, d]) => listRow('hub-row', TYPES[n.type]?.[1], n.label, d, () => focusNode(n.id))));
}

function focusNode(id) {
  const node = graph3d.graphData().nodes.find(n => n.id === id);
  if (!node) return;
  inspect(node);
}
function inspect(node) {
  // Fly the camera to the node, looking at it from a little further out.
  const far = 1 + 90 / Math.max(1, Math.hypot(node.x, node.y, node.z));
  graph3d.cameraPosition({ x: node.x * far, y: node.y * far, z: node.z * far }, node, 900);

  const box = $('inspector'), tag = document.createElement('span'), title = document.createElement('strong'), detail = document.createElement('p');
  tag.className = 'tag';
  tag.textContent = TYPES[node.type]?.[0] || node.type;
  title.textContent = node.label;
  detail.textContent = node.detail || '';
  const neighbours = graphData.links
    .filter(l => endId(l.source) === node.id || endId(l.target) === node.id)
    .map(l => graphData.nodes.find(n => n.id === (endId(l.source) === node.id ? endId(l.target) : endId(l.source))))
    .filter(Boolean);
  const list = document.createElement('div');
  list.className = 'rows';
  list.append(...neighbours.slice(0, 8).map(n => listRow('hub-row', TYPES[n.type]?.[1], n.label, '', () => focusNode(n.id))));
  box.replaceChildren(tag, title, detail, list);
  if (node.type === 'conversation') {
    const open = document.createElement('button');
    open.className = 'chip';
    open.textContent = 'Open conversation';
    open.onclick = () => openConversation(+node.id.split(':')[1]);
    box.append(open);
  }
}

$('graph-search').onkeydown = e => {
  if (e.key !== 'Enter') return;
  e.preventDefault(); // keep Enter from reaching anything else
  const q = e.target.value.trim().toLowerCase();
  const hit = q && graph3d?.graphData().nodes.find(n => n.label.toLowerCase().includes(q));
  if (hit) inspect(hit); else $('inspector').textContent = q ? `Nothing in the graph matches "${e.target.value}".` : '';
};
