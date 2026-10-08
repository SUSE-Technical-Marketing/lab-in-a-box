"""
lab-builder — runtime introspection of lab-in-a-box, driven by the project's
own Python libraries (no subprocess fan-out to shell scripts).

Add-ons are executables named install_<addon> in any language, with or
without a file extension; each one describes itself through its own
`--schema json` output (libs/apps.describe(), cached). In-process it imports:
  * scripts/lab_schema  -> base_lab_schema()   (schema of common/nodes/kclusters)
  * libs/primary        -> validate_definition (structural lab validation)
  * libs/apps           -> describe()/addon_files() (add-on discovery)

It holds NO knowledge of any specific addon or field: components and their
fields are discovered at run time from the add-ons themselves, so adding a
new install_* executable (or a field to one) appears automatically.

Configuration (all optional, env-overridable):
  LABBUILDER_SCRIPTS_DIR   dir holding install_* definitions + lab_schema
                           (auto: /usr/local/bin, else <repo>/scripts)
  LABBUILDER_LIBS_DIR      dir holding the python libs package
                           (auto: /usr/local/lib/lab_creation, else <repo>/libs)
  LABBUILDER_OUTPUT_DIR    where generated labs are written (~/.lab-builder/labs)
"""
import contextlib
import importlib.util
import io
import json
import os
import re
import sys
from importlib.machinery import SourceFileLoader

# Only definitions with these name shapes are ever introspected.
SCRIPT_RE = re.compile(r'^install_[A-Za-z0-9._-]+$')
_ANSI = re.compile(r'\x1b\[[0-9;]*m')

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.abspath(os.path.join(_HERE, "..", ".."))


# ── configuration / path resolution ───────────────────────────────────────────

def scripts_dir():
    """Directory holding every install_<addon> definition + lab_schema — a
    single deployed /usr/local/bin in production, scripts/ in a repo
    checkout (every addon, including install_ds389.py, lives in the
    same directory)."""
    d = os.environ.get("LABBUILDER_SCRIPTS_DIR")
    if d:
        return os.path.abspath(d)
    for cand in ("/usr/local/bin", os.path.join(_REPO, "scripts")):
        if os.path.isfile(os.path.join(cand, "lab_schema")):
            return os.path.abspath(cand)
    return "/usr/local/bin"


def addon_dirs():
    """
    Directories to scan for install_<addon> definitions — just
    [scripts_dir()] now that every addon (ported or not) lives in one place
    in both production and a repo checkout; kept as a list (rather than
    inlining scripts_dir() at every call site) since discover()/_def_path()
    scan it as a sequence.
    """
    return [scripts_dir()]


def libs_dir():
    d = os.environ.get("LABBUILDER_LIBS_DIR")
    if d:
        return os.path.abspath(d)
    for cand in ("/usr/local/lib/lab_creation", os.path.join(_REPO, "libs")):
        if os.path.isfile(os.path.join(cand, "primary.py")):
            return os.path.abspath(cand)
    return os.path.join(_REPO, "libs")


def output_dir():
    d = os.environ.get("LABBUILDER_OUTPUT_DIR") or os.path.expanduser("~/.lab-builder/labs")
    os.makedirs(d, exist_ok=True)
    return d


def status_file():
    return os.environ.get("LABBUILDER_STATUS_FILE") or "/srv/www/lab-builder/status.json"


def status():
    """
    Cached hypervisor status snapshot (hosts/images/config) — refreshed
    periodically by scripts/refresh_hypervisor_status.py, run as root. The
    CGI never queries the hypervisor itself (see webui/apache/lab-builder.conf's
    header: it never runs anything privileged, and root's own SSH key isn't
    readable by the Apache user anyway) — this just reads the JSON that
    script last wrote. Returns an "unavailable" shape if no snapshot exists
    yet (fresh install, refresh hasn't run) rather than erroring.
    """
    p = status_file()
    if not os.path.isfile(p):
        return {"available": False, "hosts": [], "images": [], "config": {}}
    try:
        with open(p) as f:
            data = json.load(f)
    except (ValueError, OSError):
        return {"available": False, "hosts": [], "images": [], "config": {}}
    data["available"] = True
    return data


# ── lazy, cached in-process imports of the project libraries ───────────────────

_lab_schema = None
_primary = None


def _schema_lib():
    global _lab_schema
    if _lab_schema is None:
        path = os.path.join(scripts_dir(), "lab_schema")
        loader = SourceFileLoader("lab_schema", path)
        spec = importlib.util.spec_from_loader("lab_schema", loader)
        mod = importlib.util.module_from_spec(spec)
        loader.exec_module(mod)
        _lab_schema = mod
    return _lab_schema


