// DOM-free functional smoke test for webui/htdocs/app.js's pure logic
// (validateFieldValue, applyFieldDefault) — loaded via node's vm module with
// minimal document/window stubs, no jsdom/npm dependency. Run from
// 07_webui_js.sh, in its own container — see tests/run_tests.sh.
"use strict";
const fs = require("fs");
const vm = require("vm");

const src = fs.readFileSync("webui/htdocs/app.js", "utf8");
const sandbox = {
  document: { querySelectorAll: () => [], addEventListener: () => {} },
  window: { addEventListener: () => {} },
  console, URL, URLSearchParams, TextDecoder, atob,
};
vm.createContext(sandbox);
vm.runInContext(src, sandbox, { filename: "app.js" });

let failures = 0;
function check(desc, cond) {
  if (!cond) { failures++; console.error("FAIL: " + desc); }
}

// -- validateFieldValue -------------------------------------------------
check("empty value never flagged",
  sandbox.validateFieldValue({ type: "integer" }, "") === null);
check("non-numeric integer rejected",
  sandbox.validateFieldValue({ type: "integer" }, "abc") !== null);
check("integer below min rejected",
  sandbox.validateFieldValue({ type: "integer", min: 256 }, "10") !== null);
check("integer within range accepted",
  sandbox.validateFieldValue({ type: "integer", min: 1, max: 64 }, "6") === null);
check("port above 65535 rejected",
  sandbox.validateFieldValue({ type: "port" }, "70000") !== null);
check("pattern mismatch rejected",
  sandbox.validateFieldValue({ type: "string", pattern: "^\\d+\\.\\d+\\.\\d+\\.\\d+$" }, "not-an-ip") !== null);
check("pattern match accepted",
  sandbox.validateFieldValue({ type: "string", pattern: "^\\d+\\.\\d+\\.\\d+\\.\\d+$" }, "10.0.0.1") === null);

// -- applyFieldDefault: plain input placeholder --------------------------
{
  const field = { name: "VM_MEM", type: "integer", default: 24576 };
  const input = { _field: field, tagName: "INPUT", dataset: {} };
  sandbox.applyFieldDefault(input, undefined);
  check("plain input: schema default used as placeholder when no live value",
    input.placeholder === "24576");
  sandbox.applyFieldDefault(input, "8192");
  check("plain input: live (common) value overrides schema default",
    input.placeholder === "8192");
}

// -- applyFieldDefault: empty-string default must not read as "no default" --
{
  const field = { name: "config_method", type: "string", default: "" };
  const input = {
    _field: field, tagName: "SELECT", dataset: {}, options: [
      { value: "", dataset: { label: "(default) Ignition+Combustion", fixedLabel: "" }, className: "" },
      { value: "cloud-init", dataset: { label: "cloud-init", fixedLabel: "" }, className: "" },
    ],
  };
  sandbox.applyFieldDefault(input, undefined);
  check("select: empty-string schema default still gets marked as the default option",
    input.options[0].className === "opt-default" && input.options[1].className === "");
}

// -- applyFieldDefault: select value only synced before user interaction --
{
  const field = { name: "backend", type: "string", default: "libvirt" };
  const opts = [
    { value: "", dataset: { blank: "1" }, className: "" },
    { value: "libvirt", dataset: { label: "libvirt", fixedLabel: "" }, className: "" },
  ];
  const input = { _field: field, tagName: "SELECT", dataset: {}, options: opts, value: "" };
  sandbox.applyFieldDefault(input, undefined);
  check("select: value synced to default before user touches it",
    input.value === "libvirt");
  input.dataset.userTouched = "1";
  input.value = "";
  sandbox.applyFieldDefault(input, undefined);
  check("select: value left alone once user has touched it",
    input.value === "");
}

