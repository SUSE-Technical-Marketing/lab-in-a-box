// DOM-free functional smoke test for webui/htdocs/app.js's pure logic
// (validateFieldValue, applyFieldDefault) — loaded via node's vm module with
// minimal document/window stubs, no jsdom/npm dependency. Run from
// 07_webui_js.sh, in its own container — see tests/run_tests.sh.
"use strict";
const fs = require("fs");
const vm = require("vm");

const src = fs.readFileSync("webui/htdocs/app.js", "utf8");
// Enough of an element for the schema renderer to build a tree and for the test to read it back: walk() only
// creates elements, sets a class or text, appends and (for an empty group) removes.
function makeElement(tag) {
  return {
    tagName: String(tag).toUpperCase(), className: "", textContent: "", childNodes: [], dataset: {},
    style: {}, value: "", listeners: {},
    get childElementCount() { return this.childNodes.length; },
    // A real <select> exposes its <option> children as .options, which applyFieldDefault() walks.
    get options() { return this.childNodes.filter((c) => c && c.tagName === "OPTION"); },
    // Only the one use app.js makes of innerHTML: assigning "" to empty an element.
    get innerHTML() { return ""; },
    set innerHTML(value) { if (String(value) === "") this.childNodes = []; },
    appendChild(child) { this.childNodes.push(child); return child; },
    removeChild(child) { this.childNodes = this.childNodes.filter((c) => c !== child); return child; },
    setAttribute() {},
    // Listeners are recorded rather than dropped, so a check can drive a control the way a person does:
    // choosing a value and letting the change event do its work. fire() is this stub's dispatchEvent.
    addEventListener(type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn); },
    fire(type) { (this.listeners[type] || []).forEach((fn) => fn({ target: this })); },
    // Enough of a selector engine for "input" and "input, select": every descendant of those tags. The real
    // one matches far more, and a check that needs more than this would be testing the stub.
    querySelectorAll(sel) {
      const want = String(sel).split(",").map((t) => t.trim().toUpperCase()).filter(Boolean);
      const out = [];
      const visit = (node) => {
        (node.childNodes || []).forEach((child) => {
          if (child && child.tagName) {
            if (want.indexOf(child.tagName) >= 0) out.push(child);
            visit(child);
          }
        });
      };
      visit(this);
      return out;
    },
    querySelector(sel) { return this.querySelectorAll(sel)[0] || null; },
  };
}

// In-memory localStorage: a real browser's is same-origin and synchronous, which this is close
// enough to for persistDraft()/readDraft() — the only two functions under test that touch it.
function makeLocalStorage() {
  const store = {};
  return {
    _store: store,
    getItem: (k) => (k in store ? store[k] : null),
    setItem: (k, v) => { store[k] = String(v); },
    removeItem: (k) => { delete store[k]; },
  };
}

