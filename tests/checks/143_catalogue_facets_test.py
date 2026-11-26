#!/usr/bin/env python3
# Unit tests for the catalogue's facets: webui/lib/discovery.py's FACETS/facets_of()/facets(), what the
# components action sends, and the web UI's filter being wired to it. Nothing here is a list somebody maintains:
# a facet is a fact read off an add-on's PLUGIN capabilities, so what is checked is the deriving. Run from
# 143_catalogue_facets.sh, in its own container — see tests/run_tests.sh.
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "libs"))
sys.path.insert(0, str(_REPO / "webui" / "lib"))

import discovery  # noqa: E402

failures = []


def check(desc, cond):
    if not cond:
        failures.append(desc)
        print("FAIL:", desc)


# ── facets_of(): each one is read off the capabilities, nothing else ───────
check("an add-on that installs on a cluster is on a cluster",
      discovery.facets_of({"layers": ["kubernetes"]}) == ["kubernetes"])
check("one that installs both ways carries both facets",
      discovery.facets_of({"layers": ["kubernetes", "standalone-container"]})
      == ["kubernetes", "standalone-container"])
check("an OS-native add-on is distinguished from a container on a host, which the palette's own grouping merges",
      discovery.facets_of({"layers": ["os-native"]}) == ["os-native"])
check("what it needs of a cluster is a facet of its own",
      discovery.facets_of({"requires_cluster": ["ingress", "storageclass"]})
      == ["needs-ingress", "needs-storageclass"])
check("so is what it supplies, which is the question 'who can give me an ingress' asks",
      discovery.facets_of({"provides_cluster": ["ingress"]}) == ["provides-ingress"])
check("an add-on tied to particular Kubernetes distributions says so",
      discovery.facets_of({"requires_kubernetes": ["rke2", "k3s"]}) == ["distribution-specific"])
check("...and one that works on any does not", discovery.facets_of({"requires_kubernetes": None}) == [])
check("an add-on needing a lab-wide service on the automation node says so",
      discovery.facets_of({"aux_services": ["pxe"]}) == ["lab-service"])
check("one that implements --check is findable, since that is what makes a lab verifiable",
      discovery.facets_of({"check": True}) == ["checkable"])
check("an unfinished add-on is findable too",
      discovery.facets_of({"status": "under-construction"}) == ["under-construction"])
check("a stable one is not", discovery.facets_of({"status": "stable"}) == [])
check("the facets come out in a fixed order, so the dropdown reads the same every time",
      discovery.facets_of({"check": True, "layers": ["kubernetes"]}) == ["kubernetes", "checkable"])
check("an item with no capabilities at all carries no facet rather than failing",
      discovery.facets_of({}) == [])

check("every facet has a title for the dropdown to show",
      all(name and title for name, title, _holds in discovery.FACETS))
check("facets() offers them in FACETS order, with the names the items carry",
      [f["name"] for f in discovery.facets()] == [name for name, _t, _h in discovery.FACETS])


# ── the catalogue: everything it offers is findable ───────────────────────
cat = discovery.catalogue()
check("the catalogue was built at all, so an empty result is not read as everything passing", len(cat) > 50)
unfindable = sorted(it["name"] for it in cat if not it.get("facets"))
check("every item carries at least one facet, or a filter would hide it for good: {}".format(
    ", ".join(unfindable) or "none"), not unfindable)
check("every facet an item carries is one the dropdown offers",
      not {f for it in cat for f in it["facets"]} - {name for name, _t, _h in discovery.FACETS})
check("an add-on's facets come from its own capabilities",
      any(it["name"] == "install_longhorn" and "provides-storageclass" in it["facets"] for it in cat))
check("a Kubernetes distribution is offered as such, since a lab starts by choosing one",
      all("kubernetes" in it["facets"] for it in cat if it["kind"] == "kcluster"))
check("an optional base-schema section reads as the lab-wide service it is",
      all("lab-service" in it["facets"] for it in cat if it["kind"] == "infrastructure"))
check("the capability fields a facet is read from are sent with each add-on, so the browser could too",
      all(set(("requires_cluster", "provides_cluster", "check")) <= set(it)
          for it in cat if it["kind"] == "addon"))


# ── what the browser is handed, and what it does with it ─────────────────
import api as webui_api  # noqa: E402

st, body = webui_api.dispatch("components", "GET", {}, b"", user=None, https=False)
check("the components action sends the vocabulary beside the items",
      st == 200 and [f["name"] for f in body["facets"]] == [n for n, _t, _h in discovery.FACETS])
check("...and every component it sends carries its own facets",
      all(c.get("facets") for c in body["components"]))

index_html = (_REPO / "webui" / "htdocs" / "index.html").read_text()
check("the catalogue panel has the filter", 'id="facetFilter"' in index_html)
check("...and it is labelled for a screen reader, like every other control there",
      'aria-label="Filter add-ons by what they need or install as"' in index_html)

app_js = (_REPO / "webui" / "htdocs" / "app.js").read_text()
check("the palette filters on the selected facet", "(c.facets || []).includes(facet)" in app_js)
check("changing it re-renders the palette", '$("#facetFilter").addEventListener("change"' in app_js)
check("the dropdown is built from what the API sent, not from a copy in the browser",
      "state.facets = data.facets || []" in app_js)
check("no add-on tag vocabulary is left anywhere in the library",
      not hasattr(__import__("apps"), "TAGS") and not hasattr(__import__("apps"), "tags_for"))

if failures:
    print("{} check(s) failed".format(len(failures)))
    sys.exit(1)
print("all catalogue-facet checks passed")
