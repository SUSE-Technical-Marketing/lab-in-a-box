// Runs the static apiGet() that scripts/build-cube-static.py injects into a
// built page (argv[2]) in a vm sandbox, and checks each action answers from
// the page's embedded data. Run from 66_static_webui_build.sh, in its own
// container — see tests/run_tests.sh.
"use strict";
const fs = require("fs");
const vm = require("vm");

const page = fs.readFileSync(process.argv[2], "utf8");
const dataBlock = page.match(/<script id="static-api-data" type="application\/json">([\s\S]*?)<\/script>/);
const codeBlock = page.match(/<script id="static-api-data"[\s\S]*?<\/script>\s*<script>([\s\S]*?)<\/script>/);
if (!dataBlock || !codeBlock) {
  console.error("FAIL: static API script blocks not found in the page");
  process.exit(1);
}

const sandbox = {
  window: {},
  document: { getElementById: (id) => (id === "static-api-data" ? { textContent: dataBlock[1] } : null) },
};
vm.createContext(sandbox);
vm.runInContext(codeBlock[1], sandbox, { filename: "static-api" });
const apiGet = sandbox.window.apiGet;
const data = JSON.parse(dataBlock[1]);
const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);

let failures = 0;
function check(desc, cond) {
  if (!cond) { failures++; console.error("FAIL: " + desc); }
}

(async () => {
  const comps = await apiGet("components");
  check("components answers the embedded list", same(comps, data.components));
  const name = comps.components[0].name;
  const sc = await apiGet("schema", { name });
  check("schema answers the embedded schema of " + name, same(sc, data.schemas[name]));
  sc.fields.push({ name: "extra", type: "string" });
  check("schema answers a copy, not the embedded object", same(await apiGet("schema", { name }), data.schemas[name]));
  check("base answers the embedded base schema", same(await apiGet("base"), data.base));
  check("status reports unavailable", (await apiGet("status")).available === false);
  await apiGet("schema", { name: "install_does_not_exist" }).then(
    () => check("unknown add-on is rejected", false), () => {});
  await apiGet("validate").then(
    () => check("a server-only action is rejected", false), () => {});
  if (failures) {
    console.error(failures + " check(s) failed");
    process.exit(1);
  }
})().catch((e) => { console.error("FAIL: " + e.stack); process.exit(1); });
