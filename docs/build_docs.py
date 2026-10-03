#!/usr/bin/env python3
"""Build the documentation site in docs/ from the HTML fragments in docs/src/.

Run from the repository root: python3 docs/build_docs.py
Output: docs/index.html (English), one page per translation, and docs/webui.html.
"""
import html
import re
import sys
from pathlib import Path

DOCS = Path(__file__).resolve().parent
SOURCE = DOCS / "src"

PAGES = [
    ("index.html", "index.html", "English"),
    ("de.html", "de.html", "Deutsch"),
    ("es.html", "es.html", "Español"),
    ("fr.html", "fr.html", "Français"),
    ("ja.html", "ja.html", "日本語"),
    ("pt-BR.html", "pt-BR.html", "Português (Brasil)"),
    ("zh-CN.html", "zh-CN.html", "简体中文"),
    ("webui.html", "webui.html", "Web UI"),
]

IMPORT_MAP = """<script type="importmap">
{
  "imports": {
    "three": "https://unpkg.com/three@0.184.0/build/three.module.js",
    "three/addons/controls/OrbitControls.js": "https://unpkg.com/three@0.184.0/examples/jsm/controls/OrbitControls.js"
  },
  "integrity": {
    "https://unpkg.com/three@0.184.0/build/three.module.js": "sha384-8FCZ1eVO6it4+pbec2aDtnTrwjWXZLJRC+MAGCIPDgsYnUrl/E0A2YlF8ioMKI/J",
    "https://unpkg.com/three@0.184.0/build/three.core.js": "sha384-dw2ooPewaEIrAgl6oFDBmmBWCE9oW9LxRGcfwZ0hLvEprzo202wXl7vCYHRlSnOT",
    "https://unpkg.com/three@0.184.0/examples/jsm/controls/OrbitControls.js": "sha384-4rziNxOBZKQ69i+w+f89KJ55TCYquwchVbByQwmaOeIOXdOU2PLDn3kOfXHwIJC9"
  }
}
</script>"""

FONTS = (
    '<link rel="preconnect" href="https://fonts.googleapis.com">'
    '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
    '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?'
    'family=Schibsted+Grotesk:wght@500;700&family=Source+Serif+4:ital,opsz,wght@0,8..60,400;0,8..60,600;1,8..60,400'
    '&family=IBM+Plex+Mono:wght@400&display=swap">'
)

