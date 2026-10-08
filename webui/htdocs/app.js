"use strict";
/*
 * lab-builder frontend.
 *
 * The lab is built on a canvas of 3D cubes (VM, cluster, add-on, common settings):
 * drag them in, double-click to edit. Each editor is the same schema-driven form
 * as before; the canvas only changes how the lab.json is assembled.
 *
 * The renderer knows nothing about any specific script or field. It walks a
 * schema tree (Option B): any object with `name`+`type` is a FIELD, wherever it
 * lives; `fields`/`sections` are structural wrappers that don't appear in output;
 * any other named object is a GROUP that becomes an output key. So new fields,
 * new components and new nested sections all render with zero code changes.
 */
const API = "api";

// ---- schema vocabulary (the only fixed convention) -------------------------
const STRUCTURAL = new Set(["fields", "sections"]);        // containers, not output keys
const META_SCALAR = new Set(["schema_version", "addon", "section",
  "component", "description", "repeatable", "key_label", "name", "type",
  "required", "default"]);

const isField = (o) => o && typeof o === "object" && !Array.isArray(o) &&
  typeof o.name === "string" && typeof o.type === "string";

// ---- state -----------------------------------------------------------------
// commonDefaults: name -> live value typed into a `common.*` field on the base
// topology form. setup_lab.py resolves a per-node field to the node's own
// value if set, else falls back to `common`'s value, else the schema default
// — so once a common field has a value, every other rendered field sharing
// that name (node/kcluster instances, present and future) should show it as
// their *effective* default instead of the schema's hardcoded one. Reset on
// every fresh schema load (selectBase/selectComponent).
const state = {
  components: [], lab: {}, commonDefaults: {},
  // block model behind the canvas (see compileLab); state.lab is compiled from it
  // extra: top-level lab keys the canvas does not model, kept as they are
  model: { common: {}, items: [], addonCfg: {}, extra: {}, seq: 0 },
  sel: "common", editing: null, base: null, schemaCache: {},
  textError: "",   // why the lab.json text does not parse, "" when it does
};

// ---- tiny DOM helpers ------------------------------------------------------
const $ = (s, r = document) => r.querySelector(s);
function el(tag, cls, txt) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (txt != null) e.textContent = txt;
  return e;
}
function toast(msg) {
  const t = $("#toast"); t.textContent = msg; t.hidden = false;
  clearTimeout(toast._t); toast._t = setTimeout(() => (t.hidden = true), 2600);
}

// ---- API -------------------------------------------------------------------
async function apiGet(action, params = {}) {
  const q = new URLSearchParams({ action, ...params });
  const r = await fetch(`${API}?${q}`);
  const j = await r.json();
  if (!r.ok) throw new Error(j.error || r.statusText);
  return j;
}
async function apiPost(action, payload) {
  const r = await fetch(`${API}?action=${action}`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const j = await r.json();
  if (!r.ok) throw new Error(j.error || r.statusText);
  return j;
}

// The login endpoint: saved labs, Save to server, credentials, Create lab. The
// browser asks for the login (HTTP Basic) on the first call. GET without payload.
const ADMIN_API = "admin";
async function adminCall(action, payload, params = {}) {
  const q = new URLSearchParams({ action, ...params });
  const opts = payload === undefined ? {} : {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload),
  };
  const r = await fetch(`${ADMIN_API}?${q}`, opts);
  let j = {};
  try { j = await r.json(); } catch (e) { /* not JSON: an error page */ }
  if (r.status === 401) throw new Error("log in to use this");
  if (!r.ok) throw new Error(j.error || r.statusText);
  return j;
}

// Why the server actions (login endpoint) cannot be used here, or "" when they can.
const HTTPS_ONLY = "You must connect via HTTPS to use this UI";
function serverLock() {
  if (window.LAB_STATIC) return "Available on the lab-builder of your automation node";
  if (location.protocol !== "https:") return HTTPS_ONLY;
  if (state.auth && !state.auth.login_configured) return "Add a login first: run lab-builder-passwd <user> on the automation node";
  return "";
}

// ---- catalogue (palette) ----------------------------------------------------
async function loadComponents() {
  const data = await apiGet("components");
  state.components = data.components;
  $("#countNum").textContent = data.count;
  $("#srcNote").textContent = `read from ${data.scripts_dir}`;
  renderPalette($("#filter").value || "");
}

// What a palette entry creates when it is dropped on the lab. Add-ons are
// discovered at run time (state.components); "__pxe" is the one base-schema
// section that is optional and flat, so it behaves like an add-on cube.
const PXE_SPEC = { type: "addon", comp: "__pxe", title: "pxe" };
let drag = null;   // current drag payload: {from:"palette", spec} | {from:"item", id}

function paletteCube(spec, label, letter, tip) {
  const d = el("div", "pal-cube");
  d.tabIndex = 0; d.draggable = true; d.title = tip + " — drag into the lab, or click to add";
  d.appendChild(makeCube(spec.type, 44, letter));
  d.appendChild(el("span", "pal-label", label));
  d.addEventListener("dragstart", (e) => { drag = { from: "palette", spec }; e.dataTransfer.effectAllowed = "copy"; try { e.dataTransfer.setData("text/plain", label); } catch (x) { /* ignore */ } });
  d.addEventListener("dragend", () => { drag = null; clearOver(); });
  d.addEventListener("click", () => addFromPalette(spec));
  d.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); addFromPalette(spec); } });
  return d;
}

function paletteRow(spec, title, desc, meta, layers, tip) {
  const li = el("li", "pal-row");
  li.tabIndex = 0; li.draggable = true; li.title = tip + ", or click to add";
  const flat = el("span", "flat");
  for (let j = 0; j < 9; j++) { const t = el("span"); t.style.background = CUBE_PAL.addon[CUBE_PAT[j]]; flat.appendChild(t); }
  li.appendChild(flat);
  const txt = el("div", "pal-txt");
  txt.appendChild(el("div", "ci-title", title));
  if (desc) txt.appendChild(el("div", "ci-desc", desc));
  if (meta) txt.appendChild(el("div", "ci-meta", meta));
  if (layers && layers.length) {
    const l = el("div", "ci-layers");
    layers.forEach((x) => l.appendChild(el("span", "layer-badge layer-" + x, x)));
    txt.appendChild(l);
  }
  li.appendChild(txt);
  li.addEventListener("dragstart", (e) => { drag = { from: "palette", spec }; e.dataTransfer.effectAllowed = "copy"; try { e.dataTransfer.setData("text/plain", title); } catch (x) { /* ignore */ } });
  li.addEventListener("dragend", () => { drag = null; clearOver(); });
  li.addEventListener("click", () => addFromPalette(spec));
  li.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); addFromPalette(spec); } });
  return li;
}

// Add-on palette sections, by where an add-on can be dropped (its layers:
// "kubernetes" = on a Kubernetes cluster, "standalone-container"/"os-native" =
// on a VM). `layer` is the badge a section's title already states, so its rows
// do not repeat it.
const ADDON_SECTIONS = [
  { key: "cluster", title: "Kubernetes cluster add-ons", hint: "Drop on a Kubernetes cluster", layer: "kubernetes" },
  { key: "host", title: "VM add-ons", hint: "Drop on a VM" },
  { key: "both", title: "Kubernetes cluster or VM add-ons", hint: "Drop on a Kubernetes cluster or a VM" },
];

// Where an add-on with these layers can be attached: "cluster", "host" or "both".
function addonSection(layers) {
  const onCluster = layers.includes("kubernetes");
  const onHost = layers.some((l) => l !== "kubernetes");
  return onCluster && onHost ? "both" : onHost ? "host" : "cluster";
}

// The layers a row in palette section `key` shows: all but the one its title states.
function shownLayers(layers, key) {
  const s = ADDON_SECTIONS.find((x) => x.key === key);
  return (layers || []).filter((l) => !s || l !== s.layer);
}

function paletteSection(title, hint, rows) {
  const g = el("div", "pal-group");
  g.appendChild(el("h3", "pal-title", title));
  g.appendChild(el("p", "pal-hint", hint));
  const ul = el("ul", "component-list");
  rows.forEach((r) => ul.appendChild(r));
  g.appendChild(ul);
  return g;
}

function renderPalette(filter) {
  const root = $("#palette");
  root.innerHTML = "";
  const f = (filter || "").toLowerCase();
  if (!f) {
    const g = el("div", "pal-group");
    g.appendChild(el("h3", "pal-title", "Lab"));
    const grid = el("div", "pal-grid");
    grid.appendChild(paletteCube({ type: "node" }, "VM", "V", "A virtual machine (or an existing host)"));
    grid.appendChild(paletteCube({ type: "cluster" }, "Kubernetes cluster", "K", "A Kubernetes cluster (RKE2 or K3s)"));
    g.appendChild(grid);
    root.appendChild(g);
  }
  const matches = state.components
    .filter((c) => !f || c.title.toLowerCase().includes(f) || (c.description || "").toLowerCase().includes(f));
  ADDON_SECTIONS.forEach((s) => {
    const rows = matches
      .filter((c) => addonSection(c.layers || []) === s.key)
      .map((c) => paletteRow({ type: "addon", comp: c.name, title: c.title }, c.title,
        c.description, `${c.field_count} option${c.field_count === 1 ? "" : "s"}`, shownLayers(c.layers, s.key), s.hint));
    if (rows.length) root.appendChild(paletteSection(s.title, s.hint, rows));
  });
  const pxe = !f || "pxe tftp dhcp boot service".includes(f);
  if (pxe) {
    root.appendChild(paletteSection("Infrastructure", "Lab-wide services on the automation node",
      [paletteRow(PXE_SPEC, "pxe", "TFTP / PXE-boot / DHCP service on the automation node", "", [],
        "Drop on Common settings")]));
  }
  if (f && !matches.length && !pxe) root.appendChild(el("p", "pal-hint", `No add-on matches “${filter}”.`));
}

