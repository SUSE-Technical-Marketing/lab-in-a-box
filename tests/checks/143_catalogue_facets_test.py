#!/usr/bin/env python3
# Unit tests for the catalogue's filter: webui/lib/discovery.py's CAPABILITY_FIELDS/facets_of()/facets(), what the
# components action sends, and the web UI's dropdown being wired to it. Nothing here is a list somebody maintains.
# Every option the filter offers is a capability value an add-on declares in its own PLUGIN, read off the add-ons
# themselves, so what is checked is the reading. Run from 143_catalogue_facets.sh, in its own container — see
# tests/run_tests.sh.
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "libs"))
sys.path.insert(0, str(_REPO / "webui" / "lib"))

import apps  # noqa: E402
import discovery  # noqa: E402

failures = []


def check(desc, cond):
    if not cond:
        failures.append(desc)
        print("FAIL:", desc)


# ── facets_of(): the add-on's own capability values, never a second vocabulary ─
check("the capability an add-on declares is the facet it carries",
      discovery.facets_of({"layers": ["kubernetes"]}) == ["layers:kubernetes"])
check("one that installs both ways carries both values",
      discovery.facets_of({"layers": ["kubernetes", "standalone-container"]})
      == ["layers:kubernetes", "layers:standalone-container"])
check("what it needs of a cluster is read the same way, value by value",
      discovery.facets_of({"requires_cluster": ["ingress", "storageclass"]})
      == ["requires_cluster:ingress", "requires_cluster:storageclass"])
check("so is what it supplies, which is the question 'who can give me an ingress' asks",
      discovery.facets_of({"provides_cluster": ["ingress"]}) == ["provides_cluster:ingress"])
check("a field holding a single name rather than a list is read too",
      discovery.facets_of({"requires_kubernetes": "rke2"}) == ["requires_kubernetes:rke2"])
check("...and the list shape gives one facet per distribution",
      discovery.facets_of({"requires_kubernetes": ["rke2", "k3s"]})
      == ["requires_kubernetes:rke2", "requires_kubernetes:k3s"])
check("a field left empty contributes nothing", discovery.facets_of({"requires_kubernetes": None}) == [])
check("the facets come out in CAPABILITY_FIELDS order, so the dropdown reads the same every time",
      discovery.facets_of({"targets": ["container"], "layers": ["kubernetes"]})
      == ["layers:kubernetes", "targets:container"])
check("an item declaring no capability at all carries no facet rather than failing",
      discovery.facets_of({}) == [])
check("every field read is one an add-on actually declares in its PLUGIN",
      {field for field, _group in discovery.CAPABILITY_FIELDS} <= set(apps.DEFAULT_PLUGIN))

offered = discovery.facets([{"facets": ["layers:kubernetes", "targets:container"]},
                            {"facets": ["layers:os-native"]}])
check("facets() offers each declared value once, grouped by the field it came from",
      [(f["name"], f["title"], f["group"]) for f in offered]
      == [("layers:kubernetes", "kubernetes", "Runs as"), ("layers:os-native", "os-native", "Runs as"),
          ("targets:container", "container", "Installed on")])
check("a value nothing declares is not offered, so the dropdown never shows an empty answer",
      not [f for f in offered if f["name"] == "targets:baremetal"])


# ── the catalogue: the filter and the items agree ─────────────────────────
cat = discovery.catalogue()
check("the catalogue was built at all, so an empty result is not read as everything passing", len(cat) > 50)
check("every add-on carries at least one capability, or the filter would hide it for good",
      not sorted(it["name"] for it in cat if it["kind"] == "addon" and not it.get("facets")))
check("every facet an item carries is one the dropdown offers",
      not {f for it in cat for f in it.get("facets") or []} - {f["name"] for f in discovery.facets(cat)})
check("an add-on's facets come from its own capabilities",
      any(it["name"] == "install_longhorn" and "provides_cluster:storageclass" in it["facets"] for it in cat))
check("a Kubernetes distribution is offered as such, since a lab starts by choosing one",
      all("layers:kubernetes" in it["facets"] for it in cat if it["kind"] == "kcluster"))
check("an optional base-schema section declares no capability of its own, so no capability claims it",
      all(not it["facets"] for it in cat if it["kind"] == "infrastructure"))
check("the capability fields the facets are read from are sent with each add-on, so the browser could too",
      all({field for field, _g in discovery.CAPABILITY_FIELDS} <= set(it)
          for it in cat if it["kind"] == "addon"))


# ── what the browser is handed, and what it does with it ─────────────────
import api as webui_api  # noqa: E402

st, body = webui_api.dispatch("components", "GET", {}, b"", user=None, https=False)
check("the components action sends the vocabulary beside the items",
      st == 200 and [f["name"] for f in body["facets"]] == [f["name"] for f in discovery.facets(cat)])
check("...and it is the catalogue's own, so the two cannot disagree",
      all(f["title"] and f["group"] for f in body["facets"]))

index_html = (_REPO / "webui" / "htdocs" / "index.html").read_text()
check("the catalogue panel has the filter", 'id="facetFilter"' in index_html)
check("...and it is labelled for a screen reader, like every other control there",
      'aria-label="Filter add-ons by what they need or install as"' in index_html)

app_js = (_REPO / "webui" / "htdocs" / "app.js").read_text()
check("the palette filters on the selected facet", "(c.facets || []).includes(facet)" in app_js)
check("changing it re-renders the palette", '$("#facetFilter").addEventListener("change"' in app_js)
check("the dropdown is built from what the API sent, not from a copy in the browser",
      "state.facets = data.facets || []" in app_js)
check("the options are grouped by the field they come from", "group.label = x.group" in app_js)
check("no add-on tag vocabulary is left anywhere in the library",
      not hasattr(apps, "TAGS") and not hasattr(apps, "tags_for"))

if failures:
    print("{} check(s) failed".format(len(failures)))
    sys.exit(1)
print("all catalogue-facet checks passed")