STYLE = """
:root {
  --bg: #f5f6f8; --panel: #ffffff; --ink: #1d2026; --muted: #5b6270; --line: #e1e4e9;
  --accent: #0b7a73; --code: #eceef2; --stage: #e9ebef;
  color-scheme: light;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --bg: #121519; --panel: #1a1e24; --ink: #eceef2; --muted: #9aa3b0; --line: #2b313a;
    --accent: #2fd1c2; --code: #1b2028; --stage: #1a1f26; color-scheme: dark;
  }
}
:root[data-theme="dark"] {
  --bg: #121519; --panel: #1a1e24; --ink: #eceef2; --muted: #9aa3b0; --line: #2b313a;
  --accent: #2fd1c2; --code: #1b2028; --stage: #1a1f26; color-scheme: dark;
}
*, *::before, *::after { box-sizing: border-box; }
html { -webkit-text-size-adjust: 100%; }
body {
  margin: 0; background: var(--bg); color: var(--ink);
  font: 400 17px/1.7 "Source Serif 4", Georgia, "Times New Roman", serif;
  padding-inline: 20px; padding-block: 0 56px;
}
a { color: var(--accent); text-underline-offset: 3px; text-decoration-thickness: 1px; }
a:hover { text-decoration-thickness: 2px; }
:focus-visible { outline: 2px solid var(--accent); outline-offset: 3px; border-radius: 2px; }

.topbar {
  max-width: 1080px; margin: 0 auto; padding-block: 22px 18px;
  display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: 16px;
}
.lockup { display: block; height: 40px; width: auto; }
.lockup--dark { display: none; }
html[data-theme="dark"] .lockup--light { display: none; }
html[data-theme="dark"] .lockup--dark { display: block; }
@media (prefers-color-scheme: dark) {
  html:not([data-theme="light"]) .lockup--light { display: none; }
  html:not([data-theme="light"]) .lockup--dark { display: block; }
}
.topbar nav { display: flex; flex-wrap: wrap; align-items: center; gap: 6px 18px; font: 500 14px/1.4 "Schibsted Grotesk", system-ui, sans-serif; }
.topbar nav a { color: var(--muted); text-decoration: none; padding-block: 4px; }
.topbar nav a:hover { color: var(--ink); }
.topbar nav a[aria-current="page"] { color: var(--ink); box-shadow: inset 0 -2px 0 var(--accent); }
.theme-toggle {
  font: 500 14px/1 "Schibsted Grotesk", system-ui, sans-serif; color: var(--ink);
  background: transparent; border: 1px solid var(--line); border-radius: 999px;
  padding: 8px 14px; cursor: pointer;
}
.theme-toggle:hover { border-color: var(--accent); }

.opening {
  max-width: 1080px; margin: 0 auto; padding-block: 28px 40px;
  display: grid; grid-template-columns: minmax(0, 420px) minmax(0, 1fr); gap: 48px; align-items: center;
}
#hero {
  width: 100%; aspect-ratio: 1 / 1; border-radius: 16px; background: var(--stage);
  display: flex; align-items: center; justify-content: center; overflow: hidden;
}
#hero canvas { width: 100%; height: 100%; display: block; touch-action: none; }
#hero img { width: 62%; height: auto; }
.opening-text h1 {
  font: 700 clamp(2.1rem, 4.6vw, 3.2rem)/1.06 "Schibsted Grotesk", system-ui, sans-serif;
  letter-spacing: -0.02em; margin: 0 0 18px; text-wrap: balance;
}
.lead { font-size: 1.14rem; line-height: 1.6; color: var(--muted); margin: 0; max-width: 34ch; text-wrap: pretty; }
.hint { font: 400 14px/1.4 "Schibsted Grotesk", system-ui, sans-serif; color: var(--muted); margin-top: 22px; }

main.doc { max-width: 720px; margin: 0 auto; min-width: 0; }
main.doc > p, main.doc > ul, main.doc > ol { max-width: 68ch; }
main.doc h1, main.doc h2, main.doc h3, main.doc h4 {
  font-family: "Schibsted Grotesk", system-ui, sans-serif; line-height: 1.22; letter-spacing: -0.01em; text-wrap: balance;
}
main.doc h1 { font-size: 2rem; margin: 0 0 0.6em; }
main.doc h2 { font-size: 1.55rem; font-weight: 700; margin: 2.8em 0 0.6em; }
main.doc h3 { font-size: 1.2rem; font-weight: 700; margin: 2em 0 0.4em; }
main.doc p, main.doc ul, main.doc ol { margin: 0 0 1.1em; }
main.doc li + li { margin-top: 0.3em; }
main.doc table {
  border-collapse: collapse; display: block; overflow-x: auto; max-width: 100%;
  font: 400 14px/1.5 "Schibsted Grotesk", system-ui, sans-serif; margin: 1.4em 0;
}
main.doc th, main.doc td { border-bottom: 1px solid var(--line); padding: 9px 12px; text-align: left; vertical-align: top; }
main.doc th { font-weight: 700; }
main.doc code {
  font-family: "IBM Plex Mono", ui-monospace, Menlo, Consolas, monospace; font-size: 0.86em;
  background: var(--code); padding: 0.1em 0.35em; border-radius: 4px;
}
main.doc pre {
  background: var(--code); padding: 16px 18px; border-radius: 10px; overflow-x: auto; margin: 1.4em 0;
  line-height: 1.55;
}
main.doc pre code { background: none; padding: 0; font-size: 13px; }
main.doc img { max-width: 100%; height: auto; }
main.doc details { border-top: 1px solid var(--line); padding: 10px 0; }
main.doc summary { cursor: pointer; font: 700 15px/1.4 "Schibsted Grotesk", system-ui, sans-serif; }
pre.mermaid { background: var(--panel); border: 1px solid var(--line); text-align: center; }
main.doc blockquote {
  margin: 1.4em 0; padding: 12px 18px; border-left: 3px solid var(--accent);
  background: var(--panel); border-radius: 0 8px 8px 0; font-size: 0.95em; color: var(--muted);
}
main.doc blockquote p { margin: 0; }
main.doc hr { border: 0; border-top: 1px solid var(--line); margin: 2.4em 0; }
.anchor-top { display: block; height: 0; }
footer.site {
  max-width: 720px; margin: 56px auto 0; padding-top: 22px; border-top: 1px solid var(--line);
  font: 400 14px/1.5 "Schibsted Grotesk", system-ui, sans-serif; color: var(--muted);
}

@media (max-width: 760px) {
  .opening { grid-template-columns: 1fr; gap: 28px; padding-block: 12px 28px; }
  #hero { max-width: 360px; margin: 0 auto; }
  .opening-text { text-align: center; }
  .lead { margin-inline: auto; }
}
@media (prefers-reduced-motion: reduce) {
  * { scroll-behavior: auto !important; }
}
"""

