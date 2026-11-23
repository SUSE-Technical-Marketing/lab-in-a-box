#!/usr/bin/env python3.11
# An add-on that is not in the documentation, in every language, fails here; so does a documented add-on
# counter that no longer matches the number of add-ons — see 138_addon_docs_coverage.sh.
#
# The counter is marked in its own text so that it can be found without knowing the language: the eight
# documentation pages carry <span data-addon-count>N</span> and README.md carries <!-- addon-count -->N.
# Both the sources in docs/src/ and the pages built from them in docs/ are checked, so a documentation
# change that was never rebuilt with docs/build_docs.py fails too.
import re
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]

LANGUAGES = ("index", "es", "de", "fr", "ja", "zh-CN", "pt-BR", "ar")

# A table cell naming an add-on, linked to its upstream project or not.
_CELL = "<td>(?:<a href=\"[^\"]*\">)?<code>{}</code>(?:</a>)?</td>"
_HTML_COUNTER = re.compile(r"<span[^>]*\bdata-addon-count\b[^>]*>\s*(\d+)\s*</span>")
_README_COUNTER = re.compile(r"<!--\s*addon-count\s*-->\s*\**(\d+)")

failures = []


def check(desc, cond):
    if not cond:
        failures.append(desc)
        print("FAIL:", desc)


def addon_names():
    """Every add-on the repository ships, by the name a lab definition uses in its addons[] list."""
    return sorted(p.name[len("install_"):] for p in (_REPO / "scripts").glob("install_*"))


def documented(text, name):
    """Whether `name` has a row of its own in an add-on table of this page."""
    return re.search(_CELL.format(re.escape(name)), text) is not None


addons = addon_names()
check("the add-ons were found at all", len(addons) >= 90)
check("no add-on name is empty", all(addons))

pages = []
for language in LANGUAGES:
    pages.append(_REPO / "docs" / "src" / "{}.html".format(language))
    pages.append(_REPO / "docs" / "{}.html".format(language))

for page in pages:
    where = page.relative_to(_REPO)
    if not page.is_file():
        check("{} exists".format(where), False)
        continue
    text = page.read_text(encoding="utf-8")

    missing = [name for name in addons if not documented(text, name)]
    check("{}: every add-on has a row ({} undocumented: {})".format(
        where, len(missing), ", ".join(missing[:8]) or "none"), not missing)

    counters = _HTML_COUNTER.findall(text)
    check("{}: exactly one marked add-on counter, found {}".format(where, len(counters)),
          len(counters) == 1)
    if len(counters) == 1:
        check("{}: the counter says {} but there are {} add-ons".format(
            where, counters[0], len(addons)), int(counters[0]) == len(addons))

readme = _REPO / "README.md"
text = readme.read_text(encoding="utf-8")
counters = _README_COUNTER.findall(text)
check("README.md: exactly one marked add-on counter, found {}".format(len(counters)),
      len(counters) == 1)
if len(counters) == 1:
    check("README.md: the counter says {} but there are {} add-ons".format(counters[0], len(addons)),
          int(counters[0]) == len(addons))

if failures:
    print("\n{} add-on documentation check(s) failed".format(len(failures)))
    sys.exit(1)
print("all {} add-ons are documented in {} pages, and every counter matches".format(len(addons), len(pages)))