// ---- hypervisor status panel (read-only) ------------------------------------
// Populated from the "status" action, which just reads a cached JSON
// snapshot (webui/lib/discovery.py's status()) — the CGI never queries the
// hypervisor itself. Any secret-shaped config value already arrives masked
// as a fixed "********" from the server; this renders whatever it's given
// verbatim, it never tries to unmask or judge anything.
async function loadStatus() {
  const body = $("#statusBody");
  body.innerHTML = "";
  try {
    const s = await apiGet("status");
    if (!s.available) {
      body.appendChild(el("p", "muted",
        "No hypervisor status snapshot yet — refresh_hypervisor_status.py hasn't run yet, or lab_creation.cfg isn't reachable."));
      $("#statusFreshness").textContent = "";
      return;
    }
    $("#statusFreshness").textContent = s.generated_at ? `as of ${s.generated_at}` : "";

    const hosts = s.hosts || [];
    if (hosts.length) {
      const hostsBox = el("div", "status-hosts");
      hosts.forEach((h) => {
        const card = el("div", h.error ? "host-card host-error" : "host-card");
        card.appendChild(el("div", "host-name", h.host));
        if (h.error) {
          card.appendChild(el("div", "host-error-msg", h.error));
        } else {
          card.appendChild(el("div", null, `${h.free_cpu} vCPU free`));
          card.appendChild(el("div", null, `${h.free_mem_mb} MiB RAM free`));
          card.appendChild(el("div", null, `${h.free_disk_mb} MiB disk free`));
        }
        hostsBox.appendChild(card);
      });
      body.appendChild(hostsBox);
    }

    const cfgBox = el("div", "status-config");
    const chip = (k, v) => {
      const c = el("span", "status-chip");
      c.appendChild(el("span", "k", k + ": "));
      c.appendChild(el("span", "v", String(v)));
      return c;
    };
    Object.entries(s.config || {}).forEach(([k, v]) => cfgBox.appendChild(chip(k, v)));
    cfgBox.appendChild(chip("images", (s.images || []).length));
    body.appendChild(cfgBox);

    if (s.error) body.appendChild(el("p", "host-error-msg", s.error));
  } catch (e) {
    body.appendChild(el("p", "muted", "Could not load hypervisor status: " + e.message));
  }
}

// ---- cubes -------------------------------------------------------------------
// Every block is a 3D cube with nine tiles per face, like the logo. The colour
// family says what it is: navy VM, coral cluster, teal add-on, sand common.
const CUBE_PAL = {
  node: ["#1f2f4a", "#12a99d", "#d9c7a3"],
  cluster: ["#e2725b", "#1f2f4a", "#d9c7a3"],
  addon: ["#12a99d", "#1f2f4a", "#d9c7a3"],
  common: ["#d9c7a3", "#12a99d", "#e2725b"],
};
const CUBE_PAT = [0, 1, 0, 2, 0, 1, 0, 2, 0];
const CUBE_FACES = [
  ["rotateY(0deg)", 1], ["rotateY(90deg)", 0.86], ["rotateX(90deg)", 1.1],
  ["rotateY(180deg)", 0.9], ["rotateY(-90deg)", 0.78], ["rotateX(-90deg)", 0.8],
];
function makeCube(kind, size, letter) {
  const pal = CUBE_PAL[kind] || CUBE_PAL.node;
  const wrap = el("span", "cube");
  wrap.style.setProperty("--s", size + "px");
  wrap.setAttribute("aria-hidden", "true");
  const box = el("span", "cube-box");
  CUBE_FACES.forEach((fc, fi) => {
    const face = el("span", "cube-face");
    face.style.transform = fc[0] + " translateZ(calc(var(--s) / 2))";
    face.style.filter = "brightness(" + fc[1] + ")";
    for (let j = 0; j < 9; j++) {
      const t = el("span", "cube-tile");
      t.style.background = pal[CUBE_PAT[(j + fi * 2) % 9]];
      if (fi === 0 && j === 4 && letter) t.textContent = letter;
      face.appendChild(t);
    }
    box.appendChild(face);
  });
  wrap.appendChild(box);
  return wrap;
}

// ---- lab model: blocks -> lab.json ---------------------------------------------
// The canvas edits state.model; state.lab (what Validate/Download/Save/the JSON
// tab/the diagram all read) is always compileLab(state.model). Nothing about
// the saved format changes — only how it is assembled.
//   model.common   : {field: value}                       -> lab.common
//   model.items    : {id,type:"node"|"cluster"|"addon",…}  -> lab.nodes / lab.kclusters / add-on sections
//   model.addonCfg : {section: {field: value}}             -> one shared section per add-on
const HIDDEN_NODE_FIELDS = new Set(["kcluster", "addons"]);   // set by dropping, not typing
const HIDDEN_CLUSTER_FIELDS = new Set(["addons"]);

function cleanObj(o) {
  const r = {};
  Object.keys(o || {}).forEach((k) => {
    const v = o[k];
    if (v === undefined || v === null || v === "" || (Array.isArray(v) && !v.length)) return;
    r[k] = v;
  });
  return r;
}

function compileLab(model) {
  const lab = {};
  const common = cleanObj(model.common);
  if (Object.keys(common).length) lab.common = common;
  const byId = {};
  model.items.forEach((i) => { byId[i.id] = i; });
  // addons[] of item `id`: "<addon>", or {"<addon>": {...}} with that item's own overrides.
  const attached = (id) => {
    const names = [], entries = [];
    model.items.forEach((a) => {
      if (a.type !== "addon" || a.parent !== id || !a.section || names.includes(a.section)) return;
      names.push(a.section);
      entries.push(a.override && Object.keys(a.override).length ? { [a.section]: a.override } : a.section);
    });
    return entries;
  };
  const nodes = {}, clusters = {};
  model.items.forEach((i) => {
    if (i.type === "node" && i.name) {
      const o = cleanObj(i.cfg);
      const c = i.parent && byId[i.parent];
      if (c && c.name) o.kcluster = c.name;
      const ad = attached(i.id);
      if (ad.length) o.addons = ad;
      nodes[i.name] = o;
    } else if (i.type === "cluster" && i.name) {
      const o = cleanObj(i.cfg);
      const ad = attached(i.id);
      if (ad.length) o.addons = ad;
      clusters[i.name] = o;
    }
  });
  if (Object.keys(nodes).length) lab.nodes = nodes;
  if (Object.keys(clusters).length) lab.kclusters = clusters;
  model.items.forEach((a) => {
    if (a.type !== "addon" || !a.section) return;
    const frag = model.addonCfg[a.section] || {};
    if (a.flat === false) Object.assign(lab, frag); else lab[a.section] = frag;
  });
  Object.keys(model.extra || {}).forEach((k) => { if (!(k in lab)) lab[k] = model.extra[k]; });
  return lab;
}

// The add-on component that owns top-level lab section `section`, or undefined.
function componentFor(section) {
  if (section === "pxe") return PXE_SPEC.comp;
  const c = state.components.find((x) => x.title === section) || state.components.find((x) => x.name === section);
  return c && c.name;
}

// The block model for lab definition `lab` (the inverse of compileLab): VMs
// join the Kubernetes cluster their kcluster names, each addons[] entry
// becomes an add-on block (keeping per-VM overrides), add-on sections no VM or
// Kubernetes cluster lists go under Common settings, other keys stay in extra.
function decompileLab(lab) {
  const m = { common: Object.assign({}, lab.common || {}), items: [], addonCfg: {}, extra: {}, seq: 0 };
  const nextId = (type) => type[0] + (++m.seq);
  const addonItem = (entry, parent) => {
    const sec = typeof entry === "string" ? entry : Object.keys(entry || {})[0];
    if (!sec) return;
    const it = { id: nextId("addon"), type: "addon", comp: componentFor(sec), section: sec, flat: true, parent };
    if (typeof entry === "object" && entry[sec] && Object.keys(entry[sec]).length) it.override = entry[sec];
    m.items.push(it);
  };
  Object.keys(lab.kclusters || {}).forEach((name) => {
    const cfg = Object.assign({}, lab.kclusters[name] || {});
    const addons = cfg.addons || [];
    delete cfg.addons;
    const c = { id: nextId("cluster"), type: "cluster", name, cfg, parent: null };
    m.items.push(c);
    addons.forEach((a) => addonItem(a, c.id));
  });
  Object.keys(lab.nodes || {}).forEach((name) => {
    const cfg = Object.assign({}, lab.nodes[name] || {});
    const addons = cfg.addons || [];
    delete cfg.addons;
    const c = m.items.find((i) => i.type === "cluster" && i.name === cfg.kcluster);
    if (c) delete cfg.kcluster;
    const n = { id: nextId("node"), type: "node", name, cfg, parent: c ? c.id : null };
    m.items.push(n);
    addons.forEach((a) => addonItem(a, n.id));
  });
  Object.keys(lab).forEach((k) => {
    if (k === "common" || k === "nodes" || k === "kclusters") return;
    const v = lab[k];
    const comp = componentFor(k);
    if (!comp || !v || typeof v !== "object" || Array.isArray(v)) { m.extra[k] = v; return; }
    m.addonCfg[k] = v;
    if (!m.items.some((i) => i.type === "addon" && i.section === k)) {
      m.items.push({ id: nextId("addon"), type: "addon", comp, section: k, flat: true, parent: null });
    }
  });
  return m;
}

