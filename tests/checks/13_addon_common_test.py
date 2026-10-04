#!/usr/bin/env python3
# Pure-logic unit tests for libs/addon_common.py — the CLI
# scaffolding shared by every install_<addon>.py script. No SSH/subprocess
# needed except for print_schema's lab_schema delegation, which is mocked.
# Run from 13_addon_common.sh, in its own container — see tests/run_tests.sh.
import io
import json
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "libs"))

import addon_common as ac  # noqa: E402

failures = []


def check(desc, cond):
    if not cond:
        failures.append(desc)
        print("FAIL:", desc)


def _tmpjson(data):
    f = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
    json.dump(data, f)
    f.close()
    return f.name


# ── Validator field checks ────────────────────────────────────────────────────
v = ac.Validator({"mariadb": {}})
v.vreq("mariadb", "mariadb_pass")
check("vreq: required-and-missing field produces one error", len(v.errors) == 1)
check("vreq: error names section.field and 'no default'",
      "mariadb.mariadb_pass" in v.errors[0] and "no default" in v.errors[0])

v = ac.Validator({"mariadb": {"mariadb_pass": "x"}})
v.vreq("mariadb", "mariadb_pass")
check("vreq: present field produces no error", v.errors == [])

v = ac.Validator({"mariadb": {"mariadb_ns": "Not_Valid!"}})
v.vns("mariadb")
check("vns: invalid k8s namespace produces an error", len(v.errors) == 1)
v = ac.Validator({"mariadb": {"mariadb_ns": "valid-ns1"}})
v.vns("mariadb")
check("vns: valid namespace produces no error", v.errors == [])
v = ac.Validator({"mariadb": {}})
v.vns("mariadb")
check("vns: unset field is not required, no error", v.errors == [])

v = ac.Validator({"mariadb": {"mariadb_version": "not-a-version"}})
v.vver("mariadb")
check("vver: malformed version produces an error", len(v.errors) == 1)
v = ac.Validator({"mariadb": {"mariadb_version": "10.11"}})
v.vver("mariadb")
check("vver: X.Y version passes", v.errors == [])

v = ac.Validator({"mariadb": {"mariadb_repo_url": "ftp://nope"}})
v.vurl("mariadb")
check("vurl: non-http(s) URL produces an error", len(v.errors) == 1)
v = ac.Validator({"mariadb": {"mariadb_repo_url": "https://example.com/repo"}})
v.vurl("mariadb")
check("vurl: https:// URL passes", v.errors == [])

v = ac.Validator({"mariadb": {"mariadb_ha": "yes"}})
v.vbool("mariadb", "mariadb_ha")
check("vbool: non true/false value produces an error", len(v.errors) == 1)
v = ac.Validator({"mariadb": {"mariadb_ha": "true"}})
v.vbool("mariadb", "mariadb_ha")
check("vbool: 'true' passes", v.errors == [])

v = ac.Validator({"mariadb": {"mariadb_port": "abc"}})
v.vport("mariadb", "mariadb_port")
check("vport: non-numeric port produces an error", len(v.errors) == 1)
v = ac.Validator({"mariadb": {"mariadb_port": "3306"}})
v.vport("mariadb", "mariadb_port")
check("vport: numeric port passes", v.errors == [])


# ── run_validate ───────────────────────────────────────────────────────────
def _check_fn(v):
    v.vreq("mariadb", "mariadb_pass")
    v.vport("mariadb", "mariadb_port")


path = _tmpjson({"mariadb": {"mariadb_port": "not-a-port"}})
buf = io.StringIO()
with redirect_stdout(buf):
    rc = ac.run_validate(path, _check_fn)
check("run_validate: returns the error count as the exit code", rc == 2)
check("run_validate: prints each [ERROR] line", buf.getvalue().count("[ERROR]") == 2)

path = _tmpjson({"mariadb": {"mariadb_pass": "x", "mariadb_port": "3306"}})
rc = ac.run_validate(path, _check_fn)
check("run_validate: zero errors -> exit code 0", rc == 0)

buf = io.StringIO()
with redirect_stdout(buf):
    rc = ac.run_validate("/nonexistent.json", _check_fn)
check("run_validate: unreadable file -> exit code 1, not a crash", rc == 1)
check("run_validate: reports the read failure as an [ERROR]", "[ERROR]" in buf.getvalue())

# ── run_validate: format auto-detection (JSON vs YAML) ──────────────────────
# Used to hardcode json.loads() directly, bypassing primary's JSON/YAML
# auto-detection — a .yaml lab file would always be reported as unreadable/
# unparseable even when valid. Covers the fix (primary.try_load_definition).
try:
    import yaml as _yaml_probe  # noqa: F401
    _has_yaml = True
except ImportError:
    _has_yaml = False

f = tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False)
f.write("mariadb:\n  mariadb_pass: x\n  mariadb_port: '3306'\n")
f.close()
rc = ac.run_validate(f.name, _check_fn)
if _has_yaml:
    check("run_validate: valid .yaml input parses and validates -> exit code 0", rc == 0)
else:
    check("run_validate: .yaml input without pyyaml installed fails gracefully (not a crash)", rc == 1)


# ── print_help ─────────────────────────────────────────────────────────────
script_text = '''#!/usr/bin/env python3
# Part of lab-in-a-box
#
# JSON section: mariadb
#   "mariadb": {
#     "mariadb_pass": "the root password"
#   }
import sys
print("not executed by print_help")
'''
f = tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False)
f.write(script_text)
f.close()

buf = io.StringIO()
with redirect_stdout(buf):
    ac.print_help(f.name, usage="Usage: install_mariadb <lab.json>")
