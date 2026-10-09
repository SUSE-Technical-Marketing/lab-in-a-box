#!/usr/bin/env python3.11
"""
Runs the install path (main()) of every Python add-on in scripts/ with every external command faked: subprocess.run,
check_output, call, check_call and Popen record the command and succeed; time.sleep returns at once and advances the
clock that time.time and time.monotonic read, so deadline loops end. The lab has one rke2 kcluster "c1" with one server
node "vm1.mydemo.lab"; the add-on's section holds a placeholder for each required field without a default and the
add-on's entry in EXTRA_CONFIG. Each add-on runs in its own process, in a work directory holding lab_creation.defaults
(install paths pointing at the repo and the work directory) and templates/lab_creation.cfg.example as lab_creation.cfg;
DNS zone files go to the work directory, shutil.which finds every command and socket.create_connection succeeds
without connecting.

Each add-on runs twice: with its other fields unset, so they take the code's own fallbacks, and with every schema
default filled in. It passes when both runs exit 0, send at least one command and never run `helm install`, and both
send the same commands (generated values and temporary paths masked): a schema default must be what the code uses.
Add-ons listed in NOT_COVERED are reported by name, not run.

Usage: 79_addon_install_test.py                     run every add-on, exit 1 on any failure
       79_addon_install_test.py --one <f>           run add-on file <f> in the current directory, print one JSON line
       79_addon_install_test.py --one-defaults <f>  the same, with every schema default filled in
"""
import difflib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import traceback
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
TIMEOUT = 60

PLACEHOLDERS = {"integer": "1", "port": "8080", "boolean": "true", "url": "https://example.com/charts",
                "namespace": "ns1", "version": "1.0.0", "password": "Secret-123", "array": [], "object": {}}

# Section fields an add-on's code requires beyond its schema's required fields.
EXTRA_CONFIG = {
    "install_client_registration.py": {"client_registration_activation_key_base_channel": "sles15-sp7-pool-x86_64"},
    "install_hermes.py": {"hermes_llm_api_key": "key-123"},
    "install_prometheus.py": {"prometheus_scrape_configs": [{"job_name": "node", "targets": ["vm1.mydemo.lab:9100"]}]},
    "install_smlm.py": {"smlm_fqdn": "smlm.mydemo.lab", "smlm_scc_user": "scc-user", "smlm_scc_password": "Secret-123"},
    "install_smlm_proxy.py": {"smlm_proxy_server": "smlm.mydemo.lab", "smlm_proxy_scc_user": "scc-user",
                              "smlm_proxy_scc_password": "Secret-123"},
}

# Canned replies for commands an add-on reads or polls: add-on file name -> [(regex on the command, stdout)].
# COMMON_RESPONSES apply to every add-on, after its own.
RESPONSES = {
    "install_rancher.py": [(r"bootstrapPassword", "bootstrap-pw")],
    "install_uyuni.py": [(r"cat /tmp/mgradm_install\.rc", "0")],
}
COMMON_RESPONSES = [(r"%\{http_code\}", "200"), (r"source /etc/os-release", "sles|15.7|suse"),
                    (r"systemctl is-active", "active"), (r"\.State\.Health\.Status", "healthy")]

# Add-ons this check cannot run yet: add-on file name -> reason.
NOT_COVERED = {}


def lab_for(schema: dict, name: str, with_defaults: bool = False) -> dict:
    """
    A one-kcluster, one-node lab with `schema`'s section holding required-field placeholders and EXTRA_CONFIG, and
    every schema default when `with_defaults`.
    """
    section = {}
    for f in schema.get("fields", []):
        if f.get("default") not in (None, ""):
            if with_defaults:
                section[f["name"]] = f["default"]
        elif f.get("required"):
            section[f["name"]] = PLACEHOLDERS.get(f.get("type"), "value1")
    section.update(EXTRA_CONFIG.get(name, {}))
    sec = schema["section"]
    return {"common": {"online": "1"},
            "nodes": {"vm1.mydemo.lab": {"myip": "192.168.1.10", "kcluster": "c1", "INSTALL_RKE2_TYPE": "server",
                                         "addons": [sec]}},
            "kclusters": {"c1": {"clu_type": "rke2", "clu_rel": "stable", "mydomain": "mydemo.lab", "addons": [sec]}},
            sec: section}


