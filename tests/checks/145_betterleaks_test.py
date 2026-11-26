#!/usr/bin/env python3
# Unit tests for scripts/install_betterleaks. What is asserted is the part that is its own: the command line it
# writes for a 1.x and for a 2.x release, since upstream changed it between them, and that everything else goes
# through the same libs/secret_scan.py functions install_gitleaks uses (covered by 144_gitleaks.sh). Mocked
# ssh_run, no real SSH and no container. Run from 145_betterleaks.sh, in its own container.
import json
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "libs"))
sys.path.insert(0, str(_REPO / "scripts"))

import addon_common as ac  # noqa: E402
import apps  # noqa: E402
import secret_scan  # noqa: E402

bl = apps.load_addon_module("install_betterleaks", str(_REPO / "scripts"))  # no .py: loaded by path
TOOL = bl.TOOL

failures = []


def check(desc, cond):
    if not cond:
        failures.append(desc)
        print("FAIL:", desc)


class FakeResult(object):
    def __init__(self, returncode=0):
        self.returncode = returncode
        self.stdout = ""
        self.stderr = ""


calls = []
logs = []


def _fake_ssh_run(hostname, cmd, check=True, input_text=None, capture=False, user="root"):
    calls.append((hostname, cmd, input_text))
    return FakeResult()


secret_scan.ssh_run = _fake_ssh_run
secret_scan.ssh_output = lambda hostname, cmd, *a, **kw: ""
secret_scan.ensure_podman = lambda host: True
secret_scan.log = lambda m: logs.append(m)
secret_scan.warn = lambda m: logs.append(m)


def sent():
    return "\n".join(c[1] for c in calls)


# ── the tool it describes to the shared plumbing ──────────────────────────
check("it is its own lab section, so its fields never collide with gitleaks'",
      TOOL.name == "betterleaks" and TOOL.field("scan_path") == "betterleaks_scan_path")
check("the default is the latest stable release, not a release candidate",
      TOOL.version == "1.10.1" and bl.major_of({}) == 1)
check("the image is upstream's own", TOOL.image == "ghcr.io/betterleaks/betterleaks")
check("an unset tag is the version, tagged the way the images are",
      secret_scan.image_of(TOOL, {}) == "ghcr.io/betterleaks/betterleaks:v1.10.1")
check("the report lands under the tool's own name, so two scanners on one node do not overwrite each other",
      TOOL.report == "/var/log/betterleaks/betterleaks-report.json")

check("the major version decides the dialect", bl.major_of({"betterleaks_version": "2.0.0-rc.15"}) == 2)
check("a version written with the v is read the same way", bl.major_of({"betterleaks_version": "v2.1.0"}) == 2)
check("a version that is not a number at all is treated as the 1.x line rather than crashing",
      bl.major_of({"betterleaks_version": "latest"}) == 1)


# ── 1.x: the Gitleaks command line ───────────────────────────────────────
args = bl.arguments({}, "/scan", config_path="/betterleaks.toml", report_path="/report/r.json")
check("1.x scans a directory with 'dir', as Gitleaks does", args[:2] == ["dir", "/scan"])
check("1.x takes the banner off and redacts by default",
      "--no-banner" in args and "--redact" in args)
check("1.x writes the report with --report-format and --report-path",
      args[args.index("--report-format") + 1] == "json"
      and args[args.index("--report-path") + 1] == "/report/r.json")
check("the configuration file is passed in either version",
      args[args.index("--config") + 1] == "/betterleaks.toml")

args = bl.arguments({"betterleaks_baseline_path": "/scan/base.json"}, "/scan")
check("1.x accepts a baseline", args[args.index("--baseline-path") + 1] == "/scan/base.json")

args = bl.arguments({"betterleaks_command": "git", "betterleaks_redact": "false"}, "/scan")
check("1.x scans history with 'git' and leaves the secrets in when the lab asked for that",
      args[0] == "git" and "--redact" not in args)


# ── 2.x: upstream's new command line ─────────────────────────────────────
V2 = {"betterleaks_version": "2.0.0-rc.15"}
args = bl.arguments(dict(V2), "/scan", config_path="/betterleaks.toml", report_path="/report/r.json")
check("2.x renames the directory scan to 'fs', from the same lab field", args[:2] == ["fs", "/scan"])
check("2.x writes the report with --output, whose extension chooses the format",
      args[args.index("--output") + 1] == "/report/r.json"
      and "--report-format" not in args and "--report-path" not in args)
