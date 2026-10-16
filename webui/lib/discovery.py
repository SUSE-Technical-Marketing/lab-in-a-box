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
import tempfile
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
    checkout (every addon, including install_ds389, lives in the
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
        return {"available": False, "hosts": [], "images": [], "images_by_host": {}, "config": {}}
    try:
        with open(p) as f:
            data = json.load(f)
    except (ValueError, OSError):
        return {"available": False, "hosts": [], "images": [], "images_by_host": {}, "config": {}}
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


_setup_credentials = None


def _credentials_lib():
    """scripts/setup_credentials.py, for its credential-field registries only — same loader as _schema_lib()."""
    global _setup_credentials
    if _setup_credentials is None:
        path = os.path.join(scripts_dir(), "setup_credentials.py")
        if not os.path.isfile(path):
            path = os.path.join(scripts_dir(), "setup_credentials")
        loader = SourceFileLoader("setup_credentials", path)
        spec = importlib.util.spec_from_loader("setup_credentials", loader)
        mod = importlib.util.module_from_spec(spec)
        loader.exec_module(mod)
        _setup_credentials = mod
    return _setup_credentials


def credential_types():
    """
    The fields each cloud provider and each service credential takes: {"cloud": {type: [{name, required, secret}]},
    "service": {...}}. Names and flags only, never a stored value, so this needs no login: the credentials dialog can
    show what a credential consists of before anyone authenticates. The stored files themselves stay behind the login,
    in the admin credentials action.
    """
    sc = _credentials_lib()
    table = lambda reg: {k: [{"name": n, "required": r, "secret": s} for n, r, s in v] for k, v in reg.items()}
    return {"cloud": table(sc.PROVIDER_FIELDS), "service": table(sc.SERVICE_CREDENTIAL_FIELDS)}


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
    requires_kubernetes/requires_cluster/provides_cluster/aux_services), same lazy-cached-import pattern as
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
    Resolve add-on `name` (install_<addon>) to its executable's path, whatever
    its file extension: an add-on may carry one (install_<addon>.sh), and
    install_automation_node_scripts.sh strips it on deploy.
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


def image_choices(snapshot=None):
    """
    The source images present on the KVM hosts right now, as the renderer's {value, label} enum entries: the value is
    the image's file name, which is what a lab definition stores, and the label is "<host> - <file name>", because with
    several hosts the same name is not necessarily present on all of them. Ordered by host, then by name.

    Built from images_by_host in the status snapshot refresh_hypervisor_status.py writes. An older snapshot that only
    has the flat "images" list still yields plain names, and no snapshot at all yields nothing, which leaves the field
    as free text.
    """
    snap = status() if snapshot is None else snapshot
    by_host = snap.get("images_by_host") or {}
    if by_host:
        return [{"value": name, "label": "{} - {}".format(host, name)}
                for host in sorted(by_host) for name in sorted(by_host[host] or [])]
    return list(snap.get("images") or [])


def host_choices(snapshot=None):
    """
    The configured KVM hosts, as the renderer's {value, label} enum entries, led by the empty value that means what
    omitting kvm_host means: let resolve_kvm_host() pick by free resources. A host the last refresh could not reach is
    labelled so, but stays selectable — pinning a node to a host that is temporarily down is still a valid choice.

    Empty when the status snapshot knows no host, which leaves the field as free text.
    """
    snap = status() if snapshot is None else snapshot
    hosts = [h for h in (snap.get("hosts") or []) if isinstance(h, dict) and h.get("host")]
    if not hosts:
        return []
    return [{"value": "", "label": "(default) auto — the host with the most free resources"}] + [
        {"value": h["host"], "label": h["host"] + (" (unreachable)" if h.get("error") else "")} for h in hosts]


def dynamic_enums(snapshot=None):
    """
    Live `enum` choices by field name, for the fields whose options are whatever the hypervisors currently have rather
    than anything the schema can state: the source image and the KVM host.
    """
    snap = status() if snapshot is None else snapshot
    images = image_choices(snap)
    return {"SOURCE_IMAGE": images, "ISO_IMAGE": images, "kvm_host": host_choices(snap)}


def _inject_dynamic_enums(node, choices):
    """
    Walk a schema tree (same Option-B convention app.js's renderer uses: any
    dict with name+type is a field, wherever it lives) and give each field named
    in `choices` (see dynamic_enums()) a live `enum` from the cached status
    snapshot. It then renders exactly like any other enum field. A field whose
    choices are empty is left alone, so it stays free text.
    """
    if isinstance(node, list):
        for n in node:
            _inject_dynamic_enums(n, choices)
        return
    if not isinstance(node, dict):
        return
    if _is_field(node):
        live = choices.get(node.get("name"))
        if live:
            node["enum"] = list(live)
        return
    for v in node.values():
        if isinstance(v, (dict, list)):
            _inject_dynamic_enums(v, choices)