def write_defaults(work: Path) -> None:
    """lab_creation.defaults in `work`, with its install paths pointing at the repo and at `work`."""
    paths = {"_lib_path": REPO / "libs", "_templ_addons_loc": str(REPO / "templates" / "addons") + "/",
             "LAB_SETUP_PATH": work / "www"}
    lines = []
    for line in (REPO / "lab_creation.defaults").read_text().splitlines():
        key = line.split("=", 1)[0]
        lines.append("{}='{}'".format(key, paths[key]) if key in paths else line)
    (work / "lab_creation.defaults").write_text("\n".join(lines) + "\n")


def run_one(path: str, with_defaults: bool = False) -> dict:
    """Run add-on `path`'s main() with commands faked; the result as a dict (rc, commands, error)."""
    import importlib.util
    import time
    from importlib.machinery import SourceFileLoader

    env = dict(os.environ, PATH="{}:{}".format(REPO / "scripts", os.environ.get("PATH", "")))
    out = subprocess.run([sys.executable, path, "--schema", "json"], stdout=subprocess.PIPE, universal_newlines=True,
                         env=env)
    schema = json.loads(out.stdout)
    name = Path(path).name
    with open("lab.json", "w") as fh:
        json.dump(lab_for(schema, name, with_defaults), fh)

    cmds = []
    replies = [(re.compile(rx), text) for rx, text in RESPONSES.get(name, []) + COMMON_RESPONSES]

    def record(args) -> str:
        cmd = args if isinstance(args, str) else " ".join(str(a) for a in args)
        cmds.append(cmd)
        return next((text for rx, text in replies if rx.search(cmd)), "")

    def as_output(text: str, kw: dict):
        return text if kw.get("universal_newlines") or kw.get("text") or kw.get("encoding") else text.encode()

    def fake_run(args, *a, **kw):
        text = record(args)
        return subprocess.CompletedProcess(args, 0, as_output(text, kw), as_output("", kw))

    def fake_check_output(args, *a, **kw):
        return as_output(record(args), kw)

    class FakePopen:
        def __init__(self, args, *a, **kw):
            text = record(args)
            self.args, self.returncode, self.pid, self.stdin, self.stderr = args, 0, 1, None, None
            self._out = as_output(text, kw)
            self.stdout = iter(self._out.splitlines(True))

        def communicate(self, *a, **kw):
            return self._out, None

        def wait(self, *a, **kw):
            return 0

        def poll(self):
            return 0

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    subprocess.run, subprocess.check_output, subprocess.Popen = fake_run, fake_check_output, FakePopen
    subprocess.call = lambda args, *a, **kw: (record(args), 0)[1]
    subprocess.check_call = subprocess.call
    clock = {"slept": 0.0}
    real_time, real_monotonic = time.time, time.monotonic
    time.sleep = lambda seconds, *a, **kw: clock.__setitem__("slept", clock["slept"] + seconds)
    time.time = lambda: real_time() + clock["slept"]
    time.monotonic = lambda: real_monotonic() + clock["slept"]

    shutil.which = lambda cmd, *a, **kw: "/usr/bin/" + cmd
    socket.create_connection = lambda *a, **kw: socket.socket()
    sys.path[:0] = [str(REPO / "libs"), str(REPO / "scripts")]
    import services
    services.NAMED_ZONE_DIR = Path("named").resolve()
    services.NAMED_ZONE_DIR.mkdir()
    os.environ.update({"_vm_name": "vm1.mydemo.lab", "clu_name": "c1"})
    sys.argv = [path, "lab.json"]
    loader = SourceFileLoader("addon_under_test", path)
    module = importlib.util.module_from_spec(importlib.util.spec_from_loader("addon_under_test", loader))
    rc, error = 0, ""
    try:
        loader.exec_module(module)
        module.main()
    except SystemExit as e:
        rc = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
    except Exception as e:  # noqa: BLE001 - any exception is a failure, reported with where it was raised
        frames = traceback.extract_tb(e.__traceback__)
        frame = ([f for f in frames if f.filename.startswith(str(REPO)) or f.filename == path] or frames)[-1]
        rc, error = 99, "{!r} at {}:{}".format(e, Path(frame.filename).name, frame.lineno)
    return {"rc": rc, "commands": cmds, "error": error}


