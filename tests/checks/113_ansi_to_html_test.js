// DOM-free functional smoke test for webui/htdocs/ansi.js's ansiToHtml() — loaded via node's vm module
// with a minimal `window` stub, no jsdom/npm dependency. The escape sequences come from
// libs/lab_creation.py through LAB_ANSI, set by 113_ansi_to_html.sh: this file keeps no copy of them,
// so a constant that changes there changes what is tested here. Run from 113_ansi_to_html.sh, in its
// own container — see tests/run_tests.sh.
"use strict";
const fs = require("fs");
const vm = require("vm");

const src = fs.readFileSync("webui/htdocs/ansi.js", "utf8");
const sandbox = { window: {} };
vm.createContext(sandbox);
vm.runInContext(src, sandbox);
const ansiToHtml = sandbox.window.ansiToHtml;

let pass = 0, fail = 0;
function check(desc, cond) {
  if (cond) { pass++; } else { fail++; console.log("FAIL:", desc); }
}

// libs/lab_creation.py's own constants, as the shell wrapper read them. Python writes the escape as the
// two-character "\033", so decode it to a real ESC before anything renders it.
const C = {};
for (const [name, raw] of Object.entries(JSON.parse(process.env.LAB_ANSI || "{}"))) {
  C[name] = raw.replace(/\\033|\\x1b|\\e/g, "\x1b");
}

check("the constants arrived from lab_creation.py and decoded to real CSI sequences",
  Object.keys(C).length >= 2 && typeof C.RESET === "string"
  && Object.values(C).every((v) => v.startsWith("\x1b[") && v.endsWith("m")));

// Every constant the project emits has to render, so one added to lab_creation.py is covered without
// editing this file. The named cases below additionally pin the colour each one resolves to.
for (const name of Object.keys(C)) {
  if (name === "RESET") { continue; }
  const out = ansiToHtml(C[name] + "x" + C.RESET);
  check("_" + name + " renders as one styled span, with no escape left in the output",
    /^<span style="[^"]+">x<\/span>$/.test(out) && !out.includes("\x1b"));
}

check("plain text with no escapes passes through unchanged",
  ansiToHtml("hello world") === "hello world");

check("a bold bright-red run becomes a styled span, closed before the reset",
  ansiToHtml(C.RED + "FAILED" + C.RESET + " rest") ===
  '<span style="color:#ef2929;font-weight:700">FAILED</span> rest');

check("a bold bright-green run",
  ansiToHtml(C.GREEN + "ok" + C.RESET) === '<span style="color:#8ae234;font-weight:700">ok</span>');

check("the 256-colour orange (38;5;208) resolves via the xterm cube formula",
  ansiToHtml(C.ORANGE + "warn" + C.RESET) === '<span style="color:#ff8700;font-weight:700">warn</span>');

check("bold yellow (1;33), the one constant no case covered before",
  ansiToHtml(C.YELLOW + "warn" + C.RESET) === '<span style="color:#c4a000;font-weight:700">warn</span>');

check("bold bright-white",
  ansiToHtml(C.WHITE + "RULE" + C.RESET) === '<span style="color:#eeeeec;font-weight:700">RULE</span>');

check("an unterminated color run still closes its span at the end of the string",
  ansiToHtml(C.RED + "unterminated") === '<span style="color:#ef2929;font-weight:700">unterminated</span>');

check("HTML-special characters in plain text are escaped",
  ansiToHtml("<script>&amp;") === "&lt;script&gt;&amp;amp;");

check("HTML-special characters inside a colored span are escaped too",
  ansiToHtml(C.RED + "<b>x</b>" + C.RESET) === '<span style="color:#ef2929;font-weight:700">&lt;b&gt;x&lt;/b&gt;</span>');

check("back-to-back colors without intervening text never emit an empty span",
  !ansiToHtml(C.RED + C.GREEN + "x" + C.RESET).includes("></span>"));

check("real multi-code sequences (e.g. 1;91) from this project's own output round-trip correctly",
  ansiToHtml("plain " + C.RED + "red" + C.RESET + " plain " + C.GREEN + "green" + C.RESET) ===
  'plain <span style="color:#ef2929;font-weight:700">red</span> plain ' +
  '<span style="color:#8ae234;font-weight:700">green</span>');

if (fail) {
  console.log(fail + " check(s) failed");
  process.exit(1);
}
console.log("all ansiToHtml checks passed (" + pass + ")");