HERO_SCRIPT = '<script type="module" src="assets/hero.js"></script>'
MERMAID_SCRIPT = (
    '<script type="module">'
    'import mermaid from "https://cdn.jsdelivr.net/npm/mermaid@11.4.1/dist/mermaid.esm.min.mjs";'
    'mermaid.initialize({ startOnLoad: true, theme: "neutral" });'
    "</script>"
)
THEME_INIT = """<script>
(function () {
  try {
    var saved = localStorage.getItem('lab-theme');
    if (saved === 'light' || saved === 'dark') document.documentElement.setAttribute('data-theme', saved);
  } catch (e) {}
})();
</script>"""

THEME_TOGGLE = """<script>
(function () {
  var root = document.documentElement, btn = document.getElementById('theme-toggle');
  function effective() {
    var explicit = root.getAttribute('data-theme');
    if (explicit) return explicit;
    return matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
  }
  function label() {
    var dark = effective() === 'dark';
    btn.textContent = dark ? 'Light' : 'Dark';
    btn.setAttribute('aria-label', dark ? 'Switch to light theme' : 'Switch to dark theme');
  }
  label();
  btn.addEventListener('click', function () {
    var next = effective() === 'dark' ? 'light' : 'dark';
    root.setAttribute('data-theme', next);
    try { localStorage.setItem('lab-theme', next); } catch (e) {}
    label();
  });
})();
</script>"""


def split_lead(html):
    """Take the first prose paragraph outside any blockquote to serve as the page's lead line."""
    quoted = [m.span() for m in re.finditer(r"<blockquote>.*?</blockquote>", html, flags=re.DOTALL)]
    for match in re.finditer(r"<p>(.*?)</p>", html, flags=re.DOTALL):
        if any(start <= match.start() < end for start, end in quoted):
            continue
        plain = re.sub(r"<[^>]+>", "", match.group(1)).strip()
        if len(plain) > 60:
            return match.group(1), html[: match.start()] + html[match.end():]
    return "", html


def nav_html(current_out):
    links = []
    for _, out, label in PAGES:
        current = ' aria-current="page"' if out == current_out else ""
        links.append(f'<a href="{out}"{current}>{label}</a>')
    return "<nav aria-label=\"Documentation\">{}</nav>".format("".join(links))


def page(title, lead, body, current_out, mermaid):
    return "\n".join([
        "<!doctype html>",
        '<html lang="en">',
        "<head>",
        '<meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width,initial-scale=1">',
        f"<title>{title}</title>",
        '<meta name="description" content="{}">'.format(
            html.escape(re.sub(r"<[^>]+>", "", lead).strip()[:160], quote=True)
        ),
        '<link rel="icon" href="../media/brand/favicon.svg" type="image/svg+xml">',
        FONTS,
        IMPORT_MAP,
        f"<style>{STYLE}</style>",
        THEME_INIT,
        "</head>",
        "<body>",
        '<a id="top" class="anchor-top"></a>',
        '<header class="topbar">',
        (
            '<a href="index.html" aria-label="lab-in-a-box home">'
            '<img class="lockup lockup--light" src="../media/brand/lockup-horizontal-light.svg" alt="lab-in-a-box">'
            '<img class="lockup lockup--dark" src="../media/brand/lockup-horizontal-dark.svg" alt="" aria-hidden="true">'
            "</a>"
        ),
        f"<div>{nav_html(current_out)}",
        '<button type="button" class="theme-toggle" id="theme-toggle">Dark</button></div>',
        "</header>",
        '<section class="opening">',
        (
            '<div id="hero" role="img" aria-label="lab-in-a-box logo as a rotating 3D cube">'
            '<img src="../media/brand/logo-3d-4f.svg" alt="lab-in-a-box logo"></div>'
        ),
        '<div class="opening-text">',
        f"<h1>{title}</h1>",
        f'<p class="lead">{lead}</p>',
        '<p class="hint">Drag the cube to rotate it.</p>',
        "</div>",
        "</section>",
        '<main class="doc">',
        body,
        "</main>",
        '<footer class="site">Generated from docs/src/ by docs/build_docs.py.</footer>',
        THEME_TOGGLE,
        HERO_SCRIPT,
        MERMAID_SCRIPT if mermaid else "",
        "</body>",
        "</html>",
        "",
    ])


def build():
    for source, out, label in PAGES:
        body = (SOURCE / source).read_text(encoding="utf-8")
        lead, body = split_lead(body)
        title = "lab-in-a-box" if out == "index.html" else f"lab-in-a-box · {label}"
        page_html = page(title, lead, body, out, mermaid='class="mermaid"' in body)
        (DOCS / out).write_text(page_html, encoding="utf-8")
        print(f"wrote docs/{out}")


if __name__ == "__main__":
    if not SOURCE.is_dir():
        sys.exit(f"missing source directory: {SOURCE}")
    build()
