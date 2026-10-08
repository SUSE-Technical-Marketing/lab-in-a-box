#!/usr/bin/env python3.11
"""
Build the static, backend-free lab-builder page (published on GitHub Pages).

Takes webui/htdocs/index.html and embeds the answers the live lab-builder API
(webui/lib/api.py) gives for "components", "base" and every add-on's "schema",
computed from this repo checkout. An inline script replaces app.js's apiGet()
with lookups over that data; CSS hides the controls that need the server
(hypervisor status, image refresh, Validate, Save to server, Saved labs,
Create lab); the Credentials button is greyed out.

Usage: build-cube-static.py [--output PATH]   (default: webui/htdocs/lab-builder-static.html)

Exits non-zero when any API call fails or no add-on is found, so a page with
missing add-on data is never written.
"""
import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
HTDOCS = REPO / "webui" / "htdocs"

# The page reflects this checkout only: not copies installed under /usr/local on
# the build machine, and none of its hypervisor/image data (os.devnull is never a
# regular file, so discovery.status() reports "unavailable").
os.environ["LABBUILDER_SCRIPTS_DIR"] = str(REPO / "scripts")
os.environ["LABBUILDER_LIBS_DIR"] = str(REPO / "libs")
os.environ["LABBUILDER_STATUS_FILE"] = os.devnull
sys.path.insert(0, str(REPO / "webui" / "lib"))
import api  # noqa: E402

HIDE_SERVER_CONTROLS = """
  <style>
    #statusPanel, #refreshImagesBtn, #editorImages, #validateBtn, #saveBtn, #savedBtn, #createBtn { display: none !important; }
  </style>
"""

STATIC_API = """
  <script id="static-api-data" type="application/json">%s</script>
  <script>
  (function () {
    const data = JSON.parse(document.getElementById("static-api-data").textContent);
    const copy = (v) => JSON.parse(JSON.stringify(v));
    window.LAB_STATIC = true;
    window.apiGet = async function (action, params = {}) {
      if (action === "components") return copy(data.components);
      if (action === "base") return copy(data.base);
      if (action === "status") return { available: false, hosts: [], images: [], config: {} };
      if (action === "schema") {
        if (!Object.prototype.hasOwnProperty.call(data.schemas, params.name)) throw new Error("unknown add-on: " + params.name);
        return copy(data.schemas[params.name]);
      }
      throw new Error(action + " needs the lab-builder server");
    };
  })();
  </script>
"""


def git_version() -> str:
    r = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=REPO, capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else "unknown"


def api_get(action: str, **params: str) -> dict:
    """One GET through the live API's dispatcher; exits on any non-200 answer."""
    status, body = api.dispatch(action, "GET", {k: [v] for k, v in params.items()}, b"")
    if status != 200:
        sys.exit("build-cube-static: API {} {} failed: {}".format(action, params, body.get("error")))
    return body


def collect(version: str) -> dict:
    """Every GET answer the page needs, keyed the way the static apiGet() looks them up."""
    components = api_get("components")
    if not components["count"]:
        sys.exit("build-cube-static: no add-ons found in {}".format(REPO / "scripts"))
    components["scripts_dir"] = "lab-in-a-box " + version
    components.pop("libs_dir", None)
    schemas = {c["name"]: api_get("schema", name=c["name"]) for c in components["components"]}
    return {"components": components, "base": api_get("base"), "schemas": schemas}


def build(data: dict, version: str) -> str:
    html = (HTDOCS / "index.html").read_text().replace("__LABVERSION__", version)
    payload = json.dumps(data).replace("</", "<\\/")
    head_end = html.index("</head>")
    html = html[:head_end] + HIDE_SERVER_CONTROLS + html[head_end:]
    body_end = html.rindex("</body>")
    return html[:body_end] + STATIC_API % payload + html[body_end:]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    parser.add_argument("--output", type=Path, default=HTDOCS / "lab-builder-static.html",
                        help="file to write (default: webui/htdocs/lab-builder-static.html)")
    args = parser.parse_args()

    version = git_version()
    data = collect(version)
    args.output.write_text(build(data, version))
    fields = sum(c["field_count"] for c in data["components"]["components"])
    print("built {}: {} add-ons, {} fields, version {}".format(
        args.output, data["components"]["count"], fields, version), file=sys.stderr)


if __name__ == "__main__":
    main()
