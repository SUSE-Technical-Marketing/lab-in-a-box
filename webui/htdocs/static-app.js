/*
 * Static webui — offline lab builder
 * No backend, no API calls. Works completely in the browser.
 */

const state = {
  model: { common: {}, items: [], addonCfg: {}, seq: 0 },
  schemas: window.EMBEDDED_SCHEMAS || { addons: [], schemas: {} },
  baseSchema: window.EMBEDDED_BASE_SCHEMA || {},
  sel: "common",
  editing: null,
};

const $ = (s) => document.querySelector(s);
const el = (tag, cls, txt) => {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (txt) e.textContent = txt;
  return e;
};

// Render lab JSON for download
function compileLab() {
  const lab = { ...state.model.common };
  const nodesByCluster = {};

  state.model.items.forEach(item => {
    if (item.type === "node") {
      if (item.parent) {
        if (!nodesByCluster[item.parent]) nodesByCluster[item.parent] = [];
        nodesByCluster[item.parent].push(item.name);
      }
    }
  });

  // nodes
  lab.nodes = {};
  state.model.items.filter(i => i.type === "node").forEach(n => {
    lab.nodes[n.name] = n.cfg || {};
  });

  // kclusters
  lab.kclusters = {};
  state.model.items.filter(i => i.type === "cluster").forEach(c => {
    lab.kclusters[c.name] = c.cfg || {};
  });

  // addons by section
  Object.entries(state.model.addonCfg).forEach(([section, cfg]) => {
    lab[section] = cfg;
  });

  return lab;
}

function downloadLab() {
  const lab = compileLab();
  const json = JSON.stringify(lab, null, 2);
  const blob = new Blob([json], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const a = el("a");
  a.href = url;
  a.download = "lab.json";
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
}

// Render palette
function renderPalette() {
  const root = $("#palette");
  root.innerHTML = "";

  const g = el("div", "pal-group");
  g.appendChild(el("h3", "pal-title", "Lab"));
  const grid = el("div", "pal-grid");

  grid.appendChild(createPaletteItem("node", "VM", "V", "Virtual machine"));
  grid.appendChild(createPaletteItem("cluster", "Cluster", "K", "Kubernetes cluster"));
  g.appendChild(grid);
  root.appendChild(g);

  const ag = el("div", "pal-group");
  ag.appendChild(el("h3", "pal-title", "Add-ons"));
  const ul = el("ul", "component-list");

  state.schemas.addons.forEach(name => {
    const schema = state.schemas.schemas[name] || {};
    const li = el("li", "pal-row");
    li.style.cursor = "pointer";
    li.textContent = schema.title || name;
    li.onclick = () => addItem("addon", name);
    ul.appendChild(li);
  });

  ag.appendChild(ul);
  root.appendChild(ag);
}

function createPaletteItem(type, label, letter, tip) {
  const d = el("div", "pal-cube");
  d.style.cursor = "pointer";
  d.textContent = letter;
  d.title = tip;
  d.onclick = () => addItem(type);
  return d;
}

function addItem(type, comp) {
  const count = state.model.items.filter(i => i.type === type).length + 1;
  const id = type[0] + (++state.model.seq);

  if (type === "node") {
    state.model.items.push({
      id, type, name: "node" + count, cfg: {}, parent: null
    });
  } else if (type === "cluster") {
    state.model.items.push({
      id, type, name: "cluster" + count, cfg: { clu_type: "rke2", clu_rel: "stable" }, parent: null
    });
  } else if (type === "addon") {
    const schema = state.schemas.schemas[comp] || {};
    state.model.items.push({
      id, type, comp, section: schema.section || comp, parent: null
    });
  }

  renderCanvas();
}

function removeItem(id) {
  state.model.items = state.model.items.filter(i => i.id !== id);
  renderCanvas();
}

function renderCanvas() {
  const root = $("#canvas");
  root.innerHTML = "";

  const summary = el("div", "lab-summary");
  summary.appendChild(el("h3", null, `Lab: ${Object.keys(state.model.common).length} settings`));

  const vm = state.model.items.filter(i => i.type === "node").length;
  const cl = state.model.items.filter(i => i.type === "cluster").length;
  const ad = state.model.items.filter(i => i.type === "addon").length;

  summary.appendChild(el("p", null, `VMs: ${vm} | Clusters: ${cl} | Add-ons: ${ad}`));
  root.appendChild(summary);

  // Quick list
  const list = el("ul");
  state.model.items.forEach(item => {
    const li = el("li");
    const span = el("span");
    if (item.type === "node") span.textContent = `VM: ${item.name}`;
    else if (item.type === "cluster") span.textContent = `Cluster: ${item.name}`;
    else span.textContent = `Add-on: ${item.section}`;

    const btn = el("button");
    btn.textContent = "✕";
    btn.onclick = () => removeItem(item.id);

    li.appendChild(span);
    li.appendChild(btn);
    list.appendChild(li);
  });
  root.appendChild(list);
}

// Init
window.addEventListener("DOMContentLoaded", () => {
  renderPalette();
  renderCanvas();

  $("#downloadBtn").addEventListener("click", downloadLab);
  $("#labJson").addEventListener("click", () => {
    $("#output").textContent = JSON.stringify(compileLab(), null, 2);
  });
});