// -- applyFieldDefault: repeat-group select never auto-fills its value ------
// Regression test for a real bug reported live 2026-09-01: INSTALL_RKE2_TYPE
// (a per-node/repeatable field with a schema default) always showed up in
// the saved lab.json for every node, even ones whose role was never
// actually chosen — because the select's own .value, not just its
// placeholder-equivalent labeling, was being set to the default.
{
  const field = { name: "INSTALL_RKE2_TYPE", type: "string", default: "server" };
  const opts = [
    { value: "", dataset: { blank: "1" }, className: "" },
    { value: "server", dataset: { label: "server", fixedLabel: "" }, className: "" },
    { value: "agent", dataset: { label: "agent", fixedLabel: "" }, className: "" },
  ];
  const input = { _field: field, tagName: "SELECT", dataset: { repeat: "1" }, options: opts, value: "" };
  sandbox.applyFieldDefault(input, undefined);
  check("select: repeat-group field's value stays unset even though it has a schema default",
    input.value === "");
  check("select: repeat-group field still gets its default option labeled/marked",
    opts[1].className === "opt-default");
  // Same field shape but NOT in a repeat group (dataset.repeat unset) —
  // existing common/flat-field behavior must be unchanged.
  const flatInput = { _field: field, tagName: "SELECT", dataset: {}, options: opts.map((o) => ({ ...o })), value: "" };
  sandbox.applyFieldDefault(flatInput, undefined);
  check("select: a flat (non-repeat) field with the same default still auto-fills its value",
    flatInput.value === "server");
}

// -- buildMermaidDiagram: architecture preview diagram --------------------
check("no nodes -> nothing to draw", sandbox.buildMermaidDiagram({}) === null);

{
  // A clustered node, a standalone node, cluster-level and node-level
  // addons, and an `existing` (Phase 5 pre-provisioned host) node — one
  // definition exercising every branch at once.
  const lab = {
    nodes: {
      "node1.mydemo.lab": { myip: "192.168.88.101", kcluster: "clu1" },
      "node2.mydemo.lab": { myip: "192.168.88.102", existing: true, addons: ["mariadb"] },
    },
    kclusters: {
      clu1: { clu_type: "rke2", addons: ["rancher", "longhorn"] },
    },
  };
  const def = sandbox.buildMermaidDiagram(lab);
  check("diagram starts with a valid mermaid graph declaration", def.startsWith("graph TB"));
  check("clustered node renders inside its kcluster's subgraph",
    /subgraph n_clu_clu1\["clu1 \(rke2\)"\][\s\S]*n_node1_mydemo_lab\["node1\.mydemo\.lab<br\/>192\.168\.88\.101"\][\s\S]*end/.test(def));
  check("standalone node (no kcluster) renders outside any subgraph",
    def.includes('n_node2_mydemo_lab["node2.mydemo.lab<br/>192.168.88.102"]')
    && !new RegExp("subgraph[\\s\\S]*n_node2_mydemo_lab[\\s\\S]*end").test(def.split("subgraph")[1] || ""));
  check("cluster-level addons render as linked boxes off the cluster",
    def.includes('(["rancher"])') && def.includes('(["longhorn"])'));
  check("node-level addons render as linked boxes off that node",
    def.includes('(["mariadb"])') && / -\.-> n_node2_mydemo_lab_addon_0_mariadb/.test(def));
  check("an `existing` node gets the dashed-border class applied",
    def.includes("classDef existingNode") && / class n_node2_mydemo_lab existingNode;/.test(def));
  check("a node that is NOT `existing` is never included in the existingNode class list",
    !new RegExp("class [^;]*n_node1_mydemo_lab[^;]* existingNode;").test(def));
}

{
  // Quotes/newlines in a hostname would break mermaid's own quoted-label
  // syntax outright — confirm they're neutralized rather than passed through.
  const def = sandbox.buildMermaidDiagram({ nodes: { 'weird"host\nname': { myip: "10.0.0.1" } } });
  check("quotes and newlines in a node name are neutralized, not passed through raw",
    !def.includes('"weird"host') && !/\n.*name/.test(def.split("\n").find((l) => l.includes("weird"))));
}