check("2.x is not given --no-banner, which it no longer documents", "--no-banner" not in args)
check("2.x still redacts and still takes a configuration file",
      "--redact" in args and args[args.index("--config") + 1] == "/betterleaks.toml")

args = bl.arguments(dict(V2, betterleaks_command="git"), "/scan")
check("a history scan is 'git' in both versions", args[0] == "git")

args = bl.arguments(dict(V2, betterleaks_baseline_path="/scan/base.json"), "/scan")
check("2.x is never handed --baseline-path, which upstream removed", "--baseline-path" not in args)


# ── the escape hatch ─────────────────────────────────────────────────────
args = bl.arguments({"betterleaks_extra_args": ["--max-archive-depth", "2"]}, "/scan")
check("extra arguments are appended as given, for a flag this add-on has no field for",
      args[-2:] == ["--max-archive-depth", "2"])
check("one extra argument written as a string counts as a list of one",
      bl.arguments({"betterleaks_extra_args": "--no-color"}, "/scan")[-1] == "--no-color")


# ── --validate ───────────────────────────────────────────────────────────
def errors_for(section):
    v = ac.Validator({"betterleaks": section})
    bl._validate(v)
    return "\n".join(v.errors)


check("container mode without a path to scan is refused, as it is for every scanner",
      "betterleaks_scan_path is required" in errors_for({}))
check("kubernetes mode without repositories is refused",
      "betterleaks_repos is required" in errors_for({"betterleaks_mode": "kubernetes"}))
check("a complete container configuration passes", errors_for({"betterleaks_scan_path": "/srv/code"}) == "")
check("a baseline named against a 2.x version is refused rather than quietly dropped",
      "only exists on Betterleaks 1.x" in errors_for(dict(V2, betterleaks_scan_path="/srv/code",
                                                          betterleaks_baseline_path="/scan/base.json")))
check("...and accepted against a 1.x one",
      errors_for({"betterleaks_scan_path": "/srv/code", "betterleaks_baseline_path": "/scan/base.json"}) == "")
check("extra arguments written as something other than a list are refused",
      "must be a list" in errors_for({"betterleaks_scan_path": "/srv/code",
                                      "betterleaks_extra_args": {"a": 1}}))


# ── it runs on the shared plumbing ───────────────────────────────────────
del calls[:]
secret_scan.run_container(TOOL, "n1.mydemo.lab", {"betterleaks_scan_path": "/srv/code"})
check("the container mode pulls and runs the Betterleaks image over the lab's directory",
      "podman pull ghcr.io/betterleaks/betterleaks:v1.10.1" in sent()
      and "-v /srv/code:/scan:ro" in sent() and "fs /scan" not in sent() and "dir /scan" in sent())
check("its report directory is its own", "mkdir -p /var/log/betterleaks" in sent())

del calls[:]
secret_scan.install_cronjob(TOOL, "n1.mydemo.lab", {"betterleaks_repos": ["https://example.com/a.git"]},
                            str(_REPO / "templates" / "addons"))
applied = [c[2] for c in calls if c[1] == "kubectl apply -f -" and c[2]]
manifest = applied[0] if applied else ""
check("the CronJob is named after this tool, not after the one the template was written for",
      "name: betterleaks" in manifest and "name: gitleaks" not in manifest)
check("it carries this tool's own arguments and image",
      json.dumps(bl.arguments({}, "/repos")) in manifest
      and "ghcr.io/betterleaks/betterleaks:v1.10.1" in manifest)
check("its first run is a job of its own name",
      "create job betterleaks-first --from=cronjob/betterleaks" in sent())
check("the namespace defaults to the tool's name", "betterleaks" in sent())


# ── the plugin the registry reads ───────────────────────────────────────
check("it declares the two layers it installs as",
      set(bl.PLUGIN["layers"]) == {"standalone-container", "kubernetes"})
check("it needs nothing of the cluster", bl.PLUGIN["requires_cluster"] == [])
check("it implements --check", bl.PLUGIN["check"] is True)
check("it is classified as security tooling", apps.tags_for("betterleaks") == ["security", "devops"])

if failures:
    print("{} check(s) failed".format(len(failures)))
    sys.exit(1)
print("all betterleaks checks passed")