def _primary_lib():
    global _primary
    if _primary is None:
        d = libs_dir()
        if d not in sys.path:
            sys.path.insert(0, d)
        import primary  # noqa: E402  (loaded from libs_dir)
        _primary = primary
    return _primary


_apps = None


def _apps_lib():
    """
    libs/apps.py — an addon's PLUGIN capabilities (targets/layers/
    requires_kubernetes/aux_services), same lazy-cached-import pattern as
    _primary_lib(). Uses load_plugin_from_path() (an explicit file path),
    not load_plugin()'s shutil.which() PATH lookup — a repo-checkout addon
    like scripts/install_<x>.py isn't on $PATH, but
    discovery.py already has its real path from addon_dirs()/_def_path().
    """
    global _apps
    if _apps is None:
        d = libs_dir()
        if d not in sys.path:
            sys.path.insert(0, d)
        import apps  # noqa: E402  (loaded from libs_dir)
        _apps = apps
    return _apps


# ── introspection API (all in-process) ────────────────────────────────────────

def _def_path(name):
    """
    Resolve add-on `name` (install_<addon>) to its executable's path, with or
    without a file extension: a repo checkout keeps install_<addon>.py,
    production (install_automation_node_scripts.sh) strips the extension on
    deploy.
    """
    if not SCRIPT_RE.match(name or ""):
        raise ValueError("invalid component name: %r" % name)
    for d in addon_dirs():
        path = _apps_lib().addon_files(d).get(name)
        if path:
            return path
    raise FileNotFoundError(name)


def _is_field(node):
    return isinstance(node, dict) and isinstance(node.get("name"), str) and isinstance(node.get("type"), str)


def _inject_dynamic_enums(node, images):
    """
    Walk a schema tree (same Option-B convention app.js's renderer uses: any
    dict with name+type is a field, wherever it lives) and give any field
    literally named ISO_IMAGE a live `enum` of the images actually present on
    the hypervisor right now, from the cached status snapshot. It then
    renders exactly like any other enum field — no frontend changes needed.
    A no-op when no snapshot is available yet (leaves the field as free text).
    """
    if isinstance(node, list):
        for n in node:
            _inject_dynamic_enums(n, images)
        return
    if not isinstance(node, dict):
        return
    if _is_field(node):
        if node.get("name") == "ISO_IMAGE" and images:
            node["enum"] = list(images)
        return
    for v in node.values():
        if isinstance(v, (dict, list)):
            _inject_dynamic_enums(v, images)


def schema(name):
    """Schema of a single add-on, from its own `--schema json` output."""
    sc = json.loads(json.dumps(_apps_lib().describe(_def_path(name))))
    if not sc:
        raise FileNotFoundError(name)
    sc.setdefault("capabilities", _apps_lib().attach_capabilities({}, _apps_lib().DEFAULT_PLUGIN)["capabilities"])
    _inject_dynamic_enums(sc, status().get("images", []))
    return sc


def base_schema():
    """The base lab-definition schema (common/nodes/kclusters), via the library."""
    sc = _schema_lib().base_lab_schema()
    _inject_dynamic_enums(sc, status().get("images", []))
    return sc


def discover():
    """Every install_* add-on whose `--schema json` output has a section or fields."""
    apps = _apps_lib()
    found = {}
    for d in addon_dirs():
        for name, path in apps.addon_files(d).items():
            found.setdefault(name, path)
    described = apps.describe_many(found.values())
    items = []
    for name, path in found.items():
        sc = described.get(os.path.realpath(path)) or {}
        if not sc.get("section") and not sc.get("fields"):
            continue
        caps = sc.get("capabilities") or {}
        items.append({
            "name": name,
            "kind": "addon",
            "title": sc.get("section") or name,
            "description": sc.get("description", ""),
            "field_count": len(sc.get("fields", [])),
            "layers": caps.get("layers") or [],
            "targets": caps.get("targets") or [],
        })
    return sorted(items, key=lambda it: it["name"])


def validate_lab(definition):
    """Validate a full lab definition via libs/primary (captures its stderr)."""
    buf = io.StringIO()
    ok = True
    try:
        with contextlib.redirect_stderr(buf):
            _primary_lib().validate_definition(definition, "lab.json")
    except SystemExit:
        ok = False
    return {"ok": ok, "output": _ANSI.sub("", buf.getvalue()).strip()}


def _safe_name(filename):
    base = os.path.basename(filename or "")
    if not re.match(r'^[A-Za-z0-9._-]+$', base):
        raise ValueError("invalid filename")
    return base if base.endswith(".json") else base + ".json"


def save_lab(filename, config):
    path = os.path.join(output_dir(), _safe_name(filename))
    with open(path, "w") as f:
        json.dump(config, f, indent=2)
        f.write("\n")
    return path