{
  // sanitizeId's char-class replace collapses any non-alphanumeric char to
  // "_", so two DIFFERENT node names that differ only in punctuation
  // ("a.b" vs "a-b") used to sanitize to the exact same id — mermaid then
  // silently merged them into one box in the diagram, quietly dropping one
  // node from the rendered picture entirely.
  const lab = {
    nodes: {
      "a.b": { myip: "10.0.0.1" },
      "a-b": { myip: "10.0.0.2" },
    },
  };
  const def = sandbox.buildMermaidDiagram(lab);
  const nodeLines = def.split("\n").filter((l) => l.includes("10.0.0.1") || l.includes("10.0.0.2"));
  check("two node names differing only in punctuation still get two separate node lines",
    nodeLines.length === 2);
  const ids = nodeLines.map((l) => l.trim().split("[")[0]);
  check("...and those two lines use two DIFFERENT mermaid ids, not the same one merged",
    ids[0] !== ids[1]);
  check("both node labels still show up intact (neither one silently dropped)",
    def.includes('"a.b<br/>10.0.0.1"') && def.includes('"a-b<br/>10.0.0.2"'));
}

{
  // The same raw name used twice within one render (a node acting as both a
  // node-line and an addon-box owner) must still resolve to the SAME id
  // both times — the dedup fix must not turn a stable, repeated lookup into
  // a fresh disambiguated id on its second call.
  const lab = { nodes: { "solo.mydemo.lab": { myip: "10.0.0.9", addons: ["longhorn"] } } };
  const def = sandbox.buildMermaidDiagram(lab);
  const nodeLine = def.split("\n").find((l) => l.includes("10.0.0.9"));
  const nodeId = nodeLine.trim().split("[")[0];
  check("a node's own id is reused as-is (not re-disambiguated) when it's also an addon-box owner",
    def.includes(nodeId + " -.-> "));
}

// -- compileLab: block canvas -> lab.json ---------------------------------
check("empty model compiles to an empty lab", JSON.stringify(sandbox.compileLab({ common: {}, items: [], addonCfg: {} })) === "{}");
{
  const model = {
    common: { VM_MEM: "8192", VM_DSK: "" },
    items: [
      { id: "c1", type: "cluster", name: "clu1", cfg: { clu_type: "rke2" }, parent: null },
      { id: "n1", type: "node", name: "n1.lab", cfg: { myip: "10.0.0.1" }, parent: "c1" },
      { id: "n2", type: "node", name: "n2.lab", cfg: { myip: "10.0.0.2", VM_MEM: "4096" }, parent: null },
      { id: "n3", type: "node", name: "", cfg: { myip: "10.0.0.3" }, parent: null },
      { id: "a1", type: "addon", comp: "rancher", section: "rancher", flat: true, parent: "c1" },
      { id: "a2", type: "addon", comp: "mariadb", section: "mariadb", flat: true, parent: "n2" },
      { id: "a3", type: "addon", comp: "longhorn", section: "longhorn", flat: true, parent: null },
    ],
    addonCfg: { rancher: { rancher_Version: "2.13" } },
  };
  const lab = JSON.parse(JSON.stringify(sandbox.compileLab(model)));
  check("compileLab: empty common values are dropped", JSON.stringify(lab.common) === '{"VM_MEM":"8192"}');
  check("compileLab: a VM dropped on a cluster gets kcluster",
    lab.nodes["n1.lab"].kcluster === "clu1" && lab.nodes["n2.lab"].kcluster === undefined);
  check("compileLab: per-VM override survives", lab.nodes["n2.lab"].VM_MEM === "4096");
  check("compileLab: an unnamed VM is skipped", Object.keys(lab.nodes).length === 2);
  check("compileLab: cluster add-ons listed on the cluster", JSON.stringify(lab.kclusters.clu1.addons) === '["rancher"]');
  check("compileLab: VM add-ons listed on the VM", JSON.stringify(lab.nodes["n2.lab"].addons) === '["mariadb"]');
  check("compileLab: add-on config becomes its own top-level section", lab.rancher.rancher_Version === "2.13");
  check("compileLab: an add-on with no config still gets an (empty) section",
    JSON.stringify(lab.mariadb) === "{}" && JSON.stringify(lab.longhorn) === "{}");
  const def = sandbox.buildMermaidDiagram(JSON.parse(JSON.stringify(lab)));
  check("compileLab output feeds the diagram renderer unchanged", def.includes('subgraph n_clu_clu1["clu1 (rke2)"]'));
}

