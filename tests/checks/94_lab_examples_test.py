#!/usr/bin/env python3.11
# The lab-builder's example labs — see 94_lab_examples.sh.
import json
from unittest import mock
import os
import sys
import tempfile
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "libs"))
sys.path.insert(0, str(_REPO / "webui" / "lib"))
os.environ["LABBUILDER_STATUS_FILE"] = os.devnull

import api  # noqa: E402
import discovery  # noqa: E402

failures = []


def check(desc, cond):
    if not cond:
        failures.append(desc)
        print("FAIL:", desc)


# ── the repository's own examples ─────────────────────────────────────────────
items = discovery.examples()
check("the shipped examples are listed", len(items) >= 2)
check("each one is named by its file name and has a title",
      all(e["name"] and e["title"] and "/" not in e["name"] for e in items))
check("the list is sorted by name", [e["name"] for e in items] == sorted(e["name"] for e in items))
check("a title drops the extension and reads as words",
      discovery._example_title("harvester-aws.json.template") == "harvester aws")
check("every listed example counts its VMs", all(e["nodes"] > 0 for e in items))
for e in items:
    definition = discovery.example(e["name"])
    check("{} opens as a lab definition with the nodes it counted".format(e["name"]),
          isinstance(definition, dict) and len(definition.get("nodes") or {}) == e["nodes"])

# ── a name is a name, never a path ────────────────────────────────────────────
for bad in ("../../etc/passwd", "/etc/passwd", "sub/dir.json", "", None):
    try:
        discovery.example(bad)
        check("a path is refused as an example name: {!r}".format(bad), False)
    except ValueError:
        pass
    except FileNotFoundError:
        check("a path is refused as an example name: {!r}".format(bad), False)
try:
    discovery.example("not-an-example.json")
    check("an unknown example is refused", False)
except FileNotFoundError:
    pass

# ── what counts as an example ────────────────────────────────────────────────
tmp = Path(tempfile.mkdtemp())
(tmp / "good.json").write_text(json.dumps({
    "common": {"lab_description": "Two VMs and a cluster"},
    "nodes": {"n1": {"addons": ["mariadb"]}, "n2": {}},
    "kclusters": {"c1": {"clu_type": "rke2", "addons": ["rancher", {"longhorn": {"lh_version": "1.9"}}]}},
}))
(tmp / "old-format.json").write_text(json.dumps({
    "nodes": {"n1": {}}, "cluster": {"clu_name": "c1", "addons": ["rancher"]}}))
(tmp / "broken.json").write_text("{not json at all")
(tmp / "no-nodes.json").write_text(json.dumps({"common": {}}))
(tmp / "notes.txt").write_text("not a lab")
(tmp / "adir.json").mkdir()
os.environ[discovery.EXAMPLES_DIR_ENV] = str(tmp)
try:
    names = [e["name"] for e in discovery.examples()]
    check("a definition with nodes is offered", "good.json" in names)
    check("the older single-cluster format is offered too", "old-format.json" in names)
    check("a file that does not parse is left out, not an error", "broken.json" not in names)
    check("a file without nodes is not a lab", "no-nodes.json" not in names)
    check("a file that is not a definition is ignored", "notes.txt" not in names)
    check("a directory is not an example", "adir.json" not in names)
    good = [e for e in discovery.examples() if e["name"] == "good.json"][0]
    check("its own description is used when it has one", good["description"] == "Two VMs and a cluster")
    check("VMs and Kubernetes clusters are counted", good["nodes"] == 2 and good["kclusters"] == 1)
    check("every add-on it installs is listed, per-node and per-cluster, overrides included",
          good["addons"] == ["longhorn", "mariadb", "rancher"])
    old = [e for e in discovery.examples() if e["name"] == "old-format.json"][0]
    check("the older format's add-ons are listed too", old["addons"] == ["rancher"])
    check("a lab with no add-ons lists none",
          [e for e in discovery.examples() if e["name"] == "no-nodes.json"] == [])

    # ── through the API ──────────────────────────────────────────────────────
    status, body = api.dispatch("examples", "GET", {}, b"")
    check("GET examples answers 200 with the list",
          status == 200 and [e["name"] for e in body["examples"]] == names)
    status, body = api.dispatch("example", "GET", {"name": ["good.json"]}, b"")
    check("GET example answers 200 with the definition", status == 200 and len(body["nodes"]) == 2)
    status, body = api.dispatch("example", "GET", {"name": ["../../etc/passwd"]}, b"")
    check("GET example refuses a path with 400", status == 400 and "invalid example name" in body["error"])
    status, body = api.dispatch("example", "GET", {"name": ["nope.json"]}, b"")
    check("GET example answers 400 for an unknown name", status == 400 and "no such example" in body["error"])
    status, body = api.dispatch("example", "GET", {}, b"")
    check("GET example with no name is refused", status == 400)
    status, _ = api.dispatch("examples", "POST", {}, b"")
    check("examples is a GET-only action", status == 400)