const sandbox = {
  document: {
    querySelectorAll: () => [], addEventListener: () => {}, createElement: makeElement,
    createTextNode: (text) => ({ nodeType: 3, textContent: String(text), childNodes: [] }),
    createDocumentFragment: () => makeElement("fragment"),
  },
  window: { addEventListener: () => {} },
  localStorage: makeLocalStorage(),
  // A page always has one; serverLock() reads the scheme off it to decide whether the login endpoint can work.
  location: { protocol: "https:", search: "" },
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

// -- list-of-items widget (item_fields): rows <-> items --------------------
{
  const field = { name: "ssh_authorized_keys", type: "array", item_shorthand: "key",
    item_fields: [{ name: "key", required: true }, { name: "user" }] };
  const rows = sandbox.itemsToRows(["ssh-ed25519 AAAA a@b", { key: "https://github.com/bob.keys", user: "bob" }], field);
  check("items: a plain string becomes a row with only the shorthand field",
    rows[0].key === "ssh-ed25519 AAAA a@b" && rows[0].user === undefined);
  check("items: a mapping keeps its fields", rows[1].key === "https://github.com/bob.keys" && rows[1].user === "bob");
  const items = sandbox.rowsToItems([{ key: " ssh-ed25519 AAAA a@b ", user: "" },
    { key: "/root/team.pub", user: "alice" }, { key: "", user: "carol" }], field);
  check("items: a row with only the key is written as the plain string", items[0] === "ssh-ed25519 AAAA a@b");
  check("items: a row with a user is written as a mapping",
    items[1].key === "/root/team.pub" && items[1].user === "alice");
  check("items: a row missing its required key is dropped", items.length === 2);
  check("items: no rows is no value", sandbox.rowsToItems([], field) === undefined);
  check("readWidget: an items widget reads its rows back as items",
    JSON.stringify(sandbox.readWidget({ value: JSON.stringify([{ key: "/k.pub", user: "" }]),
      dataset: { ftype: "items" }, _field: field })) === JSON.stringify(["/k.pub"]));
}

// -- Validate with credentials: passphrase asked again when wrong, 3 wrong ends it --
const credentialChecks = (async () => {
  const lab = { common: {}, nodes: { a: { cloud_account: "aws-main" } } };
  check("usesCloudAccount: a node's cloud_account counts", sandbox.usesCloudAccount(lab) === true);
  check("usesCloudAccount: no cloud_account anywhere", sandbox.usesCloudAccount({ common: {}, nodes: { a: {} } }) === false);

  const answers = (list) => { const calls = []; return { calls, call: async (p) => { calls.push(p); return list.shift(); } }; };
  let s = answers([{ need_passphrase: true }, { wrong_passphrase: true }, { ok: true, output: "fine" }]);
  const asked = [];
  let r = await sandbox.validateWithCredentials(s.call, async (failed) => { asked.push(failed); return "pw" + failed; });
  check("credentials: asked when needed, again after a wrong one, then the answer",
    r.ok === true && JSON.stringify(s.calls) === JSON.stringify(["", "pw0", "pw1"]) && JSON.stringify(asked) === "[0,1]");

  s = answers([{ need_passphrase: true }, { wrong_passphrase: true }, { wrong_passphrase: true }, { wrong_passphrase: true },
    { ok: true }]);
  r = await sandbox.validateWithCredentials(s.call, async () => "bad");
  check("credentials: three wrong passphrases end it with a failure",
    r.ok === false && /wrong 3 times/.test(r.output) && s.calls.length === 4);

  s = answers([{ need_passphrase: true }]);
  r = await sandbox.validateWithCredentials(s.call, async () => null);
  check("credentials: cancelling the dialog ends it", r.ok === false && /cancelled/.test(r.output));

  s = answers([{ ok: true, output: "no secrets" }]);
  r = await sandbox.validateWithCredentials(s.call, async () => { throw new Error("must not ask"); });
  check("credentials: plain credentials are validated without asking", r.ok === true && s.calls.length === 1);
})();

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
// A per-node/repeatable field with a schema default (KUBERNETES_NODE_TYPE)
// must not appear in the saved lab.json for a node whose role was never
// chosen: the select's own .value stays unset, only its labeling shows the
// default.
{
  const field = { name: "KUBERNETES_NODE_TYPE", type: "string", default: "server" };
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
st.model.common = { SOURCE_IMAGE: "x.qcow2" };
st.model.items.forEach((i) => { if (i.type === "node") i.cfg.VM_MEM = 2048; });
lint = JSON.parse(JSON.stringify(sandbox.lintLab(st.model)));
check("lint: VMs' own values and the common SOURCE_IMAGE satisfy the common settings", lint.errors.length === 0);
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

// -- imageChoices: per-host source images, labelled "<host> - <file name>" ----
{
  const c = sandbox.imageChoices({ images_by_host: { nuc2: ["b.iso"], nuc1: ["b.iso", "a.qcow2"] },
                                   images: ["a.qcow2", "b.iso"] });
  check("imageChoices: one entry per host and image, ordered by host then name",
    JSON.stringify(c) === JSON.stringify([
      { value: "a.qcow2", label: "nuc1 - a.qcow2" },
      { value: "b.iso", label: "nuc1 - b.iso" },
      { value: "b.iso", label: "nuc2 - b.iso" }]));
  check("imageChoices: the value is the file name a lab stores, not the label",
    c.every((o) => !o.value.includes(" - ")));
  check("imageChoices: an older snapshot with only the flat list yields plain names",
    JSON.stringify(sandbox.imageChoices({ images: ["x.qcow2"] })) === JSON.stringify(["x.qcow2"]));
  check("imageChoices: no images anywhere yields an empty list",
    JSON.stringify(sandbox.imageChoices({})) === "[]");
}

// -- prefillAddons / applyUrlParams: ?addons= puts the named add-ons on the canvas ----------------------------------
// Async, so the summary below runs from its continuation: a top-level exit would report success before these resolve.
async function urlParamChecks() {
  const st = vm.runInContext("state", sandbox);
  const saved = { comps: st.components, items: st.model.items, seq: st.model.seq, loc: sandbox.location };
  const toasts = [];
  st.components = [{ name: "install_longhorn" }, { name: "install_rancher" }];
  st.model.items = [];
  st.model.seq = 0;
  sandbox.loadAddonSchema = async (comp) => ({ section: comp.replace("install_", "") });
  sandbox.renderCanvas = () => {};
  sandbox.refreshLab = () => {};
  sandbox.toast = (m) => toasts.push(m);
  sandbox.Option = function (text, value) { return { text, value, tagName: "OPTION" }; };

  const unknown = await sandbox.prefillAddons(["longhorn", "install_rancher", "longhorn", "nope"]);
  check("?addons=: a bare name and an install_ name both match, and each add-on is added once",
    st.model.items.length === 2
    && st.model.items.every((i) => i.type === "addon" && i.parent === null)
    && st.model.items.map((i) => i.comp).join(",") === "install_longhorn,install_rancher");
  check("?addons=: an add-on no component matches is reported, not added", unknown.join(",") === "nope");

  st.model.items = [];
  st.model.seq = 0;
  sandbox.location = { search: "?addons=longhorn,nope" };
  await sandbox.applyUrlParams();
  check("applyUrlParams: reads ?addons= and names the unknown ones in a toast",
    st.model.items.length === 1 && toasts.some((t) => t.includes("Unknown add-ons, not added: nope")));

  const lab = { common: { VM_MEM: 2 }, nodes: {} };
  const b64 = Buffer.from(JSON.stringify(lab)).toString("base64url");
  const loaded = [];
  sandbox.loadLab = (l) => loaded.push(l);
  sandbox.location = { search: "?lab=" + b64 };
  await sandbox.applyUrlParams();
  check("applyUrlParams: reads ?lab= and opens that lab", loaded.length === 1 && loaded[0].common.VM_MEM === 2);

  st.components = saved.comps;
  st.model.items = saved.items;
  st.model.seq = saved.seq;
  sandbox.location = saved.loc;
}

// -- shownFields / structuralCfg: a field's show_if dependency ------------
// The Kubernetes node fields declare show_if "kcluster" in lab_schema, so a
// VM that is in no Kubernetes cluster must not be offered them.
{
  const fields = [
    { name: "myip", type: "string" },
    { name: "KUBERNETES_NODE_TYPE", type: "string", show_if: "kcluster" },
    { name: "harvester_role", type: "string", show_if: "kcluster" },
  ];
  const names = (cfg) => sandbox.shownFields(fields, cfg).map((f) => f.name);
  check("show_if unmet: only the independent fields are shown",
    JSON.stringify(names({ myip: "10.0.0.1" })) === '["myip"]');
  check("show_if met: the dependent fields are shown too",
    names({ kcluster: "clu1" }).length === 3);
  check("show_if met by an empty value is not met",
    JSON.stringify(names({ kcluster: "" })) === '["myip"]');
  check("no cfg at all hides every dependent field",
    JSON.stringify(names(undefined)) === '["myip"]');
  check("a field without show_if is never filtered",
    sandbox.shownFields([{ name: "myip" }], {}).length === 1);

  const st = vm.runInContext("state", sandbox);
  const saved = st.model.items;
  st.model.items = [{ id: "c1", type: "cluster", name: "clu1", cfg: {}, parent: null }];
  const inCluster = { id: "n1", type: "node", cfg: { myip: "10.0.0.1" }, parent: "c1" };
  const loose = { id: "n2", type: "node", cfg: { myip: "10.0.0.2" }, parent: null };
  check("structuralCfg: a VM in a cluster block gets that cluster as its kcluster",
    sandbox.structuralCfg(inCluster).kcluster === "clu1");
  check("structuralCfg: a VM outside every cluster has no kcluster",
    sandbox.structuralCfg(loose).kcluster === undefined);
  check("a clustered VM is offered the Kubernetes fields",
    sandbox.shownFields(fields, sandbox.structuralCfg(inCluster)).length === 3);
  check("a VM outside a cluster is not offered the Kubernetes fields",
    sandbox.shownFields(fields, sandbox.structuralCfg(loose)).length === 1);
  st.model.items = saved;
}

// -- walk(): an add-on's schema rendered as a form ------------------------
// A schema's "capabilities" object describes the add-on; it holds no fields, so rendering it left a group heading
// with nothing under it for targets, layers, aux_services and versions.
{
  const headings = (node) => {
    const out = [];
    const visit = (e) => {
      if (e.className === "group-head") out.push(e.childNodes.length ? e.textContent : e.textContent);
      (e.childNodes || []).forEach(visit);
    };
    (node.childNodes || []).forEach(visit);
    return out;
  };
  const schema = {
    section: "demo", description: "A demo add-on",
    fields: [{ name: "demo_port", type: "port" }],
    capabilities: { targets: ["container"], layers: ["kubernetes"], requires_kubernetes: ["rke2"],
                    aux_services: [], versions: { demo_version: [{ version: "1.0" }] }, check: true },
  };
  const form = makeElement("div");
  sandbox.walk(schema, form, []);
  check("the capabilities block renders no headings at all", headings(form).length === 0);
  check("the add-on's own field is still rendered", form.childNodes.length === 1);

  // A group that does hold a field keeps its heading.
  const grouped = { fields: [], pxe: { description: "PXE", fields: [{ name: "pxe_bridge", type: "string" }] } };
  const form2 = makeElement("div");
  sandbox.walk(grouped, form2, []);
  check("a group with a field keeps its heading", headings(form2).includes("pxe"));

  // A group whose fields are all hidden renders nothing, so no bare heading is left behind.
  const hiddenOnly = { fields: [], legacy: { description: "old", fields: [{ name: "old_key", type: "string", hidden: true }] } };
  const form3 = makeElement("div");
  sandbox.walk(hiddenOnly, form3, []);
  check("a group whose only field is hidden is dropped, heading and all", form3.childNodes.length === 0);
}


// ── errors are visible, and a second Create shows the running job ─────────
// An error used to arrive as the same quiet grey note as a confirmation and vanish in 2.6 s. The create dialog
// also offered Create again while a job was already running for that lab, so the refusal only arrived as a toast
// after the password had been typed.
{
  const css = fs.readFileSync("webui/htdocs/style.css", "utf8");
  const html = fs.readFileSync("webui/htdocs/index.html", "utf8");
  check("an error has its own toast kind", /function toastError\(/.test(src) && /toast-error/.test(src));
  check("it is announced to a screen reader", /"role", kind === "error" \? "alert"/.test(src));
  check("it is held long enough to read a sentence", /kind === "error" \? 9000/.test(src));
  check("it is styled as an error, not as a note",
        /\.toast-error\{/.test(css) && /background:var\(--danger\)/.test(css));
  check("its text takes the page's own background colour, which contrasts in both themes",
        /\.toast-error\{[^}]*color:var\(--bg\)/.test(css));
  check("a long message wraps instead of being clipped",
        /white-space:pre-wrap/.test(css) && /max-width:min/.test(css));
  check("no failure path still reports as a plain note",
        !/catch \(e\) \{\s*toast\(/.test(src) && !/=> toast\(/.test(src));
  check("every message raised from a failure is an error",
        (src.match(/toastError\(/g) || []).length > 15);
  check("the create dialog looks for a job already running for this lab, saved or example",
        /const labId = example \|\| name \+ "\.json"/.test(src)
        && /job\.lab === labId && job\.state === "running"/.test(src));
  check("...and watches it instead of offering Create again", /watchJob\(running\.job/.test(src));
  check("a failed start is shown in the dialog too, where the reader is looking",
        /createMsg/.test(src) && /The lab was not started/.test(src));
  check("the dialog has somewhere to show it", /id="createMsg"/.test(html));
}

// ── draft persistence: a page refresh must not lose in-progress work ───────
{
  const store = sandbox.localStorage._store;
  check("isModelEmpty: a brand-new model is empty",
        sandbox.isModelEmpty({ common: {}, items: [], extra: {} }));
  check("isModelEmpty: a model with one item is not",
        !sandbox.isModelEmpty({ common: {}, items: [{ id: "n1" }], extra: {} }));
  check("isModelEmpty: a model with only a common field is not",
        !sandbox.isModelEmpty({ common: { mydomain: "x" }, items: [], extra: {} }));

  sandbox.persistDraft({ common: {}, items: [{ id: "n1", type: "node", name: "x" }], addonCfg: {}, extra: {}, seq: 1 });
  check("persistDraft: a non-empty model is saved",
        JSON.parse(store["labbuilder.draft"]).model.items.length === 1);

  sandbox.persistDraft({ common: {}, items: [], addonCfg: {}, extra: {}, seq: 0 });
  check("persistDraft: an emptied model clears the saved draft rather than saving an empty one",
        !("labbuilder.draft" in store));

  check("readDraft: a missing key reads back as null",
        sandbox.readDraft("labbuilder.draft") === null);
  store["labbuilder.draft"] = "{not json";
  check("readDraft: corrupt JSON reads back as null instead of throwing",
        sandbox.readDraft("labbuilder.draft") === null);
  delete store["labbuilder.draft"];

  check("startFresh backs up the live draft before clearing it, so Restore can undo it",
        /function startFresh\(\)[\s\S]*?localStorage\.setItem\(LAST_DRAFT_KEY, current\)/.test(src));
  check("startFresh confirms before discarding a non-empty lab",
        /function startFresh\(\)[\s\S]{0,400}window\.confirm\("Start a fresh lab/.test(src));
  check("restoreDraft confirms before replacing a non-empty lab",
        /function restoreDraft\(\)[\s\S]{0,400}window\.confirm\("Restore the previous lab/.test(src));
  check("renderCanvas persists the draft after every canvas mutation",
        /function renderCanvas\(\)[\s\S]*?persistDraft\(\);\n\}/.test(src));
  check("the model restored on load is loaded before the catalogue/base fetches, not after",
        /const draft = readDraft\(DRAFT_KEY\);[\s\S]*?loadComponents\(\)/.test(src));

  // index-static.html/lab-builder-static.html are build artifacts (gitignored, scripts/build-cube-static.py's
  // output) that don't exist on a clean checkout — only index.html, the real source, is checked here.
  const htmlFiles = ["index.html"];
  htmlFiles.forEach((f) => {
    const h = fs.readFileSync("webui/htdocs/" + f, "utf8");
    check(f + ": has a Start fresh button", /id="startFreshBtn"/.test(h));
    check(f + ": has a Restore button, disabled until there is a backup to restore",
          /id="restoreBtn"[^>]*disabled/.test(h));
  });
}

// $() resolves through document.querySelector, so a check that drives real UI code needs one. Each selector
// gets its own element, remembered, with the <dialog> methods app.js calls on them.
function stubNodes() {
  const nodes = {};
  sandbox.document.querySelector = (sel) => {
    if (!nodes[sel]) {
      const node = makeElement("div");
      node.open = false;
      node.hidden = false;
      node.required = false;
      node.placeholder = "";
      node.showModal = () => { node.open = true; };
      node.close = () => { node.open = false; };
      nodes[sel] = node;
    }
    return nodes[sel];
  };
  return nodes;
}

// -- Simplify opens the dialog, and only when the server answered ----------
// The button went dead once because openWizard() called a helper that does not exist: nothing throws at parse
// time, node --check passes, and the click does nothing. So this runs the real click through the real helpers.
const wizardChecks = async () => {
  const nodes = stubNodes();
  const answer = { workflows: [{ name: "vm", title: "VMs", description: "plain virtual machines" }] };
  let asked = "";
  sandbox.fetch = async (url) => {
    asked = String(url);
    return { ok: true, status: 200, json: async () => answer };
  };
  await sandbox.openWizard();
  check("Simplify opens the step-by-step dialog", nodes["#wizardPanel"].open === true);
  check("...having asked the public API for the workflows, so no login is demanded",
        asked.startsWith("api?") && asked.includes("action=wizard"));
  check("...and rendered them into the panel", nodes["#wizardBody"].childNodes.length > 0);
  sandbox.closeWizard();
  check("closing it leaves the dialog shut", nodes["#wizardPanel"].open === false);

  sandbox.fetch = async () => ({ ok: false, status: 500, json: async () => ({ error: "nope" }) });
  await sandbox.openWizard();
  check("a server that refuses leaves the dialog shut rather than opening an empty one",
        nodes["#wizardPanel"].open === false);
};

// -- the version a cluster installs is offered per distribution -------------
// clu_rel is one field serving four distributions that name their releases differently, so a single list would
// be wrong for three of them. The browser is the only side that knows which clu_type a row chose, so it is the
// side that narrows the list. Free text has to survive, or a release the server has not listed yet becomes
// uninstallable through the form.
const releaseChoiceChecks = () => {
  const options = {
    harvester: ["v1.9.1", "v1.8.2"],
    rke2: ["stable", "latest", "v1.37.2+rke2r1"],
  };
  const container = makeElement("div");
  container.appendChild(sandbox.fieldRow(
    { name: "clu_type", type: "string", enum: ["rke2", "k3s", "vanilla", "harvester"] }, [], true));
  container.appendChild(sandbox.fieldRow(
    { name: "clu_rel", type: "string", enum_by: { on: "clu_type", options } }, [], true));
  sandbox.wireDependentFields(container);

  const inputs = container.querySelectorAll("input, select");
  const clu_type = inputs.find((i) => i._field && i._field.name === "clu_type");
  const value = inputs.find((i) => i._field && i._field.name === "clu_rel");
  const select = container.querySelectorAll("select").find((s) => s.className === "enum-by-select");
  const text = container.querySelectorAll("input").find((i) => i.className === "enum-by-custom");
  const listed = () => select.childNodes.map((o) => o.value).filter((v) => v && v !== "CUSTOM_VALUE");

  check("the version field is a dependent control, not a plain text box",
        !!select && !!text && value._dependsOn === "clu_type");

  clu_type.value = "harvester";
  clu_type.fire("change");
  check("choosing SUSE Virtualization offers its own releases",
        JSON.stringify(listed()) === JSON.stringify(options.harvester));
  check("...and not another distribution's", listed().indexOf("v1.37.2+rke2r1") < 0);

  clu_type.value = "rke2";
  clu_type.fire("change");
  check("choosing rke2 offers its channels first, then its releases",
        JSON.stringify(listed()) === JSON.stringify(options.rke2));

  select.value = "v1.37.2+rke2r1";
  select.fire("change");
  check("picking a release is what the form submits", value.value === "v1.37.2+rke2r1");

  select.value = "CUSTOM_VALUE";
  select.fire("change");
  text.value = "v1.99.0+rke2r1";
  text.fire("input");
  check("a release the server never listed can still be typed in", value.value === "v1.99.0+rke2r1");
  check("...and the free-text box is shown once it is being used", text.style.display === "block");

  clu_type.value = "k3s";
  clu_type.fire("change");
  check("a distribution the server listed nothing for leaves the field typeable rather than empty",
        listed().length === 0 && !!text);
};

// -- the step builder shows where you are and what the lab is becoming ------
// The questions used to arrive one after another with nothing to say how many there were or what had been
// answered. The strip across the top and the box beside the question are the whole of that, so they are driven
// here rather than asserted on source text.
// app.js declares its wizard state as a top-level const, which a vm context does not expose on the sandbox
// object, so it is reached by evaluating inside that context rather than through it.
const setWiz = (patch) => vm.runInContext(
  "Object.assign(wiz, " + JSON.stringify(patch) + ")", sandbox);
const getWiz = (expr) => vm.runInContext("JSON.stringify(" + expr + ")", sandbox);

const stepBuilderChecks = () => {
  const nodes = stubNodes();
  setWiz({ workflow: "kubernetes", index: 2, answers: { distribution: "rke2", nodes: "3" }, steps: [
    { name: "distribution", title: "Distribution", kind: "choice",
      options: [{ value: "rke2", label: "RKE2" }, { value: "k3s", label: "K3s" }] },
    { name: "nodes", title: "Nodes", kind: "count", min: 1, max: 9 },
    { name: "backend", title: "Where", kind: "choice",
      options: [{ value: "host:nuc6", label: "nuc6.mydemo.lab" }] },
    { name: "addons", title: "Add-ons", kind: "multi",
      options: [{ value: "rancher", label: "Rancher" }, { value: "longhorn", label: "SUSE Storage" }] },
  ] });

  sandbox.renderWizardStrip(true);
  const strip = nodes["#wizardSteps"];
  const chips = strip.childNodes;
  check("every question is a step in the strip", chips.length === 4);
  check("the strip names the questions", chips.map((c) => c.childNodes[1].textContent).join("|")
        === "Distribution|Nodes|Where|Add-ons");
  check("the answered ones are ticked", chips[0].className.includes("done") && chips[1].className.includes("done")
        && chips[0].childNodes[0].textContent === "✓");
  check("the current one is marked, not ticked",
        chips[2].className.includes("now") && chips[2].childNodes[0].textContent === "3");
  check("an answered step can be gone back to", chips[0].className.includes("can-go"));
  check("a question not yet reached cannot be jumped to",
        !chips[3].className.includes("can-go") && chips[3].disabled === true);
  check("the strip carries no answers, which the box beside the question already has",
        !chips.some((c) => (c.childNodes[1].textContent || "").includes("RKE2")));

  sandbox.renderWizardSoFar(true);
  const box = nodes["#wizardSoFar"];
  const rows = box.childNodes[1].childNodes;
  check("the box is headed as the lab so far", box.childNodes[0].textContent === "Your lab so far");
  check("an answer is shown by its own label, not the value stored",
        rows[0].childNodes[1].textContent === "RKE2");
  check("a typed answer is shown as typed", rows[1].childNodes[1].textContent === "3");
  check("the question being asked says so", rows[2].childNodes[1].textContent === "this question"
        && rows[2].childNodes[1].className.includes("pending"));
  check("a question still to come says so", rows[3].childNodes[1].textContent === "not yet");

  setWiz({ answers: { distribution: "rke2", nodes: "3", addons: ["rancher", "longhorn"] } });
  sandbox.renderWizardSoFar(true);
  check("several answers to one question read as a list",
        nodes["#wizardSoFar"].childNodes[1].childNodes[3].childNodes[1].textContent === "Rancher, SUSE Storage");

  setWiz({ answers: { distribution: "rke2", nodes: "3", addons: [] } });
  sandbox.renderWizardSoFar(true);
  check("choosing nothing is not shown as an answer",
        nodes["#wizardSoFar"].childNodes[1].childNodes[3].childNodes[1].className.includes("pending"));

  sandbox.renderWizardStrip(false);
  sandbox.renderWizardSoFar(false);
  check("both are out of the way where there is nothing to show",
        nodes["#wizardSteps"].hidden === true && nodes["#wizardSoFar"].hidden === true);

  check("the state really is the dialog's own, not a copy", getWiz("wiz.index") === "2");
  setWiz({ workflow: "", steps: [], answers: {}, index: 0 });
};

// -- the Create dialog asks for a passphrase only where one is needed -------
// "only if this lab uses encrypted credentials" put the question to the operator; the credentials list already
// carries each file's encryption flags, so it is a fact to read instead.
const passphraseChecks = async () => {
  const nodes = stubNodes();
  const listed = {
    credentials: [
      { name: "aws-plain", kind: "cloud", type: "aws", encrypted: false, encrypted_fields: [] },
      { name: "aws-locked", kind: "cloud", type: "aws", encrypted: true, encrypted_fields: [] },
      { name: "scc-partly", kind: "service", type: "scc", encrypted: false, encrypted_fields: ["regcode"] },
    ],
  };
  sandbox.fetch = async () => ({ ok: true, status: 200, json: async () => listed });

  await sandbox.setCreatePassphrase({ common: { cloud_account: "aws-plain" } });
  check("a lab whose credential is plaintext is not asked for a passphrase at all",
        nodes["#createPassRow"].hidden === true);

  await sandbox.setCreatePassphrase({ common: { cloud_account: "aws-locked" } });
  check("a lab naming an encrypted credential is asked, and told which one",
        nodes["#createPassRow"].hidden === false && nodes["#createPass"].required === true
        && nodes["#createPassNote"].textContent.includes("aws-locked"));

  await sandbox.setCreatePassphrase({ common: {}, smlm: { smlm_scc_account: "scc-partly" } });
  check("a per-field encrypted credential counts, wherever in the lab it is named",
        nodes["#createPassRow"].hidden === false
        && nodes["#createPassNote"].textContent.includes("scc-partly"));

  await sandbox.setCreatePassphrase({ common: { mydomain: "aws-plain.lab" }, nodes: { "n1": { myip: "10.0.0.1" } } });
  check("a lab naming no credential at all is not asked either",
        nodes["#createPassRow"].hidden === true);

  sandbox.fetch = async () => ({ ok: false, status: 401, json: async () => ({ error: "log in" }) });
  await sandbox.setCreatePassphrase({ common: { cloud_account: "aws-locked" } });
  check("a credentials list that cannot be read offers the field rather than deciding for the run",
        nodes["#createPassRow"].hidden === false && nodes["#createPass"].required === false);
};

// One stubbed document between them, so they take turns rather than overwrite each other's elements.
const domChecks = (async () => {
  await wizardChecks();
  await passphraseChecks();
  releaseChoiceChecks();
  stepBuilderChecks();
})();

Promise.all([urlParamChecks(), credentialChecks, domChecks]).then(() => {
  if (failures) {
    console.error(failures + " check(s) failed");
    process.exit(1);
  }
  console.log("all app.js smoke checks passed");
}, (e) => {
  console.error("FAIL: an async check threw: " + e.message);
  process.exit(1);
});