// -- addonSection / shownLayers: palette section from an add-on's layers ----
check("addonSection: kubernetes only -> Kubernetes cluster add-ons", sandbox.addonSection(["kubernetes"]) === "cluster");
check("addonSection: os-native / standalone-container -> VM add-ons",
  sandbox.addonSection(["os-native"]) === "host" && sandbox.addonSection(["standalone-container"]) === "host");
check("addonSection: kubernetes and a VM layer -> either",
  sandbox.addonSection(["kubernetes", "standalone-container"]) === "both");
check("shownLayers: the kubernetes badge is not repeated under Kubernetes cluster add-ons",
  JSON.stringify(sandbox.shownLayers(["kubernetes"], "cluster")) === "[]");
check("shownLayers: VM add-ons keep their layer badge",
  JSON.stringify(sandbox.shownLayers(["os-native"], "host")) === '["os-native"]');
check("shownLayers: add-ons for either keep every badge",
  JSON.stringify(sandbox.shownLayers(["kubernetes", "standalone-container"], "both")) === '["kubernetes","standalone-container"]');

// -- placement rules, lint, load ---------------------------------------------
const st = vm.runInContext("state", sandbox);
st.components = [
  { name: "rancher", title: "rancher", layers: ["kubernetes"] },
  { name: "grafana", title: "grafana", layers: ["standalone-container"] },
  { name: "mariadb", title: "mariadb", layers: ["kubernetes", "os-native"] },
];
st.base = {
  sections: {
    common: { fields: [{ name: "SOURCE_IMAGE", required: true }, { name: "VM_MEM", required: true }, { name: "VM_BOOT", required: false, default: "uefi" }] },
    nodes: { fields: [{ name: "myip", required: true }] },
    kclusters: { fields: [{ name: "clu_type", required: true }, { name: "clu_rel", required: true }, { name: "mydomain", required: true }] },
  },
};
st.schemaCache = { rancher: { fields: [{ name: "rancher_password", required: true }] } };
st.model = {
  common: {}, addonCfg: {}, extra: {}, seq: 9,
  items: [
    { id: "c1", type: "cluster", name: "k1", cfg: { clu_type: "rke2", clu_rel: "stable", mydomain: "lab" }, parent: null },
    { id: "n1", type: "node", name: "vm1", cfg: { myip: "10.0.0.1" }, parent: "c1" },
    { id: "n2", type: "node", name: "vm2", cfg: { myip: "10.0.0.2" }, parent: null },
  ],
};
const drop = (item, target) => JSON.parse(JSON.stringify(sandbox.dropParent(item, target)));
check("placement: a Kubernetes add-on goes on a Kubernetes cluster", drop({ type: "addon", comp: "rancher" }, "c1").parent === "c1");
check("placement: a Kubernetes add-on dropped on a cluster's VM goes to that cluster", drop({ type: "addon", comp: "rancher" }, "n1").parent === "c1");
check("placement: a Kubernetes add-on cannot go on a standalone VM", !!drop({ type: "addon", comp: "rancher" }, "n2").error);
check("placement: a VM add-on cannot go on a Kubernetes cluster", !!drop({ type: "addon", comp: "grafana" }, "c1").error);
check("placement: a VM add-on goes on a VM", drop({ type: "addon", comp: "grafana" }, "n1").parent === "n1");
check("placement: an add-on for either goes on both",
  drop({ type: "addon", comp: "mariadb" }, "c1").parent === "c1" && drop({ type: "addon", comp: "mariadb" }, "n2").parent === "n2");
check("placement: pxe never goes on a VM or a Kubernetes cluster",
  !!drop({ type: "addon", comp: "__pxe" }, "n1").error && !!drop({ type: "addon", comp: "__pxe" }, "c1").error);