// The lab definition in `text`, JSON or YAML. Throws when it is neither, or not a mapping.
function parseLab(text) {
  let lab;
  try { lab = JSON.parse(text); } catch (e) {
    if (!window.jsyaml || /^\s*[{[]/.test(text)) throw e;
    lab = window.jsyaml.load(text);
  }
  if (!lab || typeof lab !== "object" || Array.isArray(lab)) throw new Error("a lab definition is a JSON or YAML object");
  return lab;
}

function getPath(obj, path) {
  let o = obj;
  for (const k of path) { if (o == null) return undefined; o = o[k]; }
  return o;
}

// "needs input" markers: required fields with no value and no schema default.
function missingRequired(item) {
  const secs = state.base && state.base.sections;
  if (!secs) return 0;
  const m = state.model;
  const miss = (fields, cfg) => (fields || []).filter((f) => f.required && (f.default === undefined || f.default === "")
    && (cfg[f.name] === undefined || cfg[f.name] === "")).length;
  if (item === "common") return miss(secs.common.fields, m.common);
  if (item.type === "node") return miss(secs.nodes.fields, item.cfg) + (item.name ? 0 : 1);
  if (item.type === "cluster") return miss(secs.kclusters.fields, item.cfg) + (item.name ? 0 : 1);
  const sc = state.schemaCache[item.comp];
  return sc ? miss(sc.fields, m.addonCfg[item.section] || {}) : 0;
}

// Backends whose VMs get their IP from the provider, so `myip` stays empty.
const LOCAL_BACKENDS = new Set(["", "libvirt", "harvester"]);

// What is wrong with the lab in `model`: { errors, warnings }, each a list of
// sentences. Errors block Download, Save and Create; warnings do not. Checks
// that need a schema (state.base, state.schemaCache) are skipped until it is loaded.
function lintLab(model) {
  const errors = [], warnings = [];
  const items = model.items;
  const byId = {};
  items.forEach((i) => { byId[i.id] = i; });
  const nodes = items.filter((i) => i.type === "node");
  const clusters = items.filter((i) => i.type === "cluster");
  const addons = items.filter((i) => i.type === "addon");
  const has = (o, k) => o && o[k] !== undefined && o[k] !== null && o[k] !== "";
  const label = (a) => a.section || a.comp;

  clusters.forEach((c) => {
    if (!nodes.some((n) => n.parent === c.id)) errors.push("Kubernetes cluster " + c.name + " has no VM.");
  });
  const clusterNames = new Set(clusters.map((c) => c.name));
  nodes.forEach((n) => {
    if (!n.parent && has(n.cfg, "kcluster") && !clusterNames.has(n.cfg.kcluster)) {
      errors.push("VM " + n.name + " is in Kubernetes cluster " + n.cfg.kcluster + ", which the lab does not define.");
    }
  });

  const secs = state.base && state.base.sections;
  if (secs) {
    const aliases = { SOURCE_IMAGE: ["ISO_IMAGE"] };
    const set = (o, k) => has(o, k) || (aliases[k] || []).some((a) => has(o, a));
    secs.common.fields.filter((f) => f.required && (f.default === undefined || f.default === "")).forEach((f) => {
      if (set(model.common, f.name)) return;
      const without = nodes.filter((n) => !n.cfg.existing && !set(n.cfg, f.name)).map((n) => n.name);
      if (without.length) errors.push("Common setting " + f.name + " is not set, and VM " + without.join(", ") + " has no value of its own.");
    });
    nodes.forEach((n) => {
      const backend = has(n.cfg, "backend") ? n.cfg.backend : (model.common.backend || "");
      if (!has(n.cfg, "myip") && LOCAL_BACKENDS.has(backend)) errors.push("VM " + n.name + " has no IP address (myip).");
    });
    clusters.forEach((c) => {
      secs.kclusters.fields.filter((f) => f.required && !has(c.cfg, f.name)).forEach((f) => {
        errors.push("Kubernetes cluster " + c.name + ": " + f.name + " is not set.");
      });
    });
  }

  const seen = {};
  nodes.forEach((n) => {
    ["myip", "mymac"].forEach((k) => {
      if (!has(n.cfg, k)) return;
      const key = k + "=" + String(n.cfg[k]).toLowerCase();
      if (seen[key]) errors.push("VM " + seen[key] + " and VM " + n.name + " have the same " + k + " " + n.cfg[k] + ".");
      else seen[key] = n.name;
    });
  });

  const unknown = new Set();
  addons.forEach((a) => {
    if (!a.comp) { unknown.add(label(a)); return; }
    const t = a.parent && byId[a.parent];
    const where = addonPlacement(a.comp);
    if (t && (where === "common" || (where === "host" && t.type === "cluster") || (where === "cluster" && t.type === "node"))) {
      errors.push(label(a) + " cannot be attached to " + (t.type === "node" ? "VM " : "Kubernetes cluster ") + t.name + "."
        + (where === "common" ? " It is lab-wide." : where === "host" ? " It installs on a VM." : " It installs on a Kubernetes cluster."));
    }
  });
  unknown.forEach((n) => errors.push("Add-on " + n + " is not installed on this lab-builder."));

  const sections = new Set(addons.map(label));
  sections.forEach((sec) => {
    const mine = addons.filter((a) => label(a) === sec);
    const sc = mine[0].comp && state.schemaCache[mine[0].comp];
    if (sc) {
      const cfg = model.addonCfg[sec] || {};
      (sc.fields || []).filter((f) => f.required && (f.default === undefined || f.default === "") && !has(cfg, f.name))
        .forEach((f) => errors.push("Add-on " + sec + ": " + f.name + " is not set."));
    }
    if (addonPlacement(mine[0].comp) !== "common" && mine.every((a) => !a.parent)) {
      warnings.push("Add-on " + sec + " has settings, but no VM or Kubernetes cluster lists it.");
    }
  });
  return { errors, warnings };
}

async function loadBase() {
  state.base = await apiGet("base");
  return state.base;
}
async function loadAddonSchema(comp) {
  let sc;
  if (comp === "__pxe") {
    const base = await loadBase();
    sc = { section: "pxe", description: base.sections.pxe.description, fields: base.sections.pxe.fields };
  } else {
    sc = await apiGet("schema", { name: comp });
  }
  state.schemaCache[comp] = sc;
  return sc;
}

async function createItem(spec) {
  const m = state.model;
  const count = m.items.filter((i) => i.type === spec.type).length + 1;
  const id = spec.type[0] + (++m.seq);
  if (spec.type === "node") {
    const dom = m.common.mydomain;
    return { id, type: "node", name: "node" + count + (dom ? "." + dom : ""), cfg: {}, parent: null };
  }
  if (spec.type === "cluster") {
    return { id, type: "cluster", name: "cluster" + count, parent: null,
      cfg: cleanObj({ clu_type: "rke2", clu_rel: "stable", mydomain: m.common.mydomain }) };
  }
  const sc = await loadAddonSchema(spec.comp);
  return { id, type: "addon", comp: spec.comp, section: sc.section || sc.component || spec.comp,
    flat: Array.isArray(sc.fields) && !!sc.section, parent: null };
}

// Where an add-on can be attached: "common" (Infrastructure: Common settings
// only), "cluster", "host" (a VM) or "both", from its layers.
function addonPlacement(comp) {
  if (comp === PXE_SPEC.comp) return "common";
  const c = state.components.find((x) => x.name === comp);
  return c ? addonSection(c.layers || []) : "both";
}

// The parent a block gets when dropped on `targetId` ("common", an item id, or
// null for the empty canvas): { parent } (null = top level, for an add-on its
// settings only, under Common settings), or { error } when it may not go there.
// VMs join a Kubernetes cluster; Kubernetes clusters always sit at the top level.
function dropParent(item, targetId) {
  const items = state.model.items;
  let t = targetId && targetId !== "common" ? items.find((i) => i.id === targetId) : null;
  if (t && t.id === item.id) t = null;
  if (item.type === "cluster") return { parent: null };
  if (item.type === "node") return { parent: t ? (t.type === "cluster" ? t.id : t.parent || null) : null };
  if (t && t.type === "addon") t = t.parent ? items.find((i) => i.id === t.parent) : null;
  if (!t) return { parent: null };
  const where = addonPlacement(item.comp);
  const name = item.section || item.title || item.comp;
  if (where === "common") return { error: name + " is lab-wide: drop it on Common settings" };
  if (t.type === "cluster") {
    return where === "host" ? { error: name + " installs on a VM: drop it on a VM" } : { parent: t.id };
  }
  if (where !== "cluster") return { parent: t.id };
  return t.parent ? { parent: t.parent } : { error: name + " installs on a Kubernetes cluster: drop it on a Kubernetes cluster" };
}

// The block a drag payload creates or moves, as dropParent() needs it.
function dragItem(d) {
  if (!d) return null;
  return d.from === "palette" ? { type: d.spec.type, comp: d.spec.comp, title: d.spec.title }
    : state.model.items.find((i) => i.id === d.id);
}

async function handleDrop(targetId) {
  const d = drag; drag = null; clearOver();
  if (!d) return;
  const m = state.model;
  const where = dropParent(dragItem(d) || {}, targetId);
  if (where.error) { toast(where.error); return; }
  let item;
  try {
    if (d.from === "palette") { item = await createItem(d.spec); m.items.push(item); }
    else item = m.items.find((i) => i.id === d.id);
  } catch (e) { toast("Could not add: " + e.message); return; }
  if (!item) return;
  item.parent = where.parent;
  state.sel = item.id;
  renderCanvas();
  if (window.CubeFX) CubeFX.land(item);
  refreshLab();
}

function addFromPalette(spec) {
  const sel = state.model.items.find((i) => i.id === state.sel);
  drag = { from: "palette", spec };
  return handleDrop(sel && sel.type !== "addon" ? sel.id : null);
}

function removeItem(id) {
  const m = state.model;
  m.items = m.items.filter((i) => i.id !== id && !(i.type === "addon" && i.parent === id));
  m.items.forEach((i) => { if (i.type === "node" && i.parent === id) i.parent = null; });
  if (state.sel === id) state.sel = "common";
  renderCanvas();
  refreshLab();
}

// ---- canvas ------------------------------------------------------------------
function clearOver() { document.querySelectorAll(".drop-over").forEach((x) => x.classList.remove("drop-over")); }
function wireDrop(elm, targetId) {
  elm.addEventListener("dragover", (e) => {
    if (!drag || dropParent(dragItem(drag) || {}, targetId).error) return;
    e.preventDefault(); e.stopPropagation();
    if (!elm.classList.contains("drop-over")) { clearOver(); elm.classList.add("drop-over"); }
  });
  elm.addEventListener("drop", (e) => { e.preventDefault(); e.stopPropagation(); handleDrop(targetId); });
}
function selectBlock(id) {
  state.sel = id;
  document.querySelectorAll("#canvas .block").forEach((b) => b.classList.toggle("selected", b.dataset.id === id));
}

const BLOCK_SIZE = { common: 64, cluster: 60, node: 50, addon: 36, mini: 24 };

function blockEl(item, mini) {
  const type = item === "common" ? "common" : item.type;
  const id = item === "common" ? "common" : item.id;
  const b = el("div", "block block-" + type + (mini ? " block-mini" : ""));
  b.dataset.id = id;
  b.tabIndex = 0;
  b.draggable = type !== "common" && type !== "cluster";
  let name, sub, letter;
  if (type === "common") {
    const c = state.model.common;
    name = "Common settings";
    const bits = [];
    if (c.VM_MEM) bits.push(c.VM_MEM + " MiB");
    if (c.VM_CPU) bits.push(c.VM_CPU + " vCPU");
    if (c.VM_DSK) bits.push(c.VM_DSK + " GiB");
    if (c.ISO_IMAGE) bits.push(c.ISO_IMAGE);
    sub = bits.length ? bits.join(" · ") : "defaults for every VM — double-click to set";
    letter = "C";
  } else if (type === "node") {
    name = item.name || "unnamed VM"; sub = item.cfg.myip || "no IP yet"; letter = "V";
  } else if (type === "cluster") {
    name = item.name || "unnamed Kubernetes cluster"; sub = item.cfg.clu_type || "Kubernetes cluster"; letter = "K";
  } else {
    name = item.section; sub = ""; letter = String(item.section).charAt(0).toUpperCase();
  }
  b.appendChild(makeCube(type, mini ? BLOCK_SIZE.mini : BLOCK_SIZE[type], mini ? "" : letter));
  const txt = el("div", "block-txt");
  txt.appendChild(el("div", "block-name", name));
  if (sub && !mini) txt.appendChild(el("div", "block-sub", sub));
  b.appendChild(txt);
  const miss = missingRequired(item);
  if (miss) { const w = el("span", "block-warn"); w.title = miss + " required field" + (miss === 1 ? "" : "s") + " still empty"; b.appendChild(w); }
  b.title = type === "common" ? "Double-click to edit" : "Double-click to edit · drag to move · Delete to remove";
  b.addEventListener("click", (e) => { e.stopPropagation(); selectBlock(id); });
  b.addEventListener("dblclick", (e) => { e.stopPropagation(); openEditor(id); });
  b.addEventListener("keydown", (e) => {
    if (e.target !== b) return;
    if (e.key === "Enter") { e.preventDefault(); openEditor(id); }
    else if ((e.key === "Delete" || e.key === "Backspace") && type !== "common") { e.preventDefault(); removeItem(id); }
  });
  if (b.draggable) {
    b.addEventListener("dragstart", (e) => { e.stopPropagation(); drag = { from: "item", id }; e.dataTransfer.effectAllowed = "move"; try { e.dataTransfer.setData("text/plain", name); } catch (x) { /* ignore */ } });
    b.addEventListener("dragend", () => { drag = null; clearOver(); });
  }
  if (type === "node" || type === "common") wireDrop(b, id);
  if (id === state.sel) b.classList.add("selected");
  return b;
}

function nodeBlock(n) {
  const b = blockEl(n);
  const kids = state.model.items.filter((a) => a.type === "addon" && a.parent === n.id);
  if (kids.length) {
    const row = el("div", "mini-addons");
    kids.forEach((a) => row.appendChild(blockEl(a, true)));
    b.appendChild(row);
  }
  return b;
}

function renderCanvas() {
  const root = $("#canvas");
  if (!root) return;
  const m = state.model;
  root.innerHTML = "";
  const common = el("div", "common-area");
  common.appendChild(blockEl("common"));
  const settings = m.items.filter((a) => a.type === "addon" && !a.parent);
  if (settings.length) {
    const row = el("div", "common-addons");
    row.appendChild(el("span", "dz-label", "Add-on settings"));
    settings.forEach((a) => row.appendChild(blockEl(a)));
    common.appendChild(row);
  }
  root.appendChild(common);

  const zone = el("div", "dropzone");
  wireDrop(zone, null);
  root.appendChild(zone);
  if (!m.items.length) zone.appendChild(el("p", "empty-state", "Drag a VM, a Kubernetes cluster or an add-on here"));

  m.items.filter((i) => i.type === "cluster").forEach((c) => {
    const tray = el("div", "tray");
    wireDrop(tray, c.id);
    const body = el("div", "tray-body");
    body.appendChild(blockEl(c));
    const nodes = el("div", "tray-nodes");
    m.items.filter((n) => n.type === "node" && n.parent === c.id).forEach((n) => nodes.appendChild(nodeBlock(n)));
    body.appendChild(nodes);
    tray.appendChild(body);
    const ads = el("div", "tray-addons");
    ads.appendChild(el("span", "dz-label", "Kubernetes cluster add-ons"));
    const mine = m.items.filter((a) => a.type === "addon" && a.parent === c.id);
    mine.forEach((a) => ads.appendChild(blockEl(a)));
    if (!mine.length) ads.appendChild(el("span", "block-sub", "drop add-ons here"));
    tray.appendChild(ads);
    zone.appendChild(tray);
  });

  const loose = m.items.filter((i) => i.type === "node" && !i.parent);
  if (loose.length) {
    const wrap = el("div", "loose");
    wrap.appendChild(el("span", "dz-label", "Standalone VMs"));
    const row = el("div", "loose-row");
    loose.forEach((i) => row.appendChild(nodeBlock(i)));
    wrap.appendChild(row);
    zone.appendChild(wrap);
  }
}

// ---- editor (double-click) ------------------------------------------------------
// Reuses the schema-driven renderer unchanged: walk()/fieldRow()/instanceBox()
// build the form, serializeForm()/readWidget() read it back.
function setWidgetValue(input, val) {
  if (val === undefined || val === null) return;
  const s = Array.isArray(val) ? val.join(",") : String(val);
  if (input.tagName === "SELECT") {
    if (!Array.from(input.options).some((o) => o.value === s)) { const o = el("option", null, s); o.value = s; input.appendChild(o); }
    input.dataset.userTouched = "1";
  }
  input.value = s;
}

function readInstance(inst) {
  const key = inst.querySelector(".inst-key").value.trim();
  const obj = {};
  inst.querySelectorAll("[data-field]").forEach((elm) => {
    const v = readWidget(elm);
    if (v === undefined || (Array.isArray(v) && !v.length)) return;
    obj[elm.dataset.field] = v;
  });
  return { key, obj };
}

async function openEditor(id) {
  selectBlock(id);
  const m = state.model;
  const item = id === "common" ? null : m.items.find((i) => i.id === id);
  if (id !== "common" && !item) return;
  const form = $("#schemaForm");
  let kind, title, desc = "", layers = [];
  try {
    const base = await loadBase();   // fresh each time: ISO_IMAGE choices follow the hypervisor
    const secs = base.sections;
    form.innerHTML = "";
    state.commonDefaults = {};
    Object.keys(m.common).forEach((k) => { if (m.common[k] !== "" && m.common[k] != null) state.commonDefaults[k] = String(m.common[k]); });
    if (id === "common") {
      kind = "Lab · defaults"; title = "Common settings"; desc = secs.common.description;
      walk({ fields: secs.common.fields }, form, ["common"]);
      form.querySelectorAll("[data-outpath]").forEach((i) => setWidgetValue(i, getPath({ common: m.common }, JSON.parse(i.dataset.outpath))));
    } else if (item.type === "node" || item.type === "cluster") {
      const spec = item.type === "node" ? secs.nodes : secs.kclusters;
      const hidden = item.type === "node" ? HIDDEN_NODE_FIELDS : HIDDEN_CLUSTER_FIELDS;
      kind = item.type === "node" ? "VM" : "Kubernetes cluster"; title = item.name; desc = spec.description;
      const inst = instanceBox(spec.fields.filter((f) => !hidden.has(f.name)), spec.key_label || "name");
      form.appendChild(inst);
      inst.querySelector(".inst-key").value = item.name;
      inst.querySelectorAll("[data-field]").forEach((i) => setWidgetValue(i, item.cfg[i.dataset.field]));
    } else {
      const sc = await loadAddonSchema(item.comp);
      kind = "Add-on"; title = item.section; desc = sc.description || "";
      layers = (sc.capabilities && sc.capabilities.layers) || [];
      walk(sc, form, []);
      const frag = m.addonCfg[item.section] || {};
      form.querySelectorAll("[data-outpath]").forEach((i) => setWidgetValue(i, getPath(frag, JSON.parse(i.dataset.outpath))));
    }
  } catch (e) { toast("Load error: " + e.message); return; }
  state.editing = id;
  $("#editorKind").textContent = kind;
  $("#editorTitle").textContent = title;
  $("#editorDesc").textContent = desc || "";
  const lay = $("#formLayers");
  lay.innerHTML = "";
  layers.forEach((l) => lay.appendChild(el("span", "layer-badge layer-" + l, l)));
  $("#editorRemove").hidden = id === "common";
  const dlg = $("#editor");
  if (typeof dlg.showModal === "function") { if (!dlg.open) dlg.showModal(); } else dlg.setAttribute("open", "");
}

function closeEditor() {
  const dlg = $("#editor");
  state.editing = null;
  if (typeof dlg.close === "function") dlg.close(); else dlg.removeAttribute("open");
}

function applyEditor() {
  const id = state.editing;
  if (!id) return;
  const m = state.model, form = $("#schemaForm");
  if (id === "common") {
    m.common = serializeForm().common || {};
  } else {
    const item = m.items.find((i) => i.id === id);
    if (!item) return closeEditor();
    if (item.type === "node" || item.type === "cluster") {
      const r = readInstance(form.querySelector(".instance"));
      if (!r.key) { toast("A name is required."); return; }
      if (m.items.some((o) => o !== item && o.type === item.type && o.name === r.key)) { toast("“" + r.key + "” is already used."); return; }
      item.name = r.key; item.cfg = r.obj;
    } else {
      m.addonCfg[item.section] = serializeForm();
    }
  }
  closeEditor();
  renderCanvas();
  refreshLab();
}

/* Recursively render `node` into `parent`, tracking the output-key path. */
function walk(node, parent, outPath) {
  if (Array.isArray(node)) { node.forEach((n) => walk(n, parent, outPath)); return; }
  if (!node || typeof node !== "object") return;
  if (isField(node)) { if (!node.hidden) parent.appendChild(fieldRow(node, outPath)); return; }

  for (const [key, val] of Object.entries(node)) {
    if (!val || typeof val !== "object") continue;      // skip scalar meta
    if (STRUCTURAL.has(key)) { walk(val, parent, outPath); continue; }  // wrapper: no output key
    if (val.repeatable === true) {                                      // keyed map of instances
      parent.appendChild(repeatGroup(key, val, outPath.concat(key)));
      continue;
    }
    // named group -> heading + new output segment
    const box = el("div", "group");
    const head = el("div", "group-head", key);
    if (typeof val.description === "string") head.appendChild(el("span", "gh-desc", val.description));
    box.appendChild(head);
    const body = el("div", "group-body");
    box.appendChild(body);
    parent.appendChild(box);
    walk(val, body, outPath.concat(key));
  }
}

function fieldRow(field, outPath, repeatMode) {
  const row = el("div", field.required ? "field field-required" : "field field-optional");
  const label = el("label");
  label.appendChild(document.createTextNode(field.name));
  if (field.required) label.appendChild(el("span", "req", "*"));
  label.appendChild(el("span", "type-tag", field.type));
  row.appendChild(label);
  if (field.description) row.appendChild(el("div", "desc", field.description));

  // A field with a fixed set of options (boolean, or an explicit `enum` list
  // from the schema) gets a dropdown instead of free text, with whichever
  // option matches the *effective* default (see applyFieldDefault) visually
  // marked. `enum` entries may be plain strings or {value, label} objects,
  // for cases (like an empty-string default) where the raw value alone isn't
  // a readable label.
  const options = field.type === "boolean" ? ["true", "false"]
                : Array.isArray(field.enum) && field.enum.length ? field.enum
                : null;

  let input;
  // Special hybrid control for SOURCE_IMAGE: dropdown of common images + custom URL input
  if (field.name === "SOURCE_IMAGE") {
    const container = el("div", "source-image-container");

    // Select dropdown with common images
    const select = el("select");
    select.className = "source-image-select";

    const commonImages = [
      { label: "— Select or enter custom URL —", value: "" },
      { label: "SL-Micro 6.1 (QCOW2)", value: "SL-Micro.x86_64-6.1-Default-qcow-GM.qcow2" },
      { label: "SLES 15 SP6", value: "SLES-15-SP6-for-SAP-Applications.x86_64-cloud.qcow2" },
      { label: "openSUSE Leap 15.6", value: "openSUSE-Leap-15.6.x86_64.qcow2" },
      { label: "Ubuntu 24.04 LTS", value: "ubuntu-24.04-cloud-amd64.qcow2" },
      { label: "— Enter custom URL —", value: "CUSTOM_URL" }
    ];

    commonImages.forEach((img) => {
      const opt = el("option", null, img.label);
      opt.value = img.value;
      select.appendChild(opt);
    });

    // Text input for custom URL or local path
    const textInput = el("input");
    textInput.type = "text";
    textInput.className = "source-image-custom";
    textInput.placeholder = "URL (http://, https://, ftp://) or local file path";
    textInput.style.display = "none";

    select.addEventListener("change", () => {
      if (select.value === "CUSTOM_URL") {
        textInput.style.display = "block";
        textInput.focus();
      } else if (select.value === "") {
        textInput.style.display = "none";
        textInput.value = "";
      } else {
        textInput.style.display = "none";
        textInput.value = select.value;
      }
    });

    textInput.addEventListener("input", () => {
      // Update hidden field value when user types in custom URL
      select.value = "CUSTOM_URL";
    });

    container.appendChild(select);
    container.appendChild(textInput);

    // Create a hidden input field that tracks the actual value
    input = el("input");
    input.type = "hidden";
    input.className = "source-image-value";

    // Sync dropdown and text input to the hidden field
    const syncValue = () => {
      if (select.value === "CUSTOM_URL" || select.value === "") {
        input.value = textInput.value;
      } else {
        input.value = select.value;
      }
    };

    select.addEventListener("change", syncValue);
    textInput.addEventListener("input", syncValue);

    row.appendChild(container);
    // Don't append input here; let the normal flow handle it after initialization
  } else if (options) {
    const optValue = (o) => (o && typeof o === "object") ? o.value : o;
    const optLabel = (o) => (o && typeof o === "object") ? o.label : o;
    input = el("select");
    if (!options.some((o) => optValue(o) === "")) {
      const blank = el("option", null, "— unset —");
      blank.value = "";
      blank.dataset.blank = "1";
      input.appendChild(blank);
    }
    options.forEach((o) => {
      const v = optValue(o);
      const isObj = o && typeof o === "object";
      const opt = el("option", null, optLabel(o));
      opt.value = v;
      opt.dataset.label = optLabel(o);
      opt.dataset.fixedLabel = isObj ? "1" : "";   // objects carry their own label; never append "(default)"
      input.appendChild(opt);
    });
    // Once the user has actually picked something, live common-default
    // cascades must not silently switch their selection back — only the
    // "(default)" marking/bolding keeps updating.
    input.addEventListener("change", () => { input.dataset.userTouched = "1"; });
  } else {
    switch (field.type) {
      case "integer": case "port": input = el("input"); input.type = "number"; break;
      case "password": input = el("input"); input.type = "password"; break;
      case "array": input = el("input"); input.type = "text"; break;
      default: input = el("input"); input.type = "text";
    }
  }
  input._field = field;
  if (repeatMode) input.dataset.repeat = "1";
  applyFieldDefault(input, state.commonDefaults[field.name]);

  if (repeatMode) input.dataset.field = field.name;         // serialized per-instance
  else input.dataset.outpath = JSON.stringify(outPath.concat(field.name));
  input.dataset.ftype = field.type;
  row.appendChild(input);

  // Fields under `common` feed their live value forward as the effective
  // default for every other rendered field of the same name (nodes/kclusters
  // instances, present and future) — see state.commonDefaults above.
  if (!repeatMode && outPath[0] === "common") {
    const cascade = () => {
      const v = input.value;
      if (v === "") delete state.commonDefaults[field.name];
      else state.commonDefaults[field.name] = v;
      applyLiveDefault(field.name);
    };
    input.addEventListener("input", cascade);
    input.addEventListener("change", cascade);
  }

  // Schema-driven live validation — the schema is the source of truth for
  // what's correct (pattern/min/max/enum), not hand-coded per-field rules
  // here. A field left empty is never flagged (an empty optional field is
  // fine, and flagging an empty required one before the user has had a
  // chance to fill it in would just be naggy — required-ness is already
  // shown via the "*" marker, and unfilled requireds still get caught by
  // the server-side Validate button).
  if (!options) {
    const errBox = el("div", "field-error");
    errBox.hidden = true;
    row.appendChild(errBox);
    const check = () => {
      const msg = validateFieldValue(field, input.value);
      input.classList.toggle("invalid", !!msg);
      errBox.hidden = !msg;
      errBox.textContent = msg || "";
    };
    input.addEventListener("input", check);
    input.addEventListener("blur", check);
  }

  return row;
}

/* Re-render `input`'s placeholder (plain input) or default-marked option
 * (select) using `liveVal` if set, else the field's own schema default —
 * this is the single place that decides what "the effective default" looks
 * like, used both at field-creation time and when a common value cascades
 * into already-rendered fields. Never touches the user's actual typed value
 * (placeholders don't override input.value; a select's value is only synced
 * to a new default before the user has ever changed it themselves).
 *
 * Repeat-group instance fields (nodes/kclusters, dataset.repeat) are the one
 * exception: their select's OWN .value is never auto-set to the default,
 * even before the user touches it — only the option relabeling runs. A
 * flat/common select auto-filling its default is harmless (it's the exact
 * value setup_lab.py would have used anyway), but for a per-node override
 * field it silently wrote an explicit value into the saved lab.json for
 * every instance the user never actually configured (reported live
 * 2026-09-01: INSTALL_RKE2_TYPE always present, "server", regardless of
 * whether that node's role was ever actually selected) — leaving it unset
 * lets the node genuinely inherit common/the addon's own default instead,
 * matching how a plain text field's placeholder-only default already works. */
function applyFieldDefault(input, liveVal) {
  const field = input._field;
  const hasDefault = liveVal != null || field.default != null;
  const def = hasDefault ? String(liveVal != null ? liveVal : field.default) : "";

  if (input.tagName === "SELECT") {
    Array.from(input.options).forEach((opt) => {
      if (opt.dataset.blank === "1") return;
      const isDefault = hasDefault && opt.value === def;
      const fixedLabel = opt.dataset.fixedLabel === "1";
      opt.textContent = (!fixedLabel && isDefault) ? `${opt.dataset.label} (default)` : opt.dataset.label;
      opt.className = isDefault ? "opt-default" : "";
    });
    if (!input.dataset.userTouched && input.dataset.repeat !== "1") {
      input.value = Array.from(input.options).some((o) => o.value === def) ? def : "";
    }
    return;
  }

  switch (field.type) {
    case "password": input.placeholder = def ? "default set" : ""; break;
    case "array": input.placeholder = def || "comma,separated,values"; break;
    default: input.placeholder = def;
  }
}

/* A common.* field's value changed — push it as the new effective default
 * onto every other currently-rendered field sharing that name: repeat-group
 * instances (nodes/kclusters, tagged data-field) and any other flat field
 * (tagged data-outpath), common's own field excluded. */
function applyLiveDefault(name) {
  const liveVal = state.commonDefaults[name];
  document.querySelectorAll(`#schemaForm [data-field="${name}"]`).forEach((elm) => {
    applyFieldDefault(elm, liveVal);
  });
  document.querySelectorAll("#schemaForm [data-outpath]").forEach((elm) => {
    const path = JSON.parse(elm.dataset.outpath);
    if (path[0] === "common") return;
    if (path[path.length - 1] !== name) return;
    applyFieldDefault(elm, liveVal);
  });
}

/* Schema-driven validation for one field's current (string) value. Returns
 * an error message, or null if the value is fine (or empty — see fieldRow's
 * caller for why empty is never flagged here). Every rule comes from the
 * schema itself (type/pattern/min/max) — nothing here is hard-coded to a
 * specific field name, matching this renderer's "no per-script knowledge"
 * design principle. */
function validateFieldValue(field, value) {
  if (value === "" || value == null) return null;

  if (field.type === "integer" || field.type === "port") {
    if (!/^-?\d+$/.test(value)) return "must be a whole number";
    const n = parseInt(value, 10);
    const lo = field.type === "port" ? 1 : field.min;
    const hi = field.type === "port" ? 65535 : field.max;
    if (lo != null && n < lo) return `must be ${lo} or more`;
    if (hi != null && n > hi) return `must be ${hi} or less`;
    return null;
  }

  if (field.pattern) {
    try {
      if (!new RegExp(field.pattern).test(value)) return "doesn't match the expected format";
    } catch (e) { /* malformed pattern from the schema — don't block on it */ }
  }

  return null;
}

/* Gather every field under a node, wherever it lives (container-agnostic). */
function collectFields(node) {
  let out = [];
  if (Array.isArray(node)) node.forEach((n) => { out = out.concat(collectFields(n)); });
  else if (node && typeof node === "object") {
    if (isField(node)) return [node];
    for (const v of Object.values(node)) out = out.concat(collectFields(v));
  }
  return out;
}

/* A repeatable group -> a keyed map of instances (e.g. nodes, kclusters). */
function repeatGroup(name, obj, outPath) {
  const box = el("div", "group repeat");
  box.dataset.repeatpath = JSON.stringify(outPath);
  const head = el("div", "group-head", name);
  if (typeof obj.description === "string") head.appendChild(el("span", "gh-desc", obj.description));
  box.appendChild(head);
  const body = el("div", "group-body");
  const list = el("div", "instances");
  body.appendChild(list);
  const fields = collectFields(obj);
  const keyLabel = obj.key_label || "key";
  const add = el("button", "btn add-inst", "+ add " + name);
  add.type = "button";
  add.addEventListener("click", () => list.appendChild(instanceBox(fields, keyLabel)));
  body.appendChild(add);
  box.appendChild(body);
  list.appendChild(instanceBox(fields, keyLabel));   // start with one
  return box;
}

function instanceBox(fields, keyLabel) {
  const inst = el("div", "instance");
  const keyRow = el("div", "field field-required");   // the key itself is always required
  const kl = el("label");
  kl.appendChild(document.createTextNode(keyLabel));
  kl.appendChild(el("span", "req", "*"));
  keyRow.appendChild(kl);
  const ki = el("input", "inst-key");
  ki.type = "text"; ki.placeholder = keyLabel;
  keyRow.appendChild(ki);
  inst.appendChild(keyRow);
  fields.forEach((f) => inst.appendChild(fieldRow(f, [], true)));
  const rm = el("button", "btn rm-inst", "remove");
  rm.type = "button";
  rm.addEventListener("click", () => inst.remove());
  inst.appendChild(rm);
  return inst;
}

// ---- serialize form -> fragment -------------------------------------------
function readWidget(elm) {
  const raw = elm.value.trim();
  if (raw === "") return undefined;
  switch (elm.dataset.ftype) {
    case "boolean": return raw === "true";
    case "integer": case "port": { const n = Number(raw); return Number.isNaN(n) ? raw : n; }
    case "array": return raw.split(",").map((s) => s.trim()).filter(Boolean);
    default: return raw;
  }
}
function setPath(obj, path, val) {
  let o = obj;
  for (let i = 0; i < path.length - 1; i++) o = (o[path[i]] ??= {});
  o[path[path.length - 1]] = val;
}
function serializeForm() {
  const frag = {};
  // plain fields (skip those inside repeatable instances)
  $("#schemaForm").querySelectorAll("[data-outpath]").forEach((elm) => {
    if (elm.closest(".instance")) return;
    const val = readWidget(elm);
    if (val === undefined || (Array.isArray(val) && !val.length)) return;
    setPath(frag, JSON.parse(elm.dataset.outpath), val);
  });
  // repeatable groups -> { groupKey: { instanceKey: {…} } }
  $("#schemaForm").querySelectorAll(".repeat").forEach((rep) => {
    const map = {};
    rep.querySelectorAll(".instance").forEach((inst) => {
      const key = inst.querySelector(".inst-key").value.trim();
      if (!key) return;
      const obj = {};
      inst.querySelectorAll("[data-field]").forEach((elm) => {
        const val = readWidget(elm);
        if (val === undefined || (Array.isArray(val) && !val.length)) return;
        obj[elm.dataset.field] = val;
      });
      map[key] = obj;
    });
    if (Object.keys(map).length) setPath(frag, JSON.parse(rep.dataset.repeatpath), map);
  });
  return frag;
}

// ---- architecture diagram ----------------------------------------------------
// A live preview of state.lab's topology (nodes, their kcluster membership,
// addons), rendered with mermaid.js — vendored (webui/htdocs/vendor/), not
// CDN-loaded, so it keeps working on a network-restricted training/demo
// host. Rebuilt from scratch on every refreshLab() call; this renderer knows
// as little about specific field names as the rest of app.js does — it only
// reads the few structural keys (nodes/kclusters/kcluster/addons/existing/
// myip) that every lab.json already shares, the same "no per-script
// knowledge" principle the form renderer above follows.
let mermaidReady = false;
function initMermaid() {
  if (mermaidReady || typeof mermaid === "undefined") return;
  const forced = document.documentElement.getAttribute("data-theme");
  const dark = forced ? forced === "dark" : matchMedia("(prefers-color-scheme: dark)").matches;
  // lab-in-a-box brand palette
  const themeVariables = dark
    ? { background: "#1b1e23", primaryColor: "#23272d", primaryBorderColor: "#2fd1c2", primaryTextColor: "#f3f4f5", secondaryColor: "#2c3138", tertiaryColor: "#1b1e23", lineColor: "#a9aeb7", clusterBkg: "#23272d", clusterBorder: "#353a42", fontFamily: '"Schibsted Grotesk","Helvetica Neue",Helvetica,Arial,sans-serif' }
    : { background: "#ffffff", primaryColor: "#f3f4f5", primaryBorderColor: "#0f8f86", primaryTextColor: "#23272d", secondaryColor: "#eceef1", tertiaryColor: "#ffffff", lineColor: "#5a606b", clusterBkg: "#ffffff", clusterBorder: "#dfe2e6", fontFamily: '"Schibsted Grotesk","Helvetica Neue",Helvetica,Arial,sans-serif' };
  mermaid.initialize({ startOnLoad: false, theme: "base", themeVariables, securityLevel: "strict" });
  mermaidReady = true;
}

const diagramLabel = (s) => String(s).replace(/["\r\n]/g, "'"); // quotes/newlines would break mermaid's own syntax

/* A plain char-class replace collapses any non-alphanumeric character to
 * "_", so two different raw strings that only differ in punctuation (e.g.
 * "a.b" vs "a-b") sanitize to the exact same id — mermaid then treats them
 * as ONE node/subgraph, silently merging or overwriting one in the
 * diagram. Returns a per-render sanitizer: the same raw string always maps
 * to the same id (stable across the node/cluster/addon calls below, which
 * may re-derive an id for the same owner more than once), but a raw string
 * that collides with a DIFFERENT one already seen gets a disambiguating
 * numeric suffix instead of silently reusing that id. */
function makeIdSanitizer() {
  const idForRaw = new Map();
  const claimed = new Set();
  return function sanitizeId(raw) {
    const key = String(raw);
    if (idForRaw.has(key)) return idForRaw.get(key);
    const base = "n_" + key.replace(/[^A-Za-z0-9_]/g, "_");
    let id = base;
    for (let n = 2; claimed.has(id); n++) id = base + "_" + n;
    claimed.add(id);
    idForRaw.set(key, id);
    return id;
  };
}

/* Build a mermaid `graph TB` definition from the lab assembled so far, or
 * null if there's nothing to draw yet (no nodes). One subgraph per
 * kcluster, its member nodes inside; nodes with no (or an unknown)
 * kcluster stand alone. Addons — both cluster-level and per-node — render
 * as small pill-shaped boxes linked in with a dotted line, since they're
 * attached behavior, not infrastructure. An `existing` node (Phase 5's
 * pre-provisioned-host support) gets a dashed border — it's not a VM this
 * tool creates, worth telling apart from one that is at a glance. */
function buildMermaidDiagram(lab) {
  const nodes = lab.nodes || {};
  const kclusters = lab.kclusters || {};
  const nodeNames = Object.keys(nodes);
  if (!nodeNames.length) return null;

  const sanitizeId = makeIdSanitizer(); // fresh dedup state for this render only
  const lines = ["graph TB"];
  const existingIds = [];
  const addonIds = [];
  const placed = new Set();

  function addonBox(ownerRawName, ownerId, addons) {
    (addons || []).forEach((addon, i) => {
      const aid = sanitizeId(ownerRawName + "_addon_" + i + "_" + addon);
      lines.push(`  ${aid}(["${diagramLabel(addon)}"])`);
      lines.push(`  ${ownerId} -.-> ${aid}`);
      addonIds.push(aid);
    });
  }

  Object.entries(kclusters).forEach(([cluName, cluCfg]) => {
    const cluId = sanitizeId("clu_" + cluName);
    const cluType = (cluCfg && cluCfg.clu_type) || "?";
    lines.push(`  subgraph ${cluId}["${diagramLabel(cluName)} (${diagramLabel(cluType)})"]`);
    nodeNames.forEach((nname) => {
      const ncfg = nodes[nname] || {};
      if (ncfg.kcluster !== cluName) return;
      const nid = sanitizeId(nname);
      placed.add(nname);
      const ip = ncfg.myip ? `<br/>${diagramLabel(ncfg.myip)}` : "";
      lines.push(`    ${nid}["${diagramLabel(nname)}${ip}"]`);
      if (ncfg.existing) existingIds.push(nid);
    });
    lines.push("  end");
    addonBox(cluName, cluId, cluCfg && cluCfg.addons);
  });

  nodeNames.forEach((nname) => {
    if (placed.has(nname)) return;
    const ncfg = nodes[nname] || {};
    const nid = sanitizeId(nname);
    const ip = ncfg.myip ? `<br/>${diagramLabel(ncfg.myip)}` : "";
    lines.push(`  ${nid}["${diagramLabel(nname)}${ip}"]`);
    if (ncfg.existing) existingIds.push(nid);
    addonBox(nname, nid, ncfg.addons);
  });

  if (existingIds.length) {
    lines.push("  classDef existingNode stroke-dasharray: 4 3,stroke-width:2px;");
    lines.push(`  class ${existingIds.join(",")} existingNode;`);
  }
  return lines.join("\n");
}

async function renderDiagram() {
  const canvas = $("#diagramCanvas");
  const empty = $("#diagramEmpty");
  const def = buildMermaidDiagram(state.lab);
  if (!def) {
    canvas.innerHTML = "";
    empty.hidden = false;
    return;
  }
  empty.hidden = true;
  initMermaid();
  try {
    const { svg } = await mermaid.render("archDiagramSvg", def);
    canvas.innerHTML = svg;
  } catch (e) {
    canvas.innerHTML = "";
    empty.hidden = false;
    empty.textContent = "Couldn't render the diagram: " + e.message;
  }
}

function switchLabView(view) {
  const isDiagram = view === "diagram";
  $("#viewTabJson").classList.toggle("active", !isDiagram);
  $("#viewTabJson").setAttribute("aria-selected", String(!isDiagram));
  $("#viewTabDiagram").classList.toggle("active", isDiagram);
  $("#viewTabDiagram").setAttribute("aria-selected", String(isDiagram));
  $("#labPreview").hidden = isDiagram;
  $("#labDiagram").hidden = !isDiagram;
  if (isDiagram) renderDiagram();
}

// ---- assemble lab ----------------------------------------------------------
// state.lab is always compiled from the block model (see compileLab above).
// fromText: the change came from the lab.json text, which is then left as typed.
function refreshLab(fromText) {
  state.lab = compileLab(state.model);
  if (!fromText) { $("#labPreview").value = JSON.stringify(state.lab, null, 2); state.textError = ""; }
  const n = Object.keys(state.lab).length;
  $("#sectionCount").textContent = `${n} section${n === 1 ? "" : "s"}`;
  $("#validateResult").hidden = true;
  renderIssues();
  if (!$("#labDiagram").hidden) renderDiagram();
}

// Lists lintLab()'s errors and warnings under lab.json, and greys out the
// actions that write the lab while it has errors.
function renderIssues() {
  const r = lintLab(state.model);
  const errors = state.textError ? ["lab.json does not parse: " + state.textError] : r.errors;
  const box = $("#labIssues");
  box.innerHTML = "";
  errors.forEach((t) => box.appendChild(el("li", "issue err", t)));
  r.warnings.forEach((t) => box.appendChild(el("li", "issue warn", t)));
  box.hidden = !errors.length && !r.warnings.length;
  const lock = serverLock();
  [["#downloadBtn", ""], ["#saveBtn", lock], ["#createBtn", lock]].forEach(([b, why]) => {
    const btn = $(b);
    if (!btn) return;
    btn.disabled = errors.length > 0 || !!why;
    btn.title = why || (errors.length ? "Fix the errors listed above first" : "");
  });
  const saved = $("#savedBtn");
  if (saved) { saved.disabled = !!lock; saved.title = lock || "Open a lab saved on the server"; }
}

// Replaces the canvas with lab definition `lab` and loads its add-ons' schemas,
// so their required fields are checked.
function loadLab(lab, fromText) {
  state.model = decompileLab(lab);
  state.sel = "common";
  state.textError = "";
  renderCanvas();
  refreshLab(fromText);
  const comps = new Set(state.model.items.filter((i) => i.type === "addon" && i.comp && !state.schemaCache[i.comp]).map((i) => i.comp));
  Promise.all([...comps].map((c) => loadAddonSchema(c).catch(() => null)))
    .then(() => { if (comps.size) { renderCanvas(); renderIssues(); } });
}

// The lab.json text was edited: rebuild the canvas once it parses.
function labTextChanged() {
  clearTimeout(labTextChanged._t);
  labTextChanged._t = setTimeout(() => {
    try { loadLab(parseLab($("#labPreview").value), true); }
    catch (e) { state.textError = e.message; renderIssues(); }
  }, 400);
}

// Opens a lab definition file (.json, .yaml, .yml) from this computer.
function openLabFile(file) {
  if (!file) return;
  file.text().then((text) => {
    loadLab(parseLab(text));
    $("#labName").value = file.name.replace(/\.(json|ya?ml)$/i, "");
    toast("Opened " + file.name);
  }).catch((e) => toast("Cannot open " + file.name + ": " + e.message));
}

// ---- refresh available images without losing the form ---------------------
// Reported live 2026-09-01: the only way to get a fresh ISO_IMAGE list was
// to re-select the component/base topology, which wipes every value already
// typed in. This re-fetches just the "status" snapshot (already used for the
// status panel) and rebuilds ONLY the ISO_IMAGE field(s) currently on the
// form — via fieldRow(), so the rebuilt widget gets the exact same wiring
// (cascade/validation/userTouched) a freshly-rendered one would — leaving
// every other field's value untouched. The field's current value is kept
// even if it no longer matches any listed image (added as a synthetic extra
// option) rather than silently dropped.
async function refreshImages() {
  try {
    const s = await apiGet("status");
    const images = s.images || [];
    const targets = Array.from($("#schemaForm").querySelectorAll("[data-outpath],[data-field]"))
      .filter((elm) => elm._field && elm._field.name === "ISO_IMAGE");
    targets.forEach((old) => {
      const outPath = old.dataset.outpath ? JSON.parse(old.dataset.outpath).slice(0, -1) : [];
      const repeatMode = old.dataset.repeat === "1";
      const field = Object.assign({}, old._field, { enum: images.length ? images.slice() : undefined });
      const oldValue = old.value;
      if (oldValue && !images.includes(oldValue)) field.enum = (field.enum || []).concat(oldValue);
      const newRow = fieldRow(field, outPath, repeatMode);
      const newInput = newRow.querySelector("select,input");
      newInput.value = oldValue;
      if (old.dataset.userTouched) newInput.dataset.userTouched = old.dataset.userTouched;
      old.closest(".field").replaceWith(newRow);
    });
    loadStatus();
    toast(targets.length
      ? `Image list refreshed (${images.length} available).`
      : "Image list refreshed — no ISO_IMAGE field on this form.");
  } catch (e) { toast("Refresh error: " + e.message); }
}

// ---- lab actions -----------------------------------------------------------
async function validateLab() {
  const box = $("#validateResult");
  try {
    const r = await apiPost("validate", { config: state.lab });
    box.hidden = false;
    box.className = "validate-result " + (r.ok ? "ok" : "err");
    box.textContent = r.ok ? "✓ Valid lab definition." : (r.output || "Validation failed.");
  } catch (e) { toast("Validate error: " + e.message); }
}
function downloadLab() {
  if ($("#downloadBtn").disabled) return;
  const name = ($("#labName").value.trim() || "lab").replace(/[^A-Za-z0-9._-]/g, "_");
  const blob = new Blob([JSON.stringify(state.lab, null, 2) + "\n"], { type: "application/json" });
  const a = el("a"); a.href = URL.createObjectURL(blob);
  a.download = name.endsWith(".json") ? name : name + ".json";
  a.click(); URL.revokeObjectURL(a.href);
}
async function saveLab() {
  if ($("#saveBtn").disabled) return;
  try {
    const r = await adminCall("save", { filename: $("#labName").value.trim() || "lab", config: state.lab });
    // r.path is the full server-side path (api.py's "save" action already
    // returns it) — show it, not just the basename, so it's actually
    // findable (reported live 2026-09-01: the toast never said where).
    toast("Saved: " + (r.path || r.saved));
  } catch (e) { toast("Save error: " + e.message); }
}

// ---- saved labs (login) ---------------------------------------------------------
async function openSavedDialog() {
  const list = $("#savedList");
  list.innerHTML = "";
  showDialog("#savedDialog");
  try {
    const r = await adminCall("labs");
    if (!r.labs.length) list.appendChild(el("li", "muted", "No lab is saved on the server yet."));
    r.labs.forEach((l) => {
      const li = el("li", "saved-row");
      li.appendChild(el("span", "saved-name", l.name));
      li.appendChild(el("span", "muted", new Date(l.modified * 1000).toLocaleString()));
      const b = el("button", "btn", "Open");
      b.type = "button";
      b.addEventListener("click", async () => {
        try {
          const got = await adminCall("lab", undefined, { name: l.name });
          loadLab(got.lab);
          $("#labName").value = l.name.replace(/\.json$/, "");
          closeDialog("#savedDialog");
          toast("Opened " + l.name);
        } catch (e) { toast("Cannot open " + l.name + ": " + e.message); }
      });
      li.appendChild(b);
      list.appendChild(li);
    });
  } catch (e) { list.appendChild(el("li", "issue err", e.message)); }
}

function showDialog(sel) {
  const dlg = $(sel);
  if (typeof dlg.showModal === "function") { if (!dlg.open) dlg.showModal(); } else dlg.setAttribute("open", "");
}
function closeDialog(sel) {
  const dlg = $(sel);
  if (typeof dlg.close === "function") dlg.close(); else dlg.removeAttribute("open");
}

// ---- credentials (login, HTTPS) ---------------------------------------------------
// The field tables come from lab-builder-helper (setup_credentials.py's
// PROVIDER_FIELDS / SERVICE_CREDENTIAL_FIELDS); values are never read back.
async function openCredentials() {
  showDialog("#credDialog");
  const lock = serverLock();
  $("#credMsg").textContent = lock;
  $("#credMsg").hidden = !lock;
  $("#credBody").hidden = !!lock;
  if (!lock) await loadCredentials();
}

async function loadCredentials() {
  const tbody = $("#credList");
  tbody.innerHTML = "";
  try {
    state.cred = await adminCall("credentials");
  } catch (e) {
    $("#credMsg").textContent = e.message; $("#credMsg").hidden = false; return;
  }
  $("#credDir").textContent = state.cred.dir;
  if (!state.cred.credentials.length) {
    const tr = el("tr"); const td = el("td", "muted", "No credential is stored yet."); td.colSpan = 4;
    tr.appendChild(td); tbody.appendChild(tr);
  }
  state.cred.credentials.forEach((c) => {
    const tr = el("tr");
    tr.appendChild(el("td", "cred-name", c.file));
    tr.appendChild(el("td", "", (c.kind === "cloud" ? "Cloud account" : c.kind === "service" ? "Service" : "Unknown") + (c.type ? ": " + c.type : "")));
    tr.appendChild(el("td", "", c.encrypted ? "Encrypted" : c.encrypted_fields.length ? "Secrets encrypted"
      : c.plaintext_secrets.length ? "Plaintext secrets" : "No secrets"));
    const act = el("td", "cred-actions");
    if (c.plaintext_secrets.length) {
      const enc = el("button", "btn", "Encrypt…"); enc.type = "button";
      enc.addEventListener("click", () => encryptCredential(c));
      act.appendChild(enc);
    }
    const del = el("button", "btn danger", "Delete"); del.type = "button";
    del.addEventListener("click", () => deleteCredential(c));
    act.appendChild(del);
    tr.appendChild(act);
    tbody.appendChild(tr);
  });
  renderCredTypes();
}

function renderCredTypes() {
  const kind = $("#credKind").value;
  const sel = $("#credType");
  const prev = sel.value;
  sel.innerHTML = "";
  Object.keys(state.cred[kind] || {}).sort().forEach((t) => sel.appendChild(new Option(t, t)));
  if (prev && [...sel.options].some((o) => o.value === prev)) sel.value = prev;
  renderCredFields();
}

function renderCredFields() {
  const box = $("#credFields");
  box.innerHTML = "";
  (state.cred[$("#credKind").value][$("#credType").value] || []).forEach((f) => {
    const lab = el("label", "field");
    lab.appendChild(el("span", "f-name", f.name + (f.required ? " *" : "")));
    const inp = el("input");
    inp.type = f.secret ? "password" : "text";
    inp.autocomplete = "off";
    inp.dataset.cred = f.name;
    inp.required = f.required;
    lab.appendChild(inp);
    box.appendChild(lab);
  });
}

// The passphrase in inputs `a` and `b`, or null with a toast when it is too short or they differ.
function passphraseFrom(a, b) {
  const pw = $(a).value;
  if (pw.length < 8) { toast("The passphrase must have at least 8 characters."); return null; }
  if (pw !== $(b).value) { toast("The passphrases differ."); return null; }
  return pw;
}

async function addCredential() {
  const fields = {};
  document.querySelectorAll("#credFields [data-cred]").forEach((i) => { if (i.value !== "") fields[i.dataset.cred] = i.value; });
  const encrypt = $("#credEncrypt").checked;
  const passphrase = encrypt ? passphraseFrom("#credPass", "#credPass2") : null;
  if (encrypt && passphrase === null) return;
  try {
    const r = await adminCall("credentials-add", {
      kind: $("#credKind").value, type: $("#credType").value, account: $("#credAccount").value.trim(), fields, passphrase,
    });
    toast("Saved " + r.file);
    ["#credAccount", "#credPass", "#credPass2"].forEach((s) => { $(s).value = ""; });
    renderCredFields();
    loadCredentials();
  } catch (e) { toast("Not saved: " + e.message); }
}

async function encryptCredential(c) {
  const pw = window.prompt("Passphrase for " + c.name + ".encrypted.yaml (at least 8 characters). It encrypts "
    + c.plaintext_secrets.join(", ") + "; " + c.file + " is kept until you delete it.");
  if (pw === null) return;
  if (pw.length < 8) { toast("The passphrase must have at least 8 characters."); return; }
  if (window.prompt("Type the passphrase again") !== pw) { toast("The passphrases differ."); return; }
  try {
    const r = await adminCall("credentials-encrypt", { file: c.file, passphrase: pw });
    toast("Wrote " + r.file + ". Delete " + c.file + " once you have checked it.");
    loadCredentials();
  } catch (e) { toast("Not encrypted: " + e.message); }
}

async function deleteCredential(c) {
  if (!window.confirm("Delete " + c.file + "? Labs that use it stop working.")) return;
  try {
    await adminCall("credentials-delete", { file: c.file });
    toast("Deleted " + c.file);
    loadCredentials();
  } catch (e) { toast("Not deleted: " + e.message); }
}

// ---- Create lab (login, HTTPS) ----------------------------------------------------
// What the lab creates, for the confirmation.
function labSummary(lab) {
  const nodes = Object.keys(lab.nodes || {});
  const clusters = Object.keys(lab.kclusters || {});
  const backends = new Set(nodes.map((n) => (lab.nodes[n].backend || (lab.common || {}).backend || "libvirt")));
  const lines = [nodes.length + " VM" + (nodes.length === 1 ? "" : "s") + (nodes.length ? ": " + nodes.join(", ") : "")];
  if (clusters.length) lines.push(clusters.length + " Kubernetes cluster" + (clusters.length === 1 ? "" : "s") + ": " + clusters.join(", "));
  if (backends.size) lines.push("On: " + [...backends].join(", "));
  return lines.join("\n");
}

function openCreate() {
  if ($("#createBtn").disabled) return;
  const name = ($("#labName").value.trim() || "lab").replace(/\.json$/, "");
  $("#createTitle").textContent = name;
  $("#createSummary").textContent = labSummary(state.lab);
  $("#createConfirm").hidden = false;
  $("#createJob").hidden = true;
  $("#createGo").hidden = false;
  showDialog("#createDialog");
}

async function startCreate() {
  const filename = ($("#labName").value.trim() || "lab").replace(/\.json$/, "");
  try {
    await adminCall("save", { filename, config: state.lab });
    const r = await adminCall("create", { filename: filename + ".json", keep: $("#createKeep").checked });
    $("#createConfirm").hidden = true;
    $("#createGo").hidden = true;
    $("#createJob").hidden = false;
    watchJob(r.job);
  } catch (e) { toast("Not started: " + e.message); }
}

// Polls job `id` every 3 s and shows its state and log until it ends.
async function watchJob(id) {
  clearTimeout(watchJob._t);
  try {
    const j = await adminCall("job", undefined, { id });
    const st = $("#jobState");
    st.textContent = j.state === "running" ? "Creating… (job " + id + ")"
      : j.state === "done" ? "Created. setup_lab.py finished." : "Failed: setup_lab.py exited with " + (j.rc == null ? "no status" : j.rc) + ".";
    st.className = "job-state " + j.state;
    const log = $("#jobLog");
    const atEnd = log.scrollTop + log.clientHeight >= log.scrollHeight - 4;
    log.textContent = j.log || "";
    if (atEnd) log.scrollTop = log.scrollHeight;
    if (j.state === "running") watchJob._t = setTimeout(() => watchJob(id), 3000);
  } catch (e) { $("#jobState").textContent = "Cannot read the job: " + e.message; }
}

// ---- wire up ---------------------------------------------------------------
window.addEventListener("DOMContentLoaded", () => {
  $("#filter").addEventListener("input", (e) => renderPalette(e.target.value));
  $("#validateBtn").addEventListener("click", validateLab);
  $("#downloadBtn").addEventListener("click", downloadLab);
  $("#saveBtn").addEventListener("click", saveLab);
  $("#savedBtn").addEventListener("click", openSavedDialog);
  $("#savedClose").addEventListener("click", () => closeDialog("#savedDialog"));
  $("#credBtn").addEventListener("click", openCredentials);
  $("#credClose").addEventListener("click", () => closeDialog("#credDialog"));
  $("#credKind").addEventListener("change", renderCredTypes);
  $("#credType").addEventListener("change", renderCredFields);
  $("#credEncrypt").addEventListener("change", (e) => { $("#credPassBox").hidden = !e.target.checked; });
  $("#credAddForm").addEventListener("submit", (e) => { e.preventDefault(); addCredential(); });
  $("#createBtn").addEventListener("click", openCreate);
  $("#createGo").addEventListener("click", startCreate);
  $("#createClose").addEventListener("click", () => { clearTimeout(watchJob._t); closeDialog("#createDialog"); });
  $("#refreshImagesBtn").addEventListener("click", refreshImages);
  $("#editorImages").addEventListener("click", refreshImages);
  $("#viewTabJson").addEventListener("click", () => switchLabView("json"));
  $("#viewTabDiagram").addEventListener("click", () => switchLabView("diagram"));
  $("#labPreview").addEventListener("input", labTextChanged);
  $("#openBtn").addEventListener("click", () => $("#openFile").click());
  $("#openFile").addEventListener("change", (e) => { openLabFile(e.target.files[0]); e.target.value = ""; });

  // editor dialog
  $("#editorApply").addEventListener("click", applyEditor);
  $("#editorCancel").addEventListener("click", closeEditor);
  $("#editorRemove").addEventListener("click", () => { const id = state.editing; closeEditor(); if (id && id !== "common") removeItem(id); });
  $("#schemaForm").addEventListener("submit", (e) => { e.preventDefault(); applyEditor(); });
  $("#editor").addEventListener("close", () => { state.editing = null; });
  $("#canvas").addEventListener("click", () => selectBlock("common"));

  // "Required only" — hides every non-required field via CSS on the form
  // element itself, so it stays in effect across editor opens. Preference
  // remembered per-browser.
  const requiredOnlyToggle = $("#requiredOnlyToggle");
  let requiredOnly = false;
  try { requiredOnly = localStorage.getItem("labbuilder.requiredOnly") === "1"; } catch (e) { /* ignore */ }
  requiredOnlyToggle.checked = requiredOnly;
  $("#schemaForm").classList.toggle("hide-optional", requiredOnly);
  requiredOnlyToggle.addEventListener("change", (e) => {
    $("#schemaForm").classList.toggle("hide-optional", e.target.checked);
    try { localStorage.setItem("labbuilder.requiredOnly", e.target.checked ? "1" : "0"); } catch (err) { /* ignore */ }
  });

  if (window.LAB_STATIC) { $("#credBtn").disabled = true; $("#credBtn").title = serverLock(); }
  renderCanvas();
  refreshLab();
  if (!window.LAB_STATIC) apiGet("auth").then((a) => { state.auth = a; renderIssues(); }).catch(() => { /* older server */ });
  loadComponents().catch((e) => { $("#countNum").textContent = "!"; toast("Load error: " + e.message); });
  loadBase().then(() => { renderCanvas(); renderIssues(); }).catch(() => { /* shown on first edit */ });
  loadStatus();
});

// Re-theme the diagram when the user switches light/dark from the toggle.
document.addEventListener("themechange", () => {
  mermaidReady = false;
  const tab = document.getElementById("viewTabDiagram");
  if (tab && tab.classList.contains("active")) tab.click();
});
