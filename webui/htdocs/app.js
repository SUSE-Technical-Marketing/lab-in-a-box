"use strict";
/*
 * lab-builder frontend.
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
const state = { components: [], current: null, lab: {}, commonDefaults: {} };

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

// ---- catalogue -------------------------------------------------------------
async function loadComponents() {
  const data = await apiGet("components");
  state.components = data.components;
  $("#countNum").textContent = data.count;
  $("#srcNote").textContent = `read from ${data.scripts_dir}`;
  renderCatalogue("");
}
function renderCatalogue(filter) {
  const ul = $("#componentList");
  ul.innerHTML = "";
  const f = filter.toLowerCase();
  // pinned base-topology entry (common/nodes/kclusters)
  if (!f || "lab topology common nodes kclusters".includes(f)) {
    const li = el("li", "pinned");
    li.appendChild(el("div", "ci-title", "▚ Lab topology"));
    li.appendChild(el("div", "ci-desc", "common · nodes · kclusters"));
    li.addEventListener("click", () => selectBase(li));
    ul.appendChild(li);
  }
  state.components
    .filter((c) => !f || c.title.toLowerCase().includes(f) || (c.description || "").toLowerCase().includes(f))
    .forEach((c) => {
      const li = el("li");
      li.dataset.name = c.name;
      li.appendChild(el("div", "ci-title", c.title));
      if (c.description) li.appendChild(el("div", "ci-desc", c.description));
      li.appendChild(el("div", "ci-meta", `${c.field_count} option${c.field_count === 1 ? "" : "s"}`));
      if (c.layers && c.layers.length) {
        const layers = el("div", "ci-layers");
        c.layers.forEach((l) => layers.appendChild(el("span", "layer-badge layer-" + l, l)));
        li.appendChild(layers);
      }
      li.addEventListener("click", () => selectComponent(c.name, li));
      ul.appendChild(li);
    });
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

// ---- schema form -----------------------------------------------------------
async function selectComponent(name, li) {
  document.querySelectorAll("#componentList li").forEach((x) => x.classList.remove("active"));
  if (li) li.classList.add("active");
  const schema = await apiGet("schema", { name });
  state.current = schema;
  state.commonDefaults = {};
  $("#formTitle").textContent = schema.section || schema.component || name;
  $("#formDesc").textContent = schema.description || "";
  const layersEl = $("#formLayers");
  if (layersEl) {
    layersEl.innerHTML = "";
    const layers = (schema.capabilities && schema.capabilities.layers) || [];
    layers.forEach((l) => layersEl.appendChild(el("span", "layer-badge layer-" + l, l)));
  }
  $("#formEmpty").hidden = true;
  $("#addBtn").disabled = false;
  const form = $("#schemaForm");
  form.innerHTML = "";
  walk(schema, form, []);
}

async function selectBase(li) {
  document.querySelectorAll("#componentList li").forEach((x) => x.classList.remove("active"));
  if (li) li.classList.add("active");
  const schema = await apiGet("base");
  state.current = schema;
  state.commonDefaults = {};
  $("#formTitle").textContent = "Lab topology";
  $("#formDesc").textContent = schema.description || "";
  $("#formEmpty").hidden = true;
  $("#addBtn").disabled = false;
  const form = $("#schemaForm");
  form.innerHTML = "";
  walk(schema, form, []);
}

/* Recursively render `node` into `parent`, tracking the output-key path. */
function walk(node, parent, outPath) {
  if (Array.isArray(node)) { node.forEach((n) => walk(n, parent, outPath)); return; }
  if (!node || typeof node !== "object") return;
  if (isField(node)) { parent.appendChild(fieldRow(node, outPath)); return; }

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
  if (options) {
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
function addToLab() {
  const s = state.current;
  if (!s) return;
  const frag = serializeForm();
  if (s.section && Array.isArray(s.fields)) {
    // flat addon schema -> nest under its section name
    state.lab[s.section] = frag;
  } else {
    Object.assign(state.lab, frag);           // structured schema already carries its keys
  }
  refreshLab();
  toast(`Added “${s.section || s.component}” to lab.json`);
}
function refreshLab() {
  $("#labPreview").textContent = JSON.stringify(state.lab, null, 2);
  const n = Object.keys(state.lab).length;
  $("#sectionCount").textContent = `${n} section${n === 1 ? "" : "s"}`;
  $("#validateResult").hidden = true;
  if (!$("#labDiagram").hidden) renderDiagram();
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
  const name = ($("#labName").value.trim() || "lab").replace(/[^A-Za-z0-9._-]/g, "_");
  const blob = new Blob([JSON.stringify(state.lab, null, 2) + "\n"], { type: "application/json" });
  const a = el("a"); a.href = URL.createObjectURL(blob);
  a.download = name.endsWith(".json") ? name : name + ".json";
  a.click(); URL.revokeObjectURL(a.href);
}
async function saveLab() {
  try {
    const r = await apiPost("save", { filename: $("#labName").value.trim() || "lab", config: state.lab });
    // r.path is the full server-side path (api.py's "save" action already
    // returns it) — show it, not just the basename, so it's actually
    // findable (reported live 2026-09-01: the toast never said where).
    toast("Saved: " + (r.path || r.saved));
  } catch (e) { toast("Save error: " + e.message); }
}

// ---- wire up ---------------------------------------------------------------
window.addEventListener("DOMContentLoaded", () => {
  $("#filter").addEventListener("input", (e) => renderCatalogue(e.target.value));
  $("#addBtn").addEventListener("click", addToLab);
  $("#validateBtn").addEventListener("click", validateLab);
  $("#downloadBtn").addEventListener("click", downloadLab);
  $("#saveBtn").addEventListener("click", saveLab);
  $("#refreshImagesBtn").addEventListener("click", refreshImages);
  $("#viewTabJson").addEventListener("click", () => switchLabView("json"));
  $("#viewTabDiagram").addEventListener("click", () => switchLabView("diagram"));

  // "Required only" — hides every non-required field via CSS on the form
  // element itself, so it stays in effect across re-renders (selectComponent/
  // selectBase only clear the form's children, never the form element's own
  // class) and across dynamically-added repeat-group instances (nodes/
  // kclusters "+ add" rows are just more .field-optional/.field-required
  // descendants of the same #schemaForm). Preference remembered per-browser.
  const requiredOnlyToggle = $("#requiredOnlyToggle");
  let requiredOnly = false;
  try { requiredOnly = localStorage.getItem("labbuilder.requiredOnly") === "1"; } catch (e) { /* ignore */ }
  requiredOnlyToggle.checked = requiredOnly;
  $("#schemaForm").classList.toggle("hide-optional", requiredOnly);
  requiredOnlyToggle.addEventListener("change", (e) => {
    $("#schemaForm").classList.toggle("hide-optional", e.target.checked);
    try { localStorage.setItem("labbuilder.requiredOnly", e.target.checked ? "1" : "0"); } catch (err) { /* ignore */ }
  });

  loadComponents().catch((e) => { $("#countNum").textContent = "!"; toast("Load error: " + e.message); });
  loadStatus();
});

// Re-theme the diagram when the user switches light/dark from the toggle.
document.addEventListener("themechange", () => {
  mermaidReady = false;
  const tab = document.getElementById("viewTabDiagram");
  if (tab && tab.classList.contains("active")) tab.click();
});
