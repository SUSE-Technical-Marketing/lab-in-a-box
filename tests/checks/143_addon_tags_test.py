#!/usr/bin/env python3
# Unit tests for the add-on tag model: libs/apps.py's TAGS/_TAGS_BY_ADDON/tags_for(), its appearance in an
# add-on's capabilities, and the web UI filter built from it. The add-on files themselves are read rather than
# executed — asking a hundred scripts for their capabilities would cost a hundred subprocesses — so a declared
# PLUGIN["tags"] is taken from the source with ast. Run from 143_addon_tags.sh, in its own container.
import ast
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "libs"))
sys.path.insert(0, str(_REPO / "webui" / "lib"))

import apps  # noqa: E402

failures = []


def check(desc, cond):
    if not cond:
        failures.append(desc)
        print("FAIL:", desc)


def declared_tags(path):
    """PLUGIN["tags"] as the script's own source declares it, or None. A script that is not Python declares none."""
    try:
        tree = ast.parse(Path(path).read_text())
    except (SyntaxError, UnicodeDecodeError, ValueError):
        return None
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(t, ast.Name) and t.id == "PLUGIN" for t in node.targets):
            continue
        if not isinstance(node.value, ast.Dict):
            continue
        for key, value in zip(node.value.keys, node.value.values):
            if isinstance(key, ast.Constant) and key.value == "tags":
                try:
                    return list(ast.literal_eval(value))
                except ValueError:
                    return None
    return None


addons = {name[len("install_"):]: path
          for name, path in apps.addon_files(str(_REPO / "scripts")).items()}
check("the scan found the add-ons at all, so an empty result is not read as everything passing", len(addons) > 50)

# ── every add-on is classified, and only with tags that exist ──────────────
untagged = sorted(n for n, p in addons.items() if not (declared_tags(p) or apps.tags_for(n)))
check("every add-on resolves to at least one tag, from its own PLUGIN or the central table: {}".format(
    ", ".join(untagged) or "none"), not untagged)

unknown = {}
for name, path in sorted(addons.items()):
    for tag in (declared_tags(path) or apps.tags_for(name)):
        if tag not in apps.TAGS:
            unknown.setdefault(name, []).append(tag)
check("every tag an add-on carries is one of apps.TAGS: {}".format(unknown or "all known"), not unknown)

stale = sorted(set(apps._TAGS_BY_ADDON) - set(addons))
check("the central table names no add-on that no longer exists: {}".format(", ".join(stale) or "none"), not stale)

check("a tag title is given for every tag, so a filter can label them",
      all(isinstance(t, str) and t for t in apps.TAGS.values()))
check("tag_titles() keeps TAGS' order, which is the order a dropdown offers",
      [t["name"] for t in apps.tag_titles()] == list(apps.TAGS))

# ── tags_for(): an add-on's own declaration wins over the table ────────────
check("an add-on that declares its own tags overrides the central table",
      apps.tags_for("longhorn", {"tags": ["security"]}) == ["security"])
check("an add-on that declares none falls back to the table",
      apps.tags_for("longhorn", {}) == list(apps._TAGS_BY_ADDON["longhorn"]))
check("an add-on nobody classified resolves to [], not to a guess", apps.tags_for("not-an-addon") == [])
check("the file's own name answers too, which is what a directory scan has in hand",
      apps.tags_for("install_longhorn") == apps.tags_for("longhorn") != [])
check("an empty declaration is not a declaration: the table still answers",
      apps.tags_for("neuvector", {"tags": []}) == ["security"])

# ── the capabilities every consumer reads ─────────────────────────────────
caps = apps.attach_capabilities({}, {"name": "neuvector"})["capabilities"]
check("an add-on's capabilities carry its resolved tags, which is what the CLI and the web UI both read",
      caps.get("tags") == ["security"])
check("the default plugin has a tags key, so a consumer never has to guess the shape",
      "tags" in apps.DEFAULT_PLUGIN)

# ── the catalogue and the filter the browser is handed ────────────────────
import discovery  # noqa: E402

check("discovery.tags() offers the vocabulary with titles",
      [t["name"] for t in discovery.tags()] == list(apps.TAGS))

api_src = (_REPO / "webui" / "lib" / "api.py").read_text()
check("the components action sends the vocabulary, so the dropdown is not guessed from the items",
      '"tags": discovery.tags()' in api_src)

index_html = (_REPO / "webui" / "htdocs" / "index.html").read_text()
check("the catalogue panel has the category dropdown", 'id="tagFilter"' in index_html)
check("...and it is labelled for a screen reader, like every other control there",
      'aria-label="Filter add-ons by category"' in index_html)

app_js = (_REPO / "webui" / "htdocs" / "app.js").read_text()
check("the palette filters on the selected category", "(c.tags || []).includes(tag)" in app_js)
check("changing the category re-renders the palette", '$("#tagFilter").addEventListener("change"' in app_js)
check("the dropdown is built from the vocabulary the API sent", "state.tags = data.tags || []" in app_js)

if failures:
    print("{} check(s) failed".format(len(failures)))
    sys.exit(1)
print("all add-on tag checks passed")
