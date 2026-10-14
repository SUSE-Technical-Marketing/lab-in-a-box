#!/usr/bin/env python3
# Confirms each tests/examples/*.json matches (JSONC comments aside) the example of the same name embedded in
# docs/src/index.html; tests/examples/run_example.sh deploys those files on real hardware. Pure text comparison.
# Run from 33_examples_sync.sh, in its own container — see tests/run_tests.sh.
import html
import json
import re
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]

failures = []


def check(desc, cond):
    if not cond:
        failures.append(desc)
        print("FAIL:", desc)


def strip_jsonc_comments(text):
    """
    Strip `// ...` line comments from JSONC, respecting string literals —
    a literal "//" inside a string value (e.g. a "https://..." URL) must
    not be treated as a comment start.
    """
    out = []
    in_string = False
    escape = False
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if in_string:
            out.append(c)
            if escape:
                escape = False
            elif c == "\\":
                escape = True
            elif c == '"':
                in_string = False
            i += 1
            continue
        if c == '"':
            in_string = True
            out.append(c)
            i += 1
            continue
        if c == "/" and i + 1 < n and text[i + 1] == "/":
            while i < n and text[i] != "\n":
                i += 1
            continue
        out.append(c)
        i += 1
    return "".join(out)


def extract_jsonc_after(docs_text, anchor):
    """
    Returns the parsed JSON object from the first ```jsonc fence appearing
    after `anchor` (a literal substring, e.g. a section heading) in
    docs_text. Dies loudly (via check() failure + returning None) if
    either isn't found, rather than silently comparing nothing to nothing.
    """
    anchor_pos = docs_text.find(anchor)
    if anchor_pos == -1:
        check("docs/src/index.html still contains the anchor {!r}".format(anchor), False)
        return None
    m = re.search(r'<pre><code class="language-jsonc">(.*?)</code></pre>', docs_text[anchor_pos:], re.S)
    if not m:
        check("a language-jsonc code block follows {!r} in docs/src/index.html".format(anchor), False)
        return None
    return json.loads(strip_jsonc_comments(html.unescape(m.group(1))))


docs_text = (_REPO / "docs" / "src" / "index.html").read_text()

# name -> the docs/src/index.html heading whose next language-jsonc block is that example.
_ANCHORS = {
    "standalone": '<h3 id="minimal-single-vm-lab">',
    "rancher-cluster": '<h2 id="lab-definition-format">',
    "multi-host": '<h3 id="spreading-a-cluster-across-two-hosts">',
    "uyuni-lab": '<h3 id="suse-multi-linux-manager-uyuni-server--a-registered-client">',
    "legacy": '<h3 id="deploying-a-legacy-image-centos-7">',
}

for name, anchor in _ANCHORS.items():
    fixture_path = _REPO / "tests" / "examples" / "{}.json".format(name)
    if not fixture_path.is_file():
        check("tests/examples/{}.json exists".format(name), False)
        continue
    fixture = json.loads(fixture_path.read_text())
    from_docs = extract_jsonc_after(docs_text, anchor)
    if from_docs is None:
        continue
    check("tests/examples/{}.json matches the example under {!r} in docs/src/index.html "
          "(if this fails because you intentionally changed one, update the other)".format(name, anchor),
          fixture == from_docs)


if failures:
    print("{} check(s) failed".format(len(failures)))
    sys.exit(1)
print("all examples-sync checks passed")