def schema(name):
    """Schema of a single add-on, from its own `--schema json` output."""
    sc = json.loads(json.dumps(_apps_lib().describe(_def_path(name))))
    if not sc:
        raise FileNotFoundError(name)
    sc.setdefault("capabilities", _apps_lib().attach_capabilities({}, _apps_lib().DEFAULT_PLUGIN)["capabilities"])
    _inject_dynamic_enums(sc, dynamic_enums())
    return sc


def base_schema():
    """The base lab-definition schema (common/nodes/kclusters), via the library."""
    sc = _schema_lib().base_lab_schema()
    _inject_dynamic_enums(sc, dynamic_enums())
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
            "status": caps.get("status") or "stable",
            "status_note": caps.get("status_note") or "",
        })
    return sorted(items, key=lambda it: it["name"])


# Base-schema sections every lab has; the others are optional infrastructure services.
CORE_SECTIONS = ("common", "nodes", "kclusters")


EXAMPLES_DIR_ENV = "LABBUILDER_EXAMPLES_DIR"


def examples_dir():
    """Directory holding the lab definitions offered as starting points: the repository's own examples/."""
    d = os.environ.get(EXAMPLES_DIR_ENV)
    if d:
        return os.path.abspath(d)
    for cand in ("/usr/share/lab_creation/examples", os.path.join(_REPO, "examples")):
        if os.path.isdir(cand):
            return os.path.abspath(cand)
    return os.path.join(_REPO, "examples")


def _example_title(name):
    """A file name as a title: "harvester-aws.json.template" -> "harvester aws"."""
    return re.sub(r"\.(json|yaml|yml)(\.template)?$", "", name).replace("-", " ").replace("_", " ")


def examples():
    """
    The lab definitions offered as starting points, as [{"name", "title", "description", "nodes", "kclusters",
    "addons"}], sorted by name. Each one is a file of examples_dir() that parses as a lab definition with a "nodes"
    section; the counts let the UI say what a file holds without opening it. A file that does not parse is left out
    rather than failing the list — one broken example must not take the others with it.
    """
    out = []
    directory = examples_dir()
    for name in sorted(os.listdir(directory) if os.path.isdir(directory) else []):
        path = os.path.join(directory, name)
        if not os.path.isfile(path) or not re.search(r"\.(json|yaml|yml)(\.template)?$", name):
            continue
        definition, parse_error = _primary_lib().try_load_definition(path)
        if parse_error or not isinstance(definition, dict) or "nodes" not in definition:
            continue
        addons = set()
        owners = list((definition.get("kclusters") or {}).values()) + list((definition.get("nodes") or {}).values())
        if isinstance(definition.get("cluster"), dict):     # the older single-Kubernetes-cluster format
            owners.append(definition["cluster"])
        for owner in owners:
            for entry in (owner or {}).get("addons") or []:
                addons.add(_apps_lib().addon_entry_name(entry))
        out.append({
            "name": name,
            "title": _example_title(name),
            "description": (definition.get("common") or {}).get("lab_description", ""),
            "nodes": len(definition.get("nodes") or {}),
            "kclusters": len(definition.get("kclusters") or {}),
            "addons": sorted(addons),
        })
    return out


def example(name):
    """
    One example's lab definition, as a plain dict ready for the builder to open. `name` is a file name from
    examples(), never a path: anything with a separator in it, or that is not in that list, is refused.
    """
    if not name or os.path.basename(name) != name:
        raise ValueError("invalid example name: %r" % name)
    if name not in [e["name"] for e in examples()]:
        raise FileNotFoundError("no such example: %s" % name)
    definition, parse_error = _primary_lib().try_load_definition(os.path.join(examples_dir(), name))
    if parse_error:
        raise ValueError("%s does not parse: %s" % (name, parse_error))
    return dict(definition)