out = buf.getvalue()
check("print_help: prints the given usage line", "Usage: install_mariadb <lab.json>" in out)
check("print_help: re-emits the JSON section comment block", "mariadb_pass" in out)
check("print_help: strips the leading '#' from each doc line",
      not any(line.startswith("#") for line in out.splitlines()))
check("print_help: stops at the first non-comment line (doesn't leak the script body)",
      "not executed by print_help" not in out)

buf = io.StringIO()
with redirect_stdout(buf):
    ac.print_help(f.name)
check("print_help: falls back to a generic usage line when none is given",
      "Usage:" in buf.getvalue())


# ── print_schema: parses via lab_schema in-process, attaches PLUGIN
#    capabilities, emits in the requested format ─────────────────────────────
class _FakeLabSchema:
    def parse_script(self, path):
        return {"section": "mariadb", "fields": [], "_path": path}

    def _emit(self, schema, fmt):
        print("EMIT[{}] {}".format(fmt, sorted(schema.keys())))


ac._lab_schema_mod = _FakeLabSchema()
buf = io.StringIO()
with redirect_stdout(buf):
    rc = ac.print_schema("/usr/local/bin/install_mariadb", "yaml",
                          plugin={"targets": ["container"], "layers": ["kubernetes"]})
check("print_schema: emits in the requested format with capabilities attached",
      buf.getvalue().strip() == "EMIT[yaml] ['_path', 'capabilities', 'fields', 'section']")
check("print_schema: returns 0", rc == 0)
ac._lab_schema_mod = None


# ── handle_common_args: top-level dispatch ───────────────────────────────────
def _dispatch(argv, **kwargs):
    old_argv = sys.argv
    sys.argv = ["install_mariadb"] + argv
    buf = io.StringIO()
    code = None
    try:
        with redirect_stdout(buf):
            ac.handle_common_args("/usr/local/bin/install_mariadb", "1.2.3", **kwargs)
    except SystemExit as e:
        code = e.code
    finally:
        sys.argv = old_argv
    return code, buf.getvalue()


code, out = _dispatch(["--version"])
check("handle_common_args --version: exits 0", code == 0)
check("handle_common_args --version: prints name and version", out.strip() == "install_mariadb 1.2.3")

code, out = _dispatch(["-v"])
check("handle_common_args -v: same as --version", code == 0 and "1.2.3" in out)

path = _tmpjson({"mariadb": {"mariadb_pass": "x", "mariadb_port": "3306"}})
code, out = _dispatch(["--validate", path], validate_fn=_check_fn)
check("handle_common_args --validate: exits with the validator's error count", code == 0)

path = _tmpjson({"mariadb": {"mariadb_port": "not-a-port"}})
code, out = _dispatch(["--validate", path], validate_fn=_check_fn)
check("handle_common_args --validate: non-zero exit when the definition has errors", code == 2)

code, out = _dispatch(["--validate"], validate_fn=_check_fn)
check("handle_common_args --validate: missing path argument exits 1", code == 1)

code, out = _dispatch(["--validate", path])  # no validate_fn given
check("handle_common_args --validate: exits 0 when the addon defines no validate_fn", code == 0)

ac.print_help = lambda script_path, usage=None: print("HELP TEXT")
code, out = _dispatch(["--help"])
check("handle_common_args --help: exits 0 and calls print_help", code == 0 and "HELP TEXT" in out)

ac.print_schema = lambda script_path, fmt, plugin=None: 0
code, out = _dispatch(["--input-definition", "yaml"])
check("handle_common_args --input-definition: exits with print_schema's return code", code == 0)
code, out = _dispatch(["--schema"])
check("handle_common_args --schema: defaults format to json when omitted", code == 0)

code, out = _dispatch(["--capabilities"], plugin={"name": "mariadb", "targets": ["container"]})
check("handle_common_args --capabilities: exits 0 printing the plugin as JSON",
      code == 0 and json.loads(out) == {"name": "mariadb", "targets": ["container"]})
code, out = _dispatch(["--capabilities"])
check("handle_common_args --capabilities: prints {} when no plugin is passed",
      code == 0 and json.loads(out) == {})

code, out = _dispatch(["some-unrelated-arg"])
check("handle_common_args: returns None (no exit) for an addon-specific argument", code is None)


# ── require_k8s_name(): runtime guard for values interpolated unquoted into
# remote kubectl/shell commands. Validator.vns()'s format check is not invoked by the
# deploy pipeline, so this runs at
# the point of use instead) ──────────────────────────────────────────────
check("require_k8s_name: a valid lowercase-alphanumeric-plus-hyphens value passes through unchanged",
      ac.require_k8s_name({"ns": "my-ns1"}, "ns", "default") == "my-ns1")
check("require_k8s_name: a missing/empty value falls back to the given default",
      ac.require_k8s_name({}, "ns", "default-ns") == "default-ns")

_require_k8s_name_died = False
try:
    ac.require_k8s_name({"ns": "bad;rm -rf /"}, "ns", "default")
except SystemExit:
    _require_k8s_name_died = True
check("require_k8s_name: a value with a shell metacharacter exits rather than being "
      "returned for interpolation into a remote command",
      _require_k8s_name_died)

_require_k8s_name_died = False
try:
    ac.require_k8s_name({"ns": "Has-Upper-Case"}, "ns", "default")
except SystemExit:
    _require_k8s_name_died = True
check("require_k8s_name: uppercase (not a valid k8s name) is rejected too", _require_k8s_name_died)


if failures:
    print("{} check(s) failed".format(len(failures)))
    sys.exit(1)
print("all addon_common checks passed")
