#!/usr/bin/env python3
"""Build the documentation site in docs/ from the Markdown sources in docs/md/.

Run from the repository root: python3 docs/build_docs.py  (needs the `markdown` package).
Output: docs/index.html (README.md), one page per translation, and docs/webui.html.
Every page shows the rotating 3D logo (docs/assets/hero.js) as its header.
"""
import re
import sys
from pathlib import Path

import markdown

DOCS = Path(__file__).resolve().parent
REPO = DOCS.parent
SOURCE = DOCS / "md"
REPO_URL = "https://github.com/SUSE-Technical-Marketing/lab-in-a-box"

PAGES = [
    ("README.md", "index.html", "English"),
    ("README.de.md", "de.html", "Deutsch"),
    ("README.es.md", "es.html", "Español"),
    ("README.fr.md", "fr.html", "Français"),
    ("README.ja.md", "ja.html", "日本語"),
    ("README.pt-BR.md", "pt-BR.html", "Português (Brasil)"),
    ("README.zh-CN.md", "zh-CN.html", "简体中文"),
    ("README.webui.md", "webui.html", "Web UI"),
]
PAGE_FOR_SOURCE = {src: out for src, out, _ in PAGES}

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

STYLE = """
:root {
  --bg: #eceef1; --panel: #ffffff; --ink: #23272d; --muted: #5a606b; --line: #dfe2e6;
  --accent: #0f8f86; --stage: #e8eaed; --code: #f3f4f5;
  color-scheme: light;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --bg: #16191e; --panel: #23272d; --ink: #f3f4f5; --muted: #a9aeb7; --line: #353a42;
    --accent: #2fd1c2; --stage: #1d2128; --code: #2c3138; color-scheme: dark;
  }
}
:root[data-theme="dark"] {
  --bg: #16191e; --panel: #23272d; --ink: #f3f4f5; --muted: #a9aeb7; --line: #353a42;
  --accent: #2fd1c2; --stage: #1d2128; --code: #2c3138; color-scheme: dark;
}
body { margin: 0; background: var(--bg); color: var(--ink);
  font: 16px/1.6 "Helvetica Neue", Helvetica, Arial, sans-serif; padding-inline: 16px; }
a { color: var(--accent); }
header.site { max-width: 980px; margin: 0 auto; padding-block: 24px 8px; text-align: center; }
#hero { width: min(360px, 80vw); height: 340px; margin: 0 auto; border-radius: 12px; background: var(--stage);
  display: flex; align-items: center; justify-content: center; }
#hero canvas { width: 100%; height: 100%; display: block; touch-action: none; }
#hero img { max-width: 80%; height: auto; }
.wordmark { display: block; margin: 8px auto 0; max-width: min(420px, 90%); height: auto; }
nav.site { max-width: 980px; margin: 0 auto; padding-block: 12px; display: flex; flex-wrap: wrap; gap: 8px 14px;
  justify-content: center; border-bottom: 1px solid var(--line); font-size: 14px; }
nav.site a { text-decoration: none; }
nav.site a[aria-current="page"] { font-weight: 600; color: var(--ink); }
main { max-width: 980px; margin: 0 auto; padding-block: 24px 48px; min-width: 0; }
main h1, main h2, main h3 { line-height: 1.25; text-wrap: balance; }
main h2 { margin-top: 2.2em; border-bottom: 1px solid var(--line); padding-bottom: 6px; }
main table { border-collapse: collapse; display: block; overflow-x: auto; max-width: 100%; font-size: 14px; }
main th, main td { border: 1px solid var(--line); padding: 6px 10px; vertical-align: top; }
main pre { background: var(--code); padding: 12px; border-radius: 8px; overflow-x: auto; font-size: 13px; }
main code { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 0.92em; }
main img { max-width: 100%; height: auto; }
main details { background: var(--panel); border: 1px solid var(--line); border-radius: 8px; padding: 8px 14px; }
main summary { cursor: pointer; font-weight: 600; }
pre.mermaid { background: var(--panel); text-align: center; }
footer.site { max-width: 980px; margin: 0 auto; padding-block: 16px 32px; border-top: 1px solid var(--line);
  color: var(--muted); font-size: 13px; }
"""

HERO_SCRIPT = '<script type="module" src="assets/hero.js"></script>'
MERMAID_SCRIPT = (
    '<script type="module">'
    'import mermaid from "https://cdn.jsdelivr.net/npm/mermaid@11.4.1/dist/mermaid.esm.min.mjs";'
    'mermaid.initialize({ startOnLoad: true, theme: "neutral" });'
    "</script>"
)