check("placement: any add-on can go on Common settings",
  drop({ type: "addon", comp: "rancher" }, "common").parent === null && drop({ type: "addon", comp: "__pxe" }, null).parent === null);

let lint = JSON.parse(JSON.stringify(sandbox.lintLab(st.model)));
check("lint: required common settings unset while VMs have no own value are errors",
  lint.errors.some((e) => e.includes("SOURCE_IMAGE")) && lint.errors.some((e) => e.includes("VM_MEM")));
check("lint: a common setting with a default is not required", !lint.errors.some((e) => e.includes("VM_BOOT")));
st.model.common = { ISO_IMAGE: "x.qcow2" };
st.model.items.forEach((i) => { if (i.type === "node") i.cfg.VM_MEM = 2048; });
lint = JSON.parse(JSON.stringify(sandbox.lintLab(st.model)));
check("lint: VMs' own values and the ISO_IMAGE alias satisfy the common settings", lint.errors.length === 0);
st.model.items.push({ id: "c2", type: "cluster", name: "empty", cfg: { clu_type: "k3s", clu_rel: "stable", mydomain: "lab" }, parent: null });
st.model.items.push({ id: "a1", type: "addon", comp: "grafana", section: "grafana", parent: "c1" });
st.model.items.push({ id: "a2", type: "addon", comp: "rancher", section: "rancher", parent: null });
st.model.items[2].cfg.myip = "10.0.0.1";
lint = JSON.parse(JSON.stringify(sandbox.lintLab(st.model)));
check("lint: a Kubernetes cluster with no VM is an error", lint.errors.some((e) => e.includes("Kubernetes cluster empty has no VM")));
check("lint: an add-on where its layers do not allow it is an error", lint.errors.some((e) => e.startsWith("grafana cannot be attached")));
check("lint: two VMs with one IP is an error", lint.errors.some((e) => e.includes("same myip 10.0.0.1")));
check("lint: an add-on's required field is an error", lint.errors.some((e) => e.includes("rancher: rancher_password")));
check("lint: add-on settings nothing lists are a warning", lint.warnings.some((w) => w.includes("Add-on rancher has settings")));

const labIn = {
  common: { SOURCE_IMAGE: "x.qcow2", VM_MEM: 2048 },
  nodes: {
    "vm1": { myip: "10.0.0.1", kcluster: "k1", addons: ["grafana"] },
    "vm2": { myip: "10.0.0.2", kcluster: "gone", addons: [{ mariadb: { db_name: "x" } }] },
  },
  kclusters: { k1: { clu_type: "rke2", clu_rel: "stable", mydomain: "lab", addons: ["rancher"] } },
  rancher: { rancher_password: "p" },
  mariadb: {},
  pxe: { pxe_dhcp_mode: "off" },
  cluster: { clu_name: "old" },
};
const m2 = sandbox.decompileLab(JSON.parse(JSON.stringify(labIn)));
check("load: VMs join the Kubernetes cluster their kcluster names",
  m2.items.find((i) => i.name === "vm1").parent === m2.items.find((i) => i.name === "k1").id);
check("load: an unknown kcluster is kept on the VM", m2.items.find((i) => i.name === "vm2").cfg.kcluster === "gone");
check("load: pxe goes under Common settings", m2.items.some((i) => i.comp === "__pxe" && i.parent === null));
const labOut = JSON.parse(JSON.stringify(sandbox.compileLab(m2)));
check("load: compileLab gives the loaded lab back", JSON.stringify(labOut.nodes) === JSON.stringify(labIn.nodes)
  && JSON.stringify(labOut.kclusters) === JSON.stringify(labIn.kclusters) && labOut.rancher.rancher_password === "p"
  && JSON.stringify(labOut.pxe) === JSON.stringify(labIn.pxe));
check("load: keys the canvas does not model are kept", labOut.cluster.clu_name === "old");

