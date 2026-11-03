// Minimal ANSI SGR -> HTML converter, shared by status.html and app.js's own job-log view: both show the
// exact same escape sequences lab_creation.py/setup_lab.py print (_RED/_GREEN/_ORANGE/_WHITE/_RESET etc.),
// so a viewer sees the log colored the way it looks in a real terminal instead of raw "\x1b[1;91m" noise.
// Deliberately small: only SGR color/bold/reset, the only codes this project's own output ever emits.
(function (global) {
  "use strict";

  var STANDARD = ["#000000", "#cc0000", "#4e9a06", "#c4a000", "#3465a4", "#75507b", "#06989a", "#d3d7cf",
    "#555753", "#ef2929", "#8ae234", "#fce94f", "#729fcf", "#ad7fa8", "#34e2e2", "#eeeeec"];

  // The standard xterm 256-color formula: 0-15 are the ANSI colors above, 16-231 a 6x6x6 cube, 232-255 a
  // grayscale ramp. Computed, not a 256-row table, since the formula is exact and fixed by the spec.
  function xterm256(n) {
    if (n < 16) return STANDARD[n];
    if (n > 231) { var g = 8 + (n - 232) * 10; return rgbHex(g, g, g); }
    var i = n - 16, r = Math.floor(i / 36), gr = Math.floor((i % 36) / 6), b = i % 6;
    var lvl = function (v) { return v === 0 ? 0 : 55 + v * 40; };
    return rgbHex(lvl(r), lvl(gr), lvl(b));
  }
  function rgbHex(r, g, b) {
    var h = function (v) { return ("0" + v.toString(16)).slice(-2); };
    return "#" + h(r) + h(g) + h(b);
  }
  function escapeHtml(s) {
    return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }

  // `text` (raw log text, possibly containing "\x1b[...m" SGR sequences) -> HTML string with <span>s for
  // color/bold. Every plain-text chunk is HTML-escaped before it is ever placed next to a tag.
  function ansiToHtml(text) {
    // fg/bold: the style escape codes have set so far, applied to the NEXT text chunk. openFg/openBold:
    // the style of the <span> currently open in `out`, or null when none is. Kept apart so two escape
    // codes with no text between them (a common pattern: reset immediately followed by a new color)
    // never open and close an empty <span>.
    var out = [], fg = null, bold = false, openFg = null, openBold = false, open = false;
    var re = /\x1b\[([0-9;]*)m/g, last = 0, m;
    function emit(chunk) {
      if (!chunk) return;
      if (fg !== openFg || bold !== openBold) {
        if (open) out.push("</span>");
        open = !!(fg || bold);
        if (open) out.push('<span style="' + (fg ? "color:" + fg + ";" : "") + (bold ? "font-weight:700" : "") + '">');
        openFg = fg; openBold = bold;
      }
      out.push(escapeHtml(chunk));
    }
    while ((m = re.exec(text))) {
      emit(text.slice(last, m.index));
      var codes = m[1] === "" ? [0] : m[1].split(";").map(Number);
      for (var i = 0; i < codes.length; i++) {
        var c = codes[i];
        if (c === 0) { fg = null; bold = false; }
        else if (c === 1) bold = true;
        else if (c === 22) bold = false;
        else if (c === 39) fg = null;
        else if (c >= 30 && c <= 37) fg = STANDARD[c - 30];
        else if (c >= 90 && c <= 97) fg = STANDARD[8 + c - 90];
        else if (c === 38 && codes[i + 1] === 5) { fg = xterm256(codes[i + 2]); i += 2; }
      }
      last = re.lastIndex;
    }
    emit(text.slice(last));
    if (open) out.push("</span>");
    return out.join("");
  }

  global.ansiToHtml = ansiToHtml;
})(window);