def strip_logo_header(text):
    """Remove the README's own logo block: the hero replaces it on every page."""
    return re.sub(
        r'<a id="top"></a>\s*<p align="center">\s*<a href="[^"]*logo[^"]*">.*?</p>\s*',
        "",
        text,
        count=1,
        flags=re.S,
    )


def rewrite_links(text):
    """Point links and image paths at the generated pages and the repo."""
    text = re.sub(r'(src|href)="media/', r'\1="../media/', text)
    text = re.sub(r'\]\(media/', "](../media/", text)

    def page_link(match):
        target = match.group(2)
        if target in PAGE_FOR_SOURCE:
            return "{}{}".format(match.group(1), PAGE_FOR_SOURCE[target])
        return match.group(0)

    text = re.sub(r'((?:href="|\]\())(README[\w.-]*\.md)', page_link, text)
    text = re.sub(
        r'\]\((?!https?:|#|\.\./|mailto:)([\w./-]+\.(?:md|py|sh|json|yaml|yml|txt|cfg|bash|sh))\)',
        lambda m: "]({}/blob/main/{})".format(REPO_URL, m.group(1)),
        text,
    )
    text = re.sub(r'\]\(README\.md#', "](index.html#", text)
    text = re.sub(r'\]\(([\w.-]+/)\)', lambda m: "]({}/tree/main/{})".format(REPO_URL, m.group(1)), text)
    return text


def github_slug(value, separator):
    """Heading anchors built the way GitHub builds them, so in-page links still resolve."""
    return re.sub(r"[^\w\- ]", "", value.strip().lower()).replace(" ", separator)


def render_body(text):
    html = markdown.markdown(
        text,
        extensions=["tables", "fenced_code", "attr_list", "md_in_html", "toc", "sane_lists"],
        extension_configs={"toc": {"slugify": github_slug}},
    )
    html = re.sub(
        r'<pre><code class="language-mermaid">(.*?)</code></pre>',
        r'<pre class="mermaid">\1</pre>',
        html,
        flags=re.S,
    )
    return html


def nav_html(current_out):
    links = []
    for _, out, label in PAGES:
        current = ' aria-current="page"' if out == current_out else ""
        links.append('<a href="{}"{}>{}</a>'.format(out, current, label))
    return "<nav class=\"site\">{}</nav>".format(" · ".join(links))


def page(title, body, current_out, mermaid):
    return "\n".join([
        "<!doctype html>",
        '<html lang="en">',
        "<head>",
        '<meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width,initial-scale=1">',
        "<title>{}</title>".format(title),
        '<link rel="icon" href="../media/brand/favicon.svg" type="image/svg+xml">',
        IMPORT_MAP,
        "<style>{}</style>".format(STYLE),
        "</head>",
        "<body>",
        '<header class="site">',
        '<div id="hero" role="img" aria-label="lab-in-a-box logo, a rotating 3D cube (drag to rotate)">',
        '<img src="../media/brand/logo-3d-4f.svg" alt="lab-in-a-box logo">',
        "</div>",
        '<picture><source media="(prefers-color-scheme: dark)" srcset="../media/brand/logo-text-dark.png">'
        '<img class="wordmark" src="../media/brand/logo-text.png" alt="lab-in-a-box wordmark"></picture>',
        "</header>",
        nav_html(current_out),
        '<a id="top"></a>',
        "<main>",
        body,
        "</main>",
        '<footer class="site">Generated from docs/md/ by docs/build_docs.py. Edit the Markdown, then rebuild.</footer>',
        HERO_SCRIPT,
        MERMAID_SCRIPT if mermaid else "",
        "</body>",
        "</html>",
        "",
    ])


def build():
    for source, out, label in PAGES:
        text = (SOURCE / source).read_text(encoding="utf-8")
        text = strip_logo_header(text)
        text = rewrite_links(text)
        body = render_body(text)
        title = "lab-in-a-box" if out == "index.html" else "lab-in-a-box · {}".format(label)
        html = page(title, body, out, mermaid='class="mermaid"' in body)
        (DOCS / out).write_text(html, encoding="utf-8")
        print("wrote docs/{}".format(out))


if __name__ == "__main__":
    if not SOURCE.is_dir():
        sys.exit("missing source directory: {}".format(SOURCE))
    build()