finally:
    del os.environ[discovery.EXAMPLES_DIR_ENV]

# ── the UI offers them ────────────────────────────────────────────────────────
app_js = (_REPO / "webui" / "htdocs" / "app.js").read_text()
index_html = (_REPO / "webui" / "htdocs" / "index.html").read_text()
check("the page has an Examples control and its dialog",
      'id="examplesBtn"' in index_html and 'id="examplesDialog"' in index_html)
check("the control is wired to the dialog", '$("#examplesBtn").addEventListener' in app_js)
check("the dialog reads the examples through the API", 'apiGet("examples")' in app_js)
check("opening one loads it onto the canvas", 'loadLab(await apiGet("example"' in app_js)
static = (_REPO / "scripts" / "build-cube-static.py").read_text()
check("the static build embeds the examples and answers for them",
      '"examples": examples' in static and 'action === "examples"' in static)

# ── what the dialog shows, and that it can show it ────────────────────────
# An example's description is a sentence. On the single-line row the examples used to borrow, the name was
# ellipsised away and the Open button squeezed off the row, so the rows are their own two-line layout now.
app = (_REPO / "webui" / "htdocs" / "app.js").read_text()
css = (_REPO / "webui" / "htdocs" / "style.css").read_text()
check("an example row is its own layout, not the one-line saved-lab row",
      'el("li", "example-row")' in app and "example-row" in css)
check("the description wraps under the name rather than beside it",
      'el("p", "example-desc", ex.description)' in app and ".example-desc" in css)
check("the name is allowed to wrap instead of being cut off",
      ".example-row .saved-name" in css and "white-space: normal" in css)
check("the Open button keeps its place whatever the text does", ".example-row .btn" in css)
check("a long description is clamped rather than filling the dialog", "-webkit-line-clamp" in css)

# The older format keeps its cluster under "cluster" and one shared add-on list at the top level, so an example in
# that format used to be listed as having no cluster and no add-ons at all.
old_format = {"nodes": {"n1": {}, "n2": {}}, "cluster": {"clu_name": "c1", "clu_type": "rke2"},
              "addons": ["rancher", "longhorn"], "common": {}}
with tempfile.TemporaryDirectory() as tmp:
    Path(tmp, "old-format.json").write_text(json.dumps(old_format))
    with mock.patch.dict(os.environ, {"LABBUILDER_EXAMPLES_DIR": tmp}):
        listed = discovery.examples()
check("an older-format example is listed with its cluster and its add-ons",
      len(listed) == 1 and listed[0]["kclusters"] == 1
      and listed[0]["addons"] == ["longhorn", "rancher"] and listed[0]["nodes"] == 2)

if failures:
    print("{} check(s) failed".format(len(failures)))
    sys.exit(1)
print("all lab-example checks passed")
