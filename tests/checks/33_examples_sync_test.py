#!/usr/bin/env python3
# Confirms tests/examples/*.json stays byte-for-byte in sync (modulo JSONC
# comments) with the example embedded in README.md — see
# tests/examples/README.md for why these two copies exist at all (the
# README's own copy is what a reader sees; tests/examples/'s copy is what
# actually gets deployed+checked against real hardware by
# tests/examples/run_example.sh, which needs a real .json file to hand to
# setup_lab.py, not a markdown fence). No hardware needed for THIS check —
# it's pure text comparison, so it runs in the normal containerized sweep.
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


def extract_jsonc_after(readme_text, anchor):
    """
    Returns the parsed JSON object from the first ```jsonc fence appearing
    after `anchor` (a literal substring, e.g. a section heading) in
    readme_text. Dies loudly (via check() failure + returning None) if
    either isn't found, rather than silently comparing nothing to nothing.
    """
    anchor_pos = readme_text.find(anchor)
    if anchor_pos == -1:
        check("README.md still contains the anchor {!r}".format(anchor), False)
        return None
    m = re.search(r'<pre><code class="language-jsonc">(.*?)</code></pre>', readme_text[anchor_pos:], re.S)
    if not m:
        check("a language-jsonc code block follows {!r} in README.md".format(anchor), False)
        return None
    return json.loads(strip_jsonc_comments(html.unescape(m.group(1))))


readme_text = (_REPO / "docs" / "src" / "index.html").read_text()

# name -> the README anchor whose NEXT ```jsonc fence is that example's
# source of truth. rancher-cluster's own "### RKE2 + Rancher + Longhorn"
# section deliberately has no JSON of its own — it points back at the full
# example under "## Lab definition format" instead (see the README's own
# "see the full ... example above").
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
    from_readme = extract_jsonc_after(readme_text, anchor)
    if from_readme is None:
        continue
    check("tests/examples/{}.json matches the example under {!r} in README.md "
          "(if this fails because you intentionally changed one, update the other)".format(name, anchor),
          fixture == from_readme)


if failures:
    print("{} check(s) failed".format(len(failures)))
    sys.exit(1)
print("all examples-sync checks passed")