def catalogue():
    """
    Everything a lab can use, as items with name, kind, title, description, field_count, layers, targets, status and status_note. kind is
    "addon" (the add-ons of discover()), "infrastructure" (the base schema's optional sections, e.g. pxe) or
    "kcluster" (each Kubernetes cluster type, the kclusters clu_type values).
    """
    base = base_schema()["sections"]
    items = discover()
    for name, sec in sorted(base.items()):
        if name not in CORE_SECTIONS:
            items.append({"name": name, "kind": "infrastructure", "title": sec.get("title") or name,
                          "description": sec.get("description", ""), "field_count": len(sec.get("fields", [])),
                          "layers": [], "targets": []})
    clu_types = [f.get("enum") for f in base["kclusters"].get("fields", []) if f.get("name") == "clu_type"]
    if not clu_types or not clu_types[0]:
        raise ValueError("lab_schema: kclusters has no clu_type enum")
    for name in clu_types[0]:
        items.append({"name": name, "kind": "kcluster", "title": name, "description": "", "field_count": 0,
                      "layers": [], "targets": []})
    return items


_lab_creation = None


def _lab_creation_lib():
    """libs/lab_creation.py — the preflight validator, same lazy-cached import as _primary_lib()."""
    global _lab_creation
    if _lab_creation is None:
        d = libs_dir()
        if d not in sys.path:
            sys.path.insert(0, d)
        import lab_creation  # noqa: E402  (loaded from libs_dir)
        _lab_creation = lab_creation
    return _lab_creation


_libs_cache = {}


def _lib(name):
    """libs/<name>.py, imported from libs_dir() once and kept — the same lazy-cached pattern as _primary_lib(), for
    the libraries that need no wrapper of their own."""
    if name not in _libs_cache:
        d = libs_dir()
        if d not in sys.path:
            sys.path.insert(0, d)
        _libs_cache[name] = __import__(name)
    return _libs_cache[name]


def preview_addresses(definition):
    """
    What address each node with no myip would be given, as {node: ip}, using the same allocator the run itself uses
    (libs/ipalloc.py). Deterministic, so what this shows is what the run will do, and the ping gate is off because a
    browser cannot wait for it: the run's own probe and its preflight still have the last word.

    A node whose lab has no network to take an address from, and a cloud node whose provider assigns one, are left
    out of the answer rather than guessed at.
    """
    ipalloc = _lib("ipalloc")
    common = definition.get("common", {}) or {}
    if ipalloc.lab_network(common) is None:
        return {}
    cloud = set(_lib("backends").CLOUD_BACKEND_NAMES)
    out, taken = {}, set()
    for name, cfg in (definition.get("nodes") or {}).items():
        cfg = cfg or {}
        if str(cfg.get("myip") or "").strip():
            continue
        backend = str(cfg.get("backend") or common.get("backend") or "")
        if backend in cloud or cfg.get("cloud_account"):
            continue
        address = ipalloc.allocate(name, definition, used=taken,
                                   mydomain=str(common.get("mydomain") or ""), probe=False)
        if address:
            out[name] = address
            taken.add(address)
    return out


def validate_lab(definition):
    """
    Validate a full lab definition with the same preflight setup_lab.py runs (lab_creation.validate_lab_definition),
    so what this reports and what a deploy accepts cannot drift apart. Its host checks are off: this runs as the web
    server's user, which has no SSH access to the hypervisors, so a node's reachability and the source image on the
    host are not checked here.

    The definition is written to a temporary file first, because the preflight reads source_path for its report and
    passes that path to each referenced add-on's own --validate. The file is removed before returning.

    Returns {"ok": bool, "output": str} — the full report without terminal colour codes. A valid lab still reports its
    warnings in output.
    """
    primary = _primary_lib()
    lc = _lab_creation_lib()
    buf = io.StringIO()
    ok = True
    fd, path = tempfile.mkstemp(prefix="lab-builder-validate-", suffix=".json", dir=output_dir())
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(definition, fh)
        # The paths and host settings come from the status snapshot, which refresh_hypervisor_status.py writes with its
        # secret-shaped values masked. /etc/lab_creation.defaults is root-only and lab_creation.cfg can hold
        # credentials, so this never reads them: the web server's user has no business doing so, and the host checks
        # those settings drive are off here anyway.
        snapshot = status().get("config", {})
        config = {k: v for k, v in snapshot.items() if k in ("REMOTE_HOST", "KVM_HOSTS", "VIRT_SRV", "BACKEND")}
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            try:
                loaded = primary.load_definition(path)
                ok = lc.validate_lab_definition(
                    loaded, config,
                    snapshot.get("ISO_LOC") or "/var/lib/libvirt/images/sources",
                    os.environ.get("LABBUILDER_LAB_SETUP_PATH") or "/srv/www/htdocs/lab_creation",
                    vm_img_loc=(snapshot.get("VM_IMG_LOC") or "/var/lib/libvirt/images/").rstrip("/"),
                    host_checks=False,
                )
            except SystemExit:
                ok = False
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    return {"ok": bool(ok), "output": _ANSI.sub("", buf.getvalue()).strip()}


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
