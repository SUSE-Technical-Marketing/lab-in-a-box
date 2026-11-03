// DOM-free test of status.html's pure summary-parsing functions (stripAnsi/parseComponents/parseIssues).
// These never touch the DOM, so they are extracted straight out of the real <script> block by name and
// run in a bare vm context — no jsdom/npm dependency, no need to stub fetch/document/location just to
// exercise text parsing. Run from 114_status_page_components.sh, in its own container — see
// tests/run_tests.sh.
"use strict";
const fs = require("fs");
const vm = require("vm");

const html = fs.readFileSync("webui/htdocs/status.html", "utf8");

function extractFn(name) {
  const start = html.indexOf("function " + name + "(");
  if (start === -1) throw new Error("function " + name + " not found in status.html");
  // Balance braces from the first "{" after the signature to find the matching close.
  let i = html.indexOf("{", start), depth = 0, end = -1;
  for (; i < html.length; i++) {
    if (html[i] === "{") depth++;
    else if (html[i] === "}") { depth--; if (depth === 0) { end = i + 1; break; } }
  }
  if (end === -1) throw new Error("could not balance braces for " + name);
  return html.slice(start, end);
}

const src = [extractFn("stripAnsi"), extractFn("parseComponents"), extractFn("parseIssues"),
  extractFn("escapeHtmlLocal"), extractFn("extractAccessLines")].join("\n");
const sandbox = {};
vm.createContext(sandbox);
vm.runInContext(src, sandbox);

let pass = 0, fail = 0;
function check(desc, cond) {
  if (cond) { pass++; } else { fail++; console.log("FAIL:", desc); }
}

// A realistic LAB SUMMARY block, with the same ANSI wrapping print_summary() actually emits
// (color-escape + left-padded status + reset, see scripts/setup_lab.py's print_summary()).
const RED = "\x1b[1;91m", GREEN = "\x1b[1;92m", RESET = "\x1b[0m";
const summary = [
  "Nodes (3):",
  "  " + GREEN + "created " + RESET + " callisto.mydemoaws.lab, charon.mydemoaws.lab",
  "  " + RED + "FAILED  " + RESET + " pluto.mydemoaws.lab",
  "Clusters (1):",
  "  " + GREEN + "ok      " + RESET + " ai-gpu",
  "Addons (2):",
  "  " + GREEN + "ok              " + RESET + " ollama (cluster:ai-gpu), smlm (node:sol.mydemoaws.lab)",
  "  " + RED + "FAILED          " + RESET + " client_registration (node:mercury.mydemoaws.lab)",
  "Access:",
  "  sol.mydemoaws.lab — ssh root@1.2.3.4 (password: linux)",
  "  pluto.mydemoaws.lab — IP not resolvable yet (DNS not propagated, or not created)",
  "Warnings (1):",
  "  • could not add system(s) (mercury.mydemoaws.lab) to group 'terrestrial-planets'",
  "Errors (1):",
  "  • no system named 'mercury.mydemoaws.lab' found on the server",
].join("\n");
const plain = sandbox.stripAnsi(summary);

check("stripAnsi removes every escape sequence", !/\x1b/.test(plain));

const items = sandbox.parseComponents(plain);
check("parses both good nodes into their own entries",
  items.some((i) => i.kind === "node" && i.name === "callisto.mydemoaws.lab" && i.status === "created") &&
  items.some((i) => i.kind === "node" && i.name === "charon.mydemoaws.lab" && i.status === "created"));
check("parses the failed node", items.some((i) => i.kind === "node" && i.name === "pluto.mydemoaws.lab" && i.status === "FAILED"));
check("parses the cluster", items.some((i) => i.kind === "cluster" && i.name === "ai-gpu" && i.status === "ok"));
check("parses an addon with its scope split out",
  items.some((i) => i.kind === "addon" && i.name === "ollama" && i.scope === "cluster:ai-gpu" && i.status === "ok"));
check("parses the failed addon with its scope",
  items.some((i) => i.kind === "addon" && i.name === "client_registration" && i.scope === "node:mercury.mydemoaws.lab" && i.status === "FAILED"));
check("exactly 7 items total (3 nodes, 1 cluster, 3 addons: the 'ok' line splits into 2)", items.length === 7);

const issues = sandbox.parseIssues(plain);
check("one warning bullet captured", issues.warnings.length === 1 && issues.warnings[0].indexOf("mercury") !== -1);
check("one error bullet captured", issues.errors.length === 1 && issues.errors[0].indexOf("mercury") !== -1);
check("a name with nothing in Warnings/Errors has no matches",
  !issues.errors.concat(issues.warnings).some((l) => l.indexOf("pluto") !== -1));
check("the Access: section does not leak into parseComponents as addon entries",
  !items.some((i) => i.name.indexOf("sol.mydemoaws.lab") !== -1 || i.name.indexOf("ssh root@") !== -1));
check("...nor into parseIssues as warnings/errors",
  !issues.errors.concat(issues.warnings).some((l) => l.indexOf("ssh root@") !== -1));

const accessLines = sandbox.extractAccessLines(plain);
check("extractAccessLines gets exactly the two Access: lines, nothing from Nodes/Addons/Warnings",
  accessLines.length === 2 && accessLines[0].indexOf("sol.mydemoaws.lab") !== -1
  && accessLines[1].indexOf("pluto.mydemoaws.lab") !== -1);
check("extractAccessLines excludes the Warnings/Errors lines that follow",
  !accessLines.some((l) => l.indexOf("mercury") !== -1));

check("escapeHtmlLocal escapes the HTML-special characters", sandbox.escapeHtmlLocal("<a>&</a>") === "&lt;a&gt;&amp;&lt;/a&gt;");

if (fail) {
  console.log(fail + " check(s) failed");
  process.exit(1);
}
console.log("all status-page component-parsing checks passed (" + pass + ")");