vm.runInContext(fs.readFileSync("webui/htdocs/vendor/js-yaml.min.js", "utf8"), sandbox);
sandbox.window.jsyaml = sandbox.jsyaml;
check("parseLab: JSON", sandbox.parseLab('{"common": {"VM_MEM": 1}}').common.VM_MEM === 1);
check("parseLab: YAML", sandbox.parseLab("common:\n  VM_MEM: 1\nnodes: {}\n").common.VM_MEM === 1);
let threw = false;
try { sandbox.parseLab("[1, 2]"); } catch (e) { threw = true; }
check("parseLab: a list is not a lab definition", threw);
threw = "";
try { sandbox.parseLab("{ broken"); } catch (e) { threw = e.name; }
check("parseLab: broken JSON reports the JSON error, not a YAML one", threw === "SyntaxError");

// -- embedding (Rodeo Builder hand-off) --------------------------------
check("embed: origin accepted inside a frame",
  sandbox.embedOrigin("?embed=1&origin=http%3A%2F%2Flocalhost%3A8000", true) === "http://localhost:8000");
check("embed: not embedded outside a frame", sandbox.embedOrigin("?embed=1&origin=https%3A%2F%2Fa.example", false) === "");
check("embed: an origin with a path is refused", sandbox.embedOrigin("?embed=1&origin=https%3A%2F%2Fa.example%2Fx", true) === "");
check("embed: a non-http origin is refused", sandbox.embedOrigin("?embed=1&origin=javascript%3Aalert(1)", true) === "");
check("embed: no embed=1, no embedding", sandbox.embedOrigin("?origin=https%3A%2F%2Fa.example", true) === "");
const pw = sandbox.passwordFields([
  { sections: { kclusters: { fields: [{ name: "harvester_token", type: "password" }, { name: "clu_type", type: "string" }] } } },
  { section: "rancher", fields: [{ name: "rancher_password", type: "password" }] },
]);
check("embed: password fields found in every schema", pw.has("harvester_token") && pw.has("rancher_password") && !pw.has("clu_type"));
const redacted = sandbox.withSecretPlaceholders({
  kclusters: { h: { clu_type: "harvester", harvester_token: "s3cret" } },
  rancher: { rancher_password: "p", rancher_shorthn: "r" },
  other: { rancher_password: "" },
  kept: { rancher_password: "??mine" },
}, pw);
check("embed: password values become ??<field> placeholders",
  redacted.kclusters.h.harvester_token === "??harvester_token" && redacted.rancher.rancher_password === "??rancher_password");
check("embed: other values, empty values and existing placeholders are kept",
  redacted.kclusters.h.clu_type === "harvester" && redacted.rancher.rancher_shorthn === "r"
  && redacted.other.rancher_password === "" && redacted.kept.rancher_password === "??mine");
check("embed: no secret left in the sent lab", !JSON.stringify(redacted).includes("s3cret"));
const b64 = Buffer.from('{"common": {"VM_MEM": 2}}').toString("base64").replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
check("?lab=: base64url lab decoded", sandbox.decodeLabParam(b64).common.VM_MEM === 2);
{
  // setupEmbed: wires the send button and accepts labinabox:load only from the parent at that origin.
  const fake = () => ({ hidden: false, after() {}, addEventListener() {} });
  const savedDoc = sandbox.document, savedWin = sandbox.window, savedLoad = sandbox.loadLab, savedToast = sandbox.toast;
  const handlers = [];
  const parent = {};
  const loaded = [];
  sandbox.document = { querySelector: fake, querySelectorAll: () => [], createElement: fake, addEventListener: () => {} };
  sandbox.window = { parent, addEventListener: (t, h) => { if (t === "message") handlers.push(h); } };
  sandbox.loadLab = (lab) => loaded.push(lab);
  sandbox.toast = () => {};
  sandbox.setupEmbed("https://rodeo.example");
  const send = (ev) => handlers.forEach((h) => h(ev));
  send({ origin: "https://evil.example", source: parent, data: { type: "labinabox:load", lab: { nodes: {} } } });
  send({ origin: "https://rodeo.example", source: {}, data: { type: "labinabox:load", lab: { nodes: {} } } });
  send({ origin: "https://rodeo.example", source: parent, data: { type: "other", lab: { nodes: {} } } });
  check("embed: load from another origin, source or type is ignored", loaded.length === 0);
  send({ origin: "https://rodeo.example", source: parent, data: { type: "labinabox:load", lab: { nodes: { a: {} } } } });
  check("embed: load from the parent at the origin opens the lab", loaded.length === 1 && "a" in loaded[0].nodes);
  Object.assign(sandbox, { document: savedDoc, window: savedWin, loadLab: savedLoad, toast: savedToast });
}

