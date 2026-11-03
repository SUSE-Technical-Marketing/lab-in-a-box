// DOM-free functional smoke test for webui/htdocs/ansi.js's ansiToHtml() — loaded via node's vm module
// with a minimal `window` stub, no jsdom/npm dependency. Run from 113_ansi_to_html.sh, in its own
// container — see tests/run_tests.sh.
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

// The project's own real sequences (libs/lab_creation.py): _RED, _GREEN, _ORANGE (256-colour), _WHITE, _RESET.
const RED = "\x1b[1;91m", GREEN = "\x1b[1;92m", ORANGE = "\x1b[1;38;5;208m", WHITE = "\x1b[1;97m", RESET = "\x1b[0m";

check("plain text with no escapes passes through unchanged",
  ansiToHtml("hello world") === "hello world");

check("a bold bright-red run becomes a styled span, closed before the reset",
  ansiToHtml(RED + "FAILED" + RESET + " rest") ===
  '<span style="color:#ef2929;font-weight:700">FAILED</span> rest');

check("a bold bright-green run",
  ansiToHtml(GREEN + "ok" + RESET) === '<span style="color:#8ae234;font-weight:700">ok</span>');

check("the 256-colour orange (38;5;208) resolves via the xterm cube formula",
  ansiToHtml(ORANGE + "warn" + RESET) === '<span style="color:#ff8700;font-weight:700">warn</span>');

check("bold bright-white",
  ansiToHtml(WHITE + "RULE" + RESET) === '<span style="color:#eeeeec;font-weight:700">RULE</span>');

check("an unterminated color run still closes its span at the end of the string",
  ansiToHtml(RED + "unterminated") === '<span style="color:#ef2929;font-weight:700">unterminated</span>');

check("HTML-special characters in plain text are escaped",
  ansiToHtml("<script>&amp;") === "&lt;script&gt;&amp;amp;");

check("HTML-special characters inside a colored span are escaped too",
  ansiToHtml(RED + "<b>x</b>" + RESET) === '<span style="color:#ef2929;font-weight:700">&lt;b&gt;x&lt;/b&gt;</span>');

check("back-to-back colors without intervening text never emit an empty span",
  !ansiToHtml(RED + GREEN + "x" + RESET).includes("></span>"));

check("real multi-code sequences (e.g. 1;91) from this project's own output round-trip correctly",
  ansiToHtml("plain " + RED + "red" + RESET + " plain " + GREEN + "green" + RESET) ===
  'plain <span style="color:#ef2929;font-weight:700">red</span> plain ' +
  '<span style="color:#8ae234;font-weight:700">green</span>');

if (fail) {
  console.log(fail + " check(s) failed");
  process.exit(1);
}
console.log("all ansiToHtml checks passed (" + pass + ")");