def problems(result: dict) -> list:
    """Why `result` fails the check, as sentences."""
    out = []
    if result["rc"] != 0:
        out.append("exit code {}{}".format(result["rc"], ": " + result["error"] if result["error"] else ""))
    if not result["commands"]:
        out.append("sent no command")
    out += ["runs helm install: " + c[:160] for c in result["commands"] if re.search(r"\bhelm\s+install\b", c)]
    return out


def masked(commands: list) -> list:
    """`commands` with generated values (runs of 20+ token characters) and temporary paths replaced by <X>."""
    return [re.sub(r"/tmp/tmp\w+|[A-Za-z0-9+/=_-]{20,}", "<X>", c) for c in commands]


def run_isolated(path: str, mode: str):
    """Run `79_addon_install_test.py <mode> <path>` in a fresh work directory: (result or None, problems, output lines)."""
    work = tempfile.mkdtemp()
    write_defaults(Path(work))
    shutil.copy(str(REPO / "templates" / "lab_creation.cfg.example"), os.path.join(work, "lab_creation.cfg"))
    try:
        p = subprocess.run([sys.executable, __file__, mode, path], cwd=work, stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, universal_newlines=True, timeout=TIMEOUT)
        lines = p.stdout.strip().splitlines()
        try:
            result = json.loads(lines[-1])
            return result, problems(result), lines[:-1]
        except (IndexError, ValueError):
            return None, ["no result: " + " | ".join(lines[-3:])], lines
    except subprocess.TimeoutExpired:
        return None, ["still running after {}s".format(TIMEOUT)], []
    finally:
        shutil.rmtree(work, ignore_errors=True)


def main() -> int:
    if sys.argv[1:2] in (["--one"], ["--one-defaults"]):
        print(json.dumps(run_one(sys.argv[2], with_defaults=sys.argv[1] == "--one-defaults")))
        return 0
    addons = sorted(str(REPO / p) for p in subprocess.run(
        ["git", "-c", "safe.directory=*", "-C", str(REPO), "ls-files", "scripts/install_*.py"],
        stdout=subprocess.PIPE, universal_newlines=True, check=True).stdout.split())
    failed = 0
    for path in addons:
        name = Path(path).name
        if name in NOT_COVERED:
            print("not covered: {} ({})".format(name, NOT_COVERED[name]))
            continue
        unset, found, lines = run_isolated(path, "--one")
        filled, found_filled, lines_filled = run_isolated(path, "--one-defaults")
        found += ["with defaults filled in: " + f for f in found_filled]
        if unset and filled and not found:
            a, b = masked(unset["commands"]), masked(filled["commands"])
            diff = [ln for ln in difflib.unified_diff(a, b, lineterm="", n=0) if ln[:1] in "-+" and ln[:3] not in ("---", "+++")]
            if diff:
                found.append("schema defaults differ from the code's fallbacks (- unset, + defaults filled in)")
                lines = ["  " + ln[:200].replace("\n", " ") for ln in diff[:6]]
        if found:
            failed += 1
            print("FAIL: {}: {}".format(name, "; ".join(found)))
            for line in [ln for ln in lines + lines_filled if ln.startswith("  ") or re.search(r"ERROR|Error|error", ln)][:6]:
                print("      " + line)
        else:
            print("ok: {} ({} commands)".format(name, len(unset["commands"])))
    print("{} add-on(s) failed".format(failed) if failed else "all add-on install runs passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