// -- version matrix (mirrors 77_version_matrix_test.py) ----------------
{
  const M = { x_version: [
    { version: "2.13", kubernetes: { rke2: { min: "1.32", max: "1.34" }, k3s: { min: "1.32" } }, os: ["sle15sp7", "sles16.0"] },
    { version: "2.12" },
  ] };
  const vi = (...a) => sandbox.versionIssues(M, ...a);
  check("versions: suggestions newest first", JSON.stringify(sandbox.versionSuggestions(M, "x_version")) === '["2.13","2.12"]');
  check("versions: --version and v ignored", sandbox.versionMatches("v1.20", "--version v1.20.2"));
  check("versions: prefix does not match a longer minor", !sandbox.versionMatches("2.1", "2.13.0"));
  check("versions: empty value not checked", vi({ x_version: "" }).length === 0);
  check("versions: unknown version warned", vi({ x_version: "2.11.0" }).length === 1);
  check("versions: in range is clean", vi({ x_version: "2.13.1" }, "rke2", "v1.33.2+rke2r1", ["sle15sp7"]).length === 0);
  check("versions: above max warned", vi({ x_version: "2.13.1" }, "rke2", "v1.35.0+rke2r1").length === 1);
  check("versions: below min warned", vi({ x_version: "2.13.1" }, "rke2", "v1.31.0+rke2r1").length === 1);
  check("versions: open max not checked", vi({ x_version: "2.13" }, "k3s", "v1.40.0+k3s1").length === 0);
  check("versions: channel not range-checked", vi({ x_version: "2.13" }, "rke2", "stable").length === 0);
  check("versions: undeclared clu_type warned", vi({ x_version: "2.13" }, "harvester", "stable").length === 1);
  check("versions: undeclared OS warned once", vi({ x_version: "2.13" }, "", "", ["slem5.5", "sle15sp7", "slem5.5"]).length === 1);
  check("versions: same sentence as the Python side",
    vi({ x_version: "2.13.1" }, "rke2", "v1.35.0+rke2r1")[0] === "x_version 2.13 supports Kubernetes 1.32–1.34 on rke2, the kcluster's clu_rel is 'v1.35.0+rke2r1'");

  // lintLab warns for an add-on on a Kubernetes cluster outside the matrix.
  const saved = { cache: st.schemaCache, base: st.base };
  st.schemaCache = { install_x: { fields: [], capabilities: { layers: ["kubernetes"], versions: M } } };
  st.base = null;
  const model = { common: {}, addonCfg: { x: { x_version: "2.13" } }, extra: {}, seq: 3, items: [
    { id: "c1", type: "cluster", name: "k1", parent: null, cfg: { clu_type: "rke2", clu_rel: "v1.36.0+rke2r1" } },
    { id: "n2", type: "node", name: "vm1", parent: "c1", cfg: {} },
    { id: "a3", type: "addon", comp: "install_x", section: "x", parent: "c1" },
  ] };
  const w = sandbox.lintLab(model).warnings;
  check("versions: lintLab warns with the placement", w.some((t) => t.startsWith("Add-on x on Kubernetes cluster k1: x_version 2.13 supports")));
  st.schemaCache = saved.cache; st.base = saved.base;
}

if (failures) {
  console.error(failures + " check(s) failed");
  process.exit(1);
}
console.log("all app.js smoke checks passed");
