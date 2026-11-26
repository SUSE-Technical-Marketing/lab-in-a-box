#!/usr/bin/env python3
# Unit tests for scripts/install_gitleaks — mocked ssh_run/ssh_output, no real SSH and no container. What is
# asserted is which commands each of the two modes would send, that the scan flags come from the lab definition,
# and that a found secret only stops the lab when the lab asked it to. The CronJob template is rendered with the
# real process_template(). Run from 144_gitleaks.sh, in its own container — see tests/run_tests.sh.
import json
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "libs"))
sys.path.insert(0, str(_REPO / "scripts"))

import addon_common as ac  # noqa: E402
import apps  # noqa: E402

gl = apps.load_addon_module("install_gitleaks", str(_REPO / "scripts"))  # no .py: loaded by path

failures = []


def check(desc, cond):
    if not cond:
        failures.append(desc)
        print("FAIL:", desc)


class FakeResult(object):
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


calls = []
returncodes = {}          # substring of the command -> the return code it answers with
outputs = {}
logs = []
warnings = []
died = []


def _fake_ssh_run(hostname, cmd, check=True, input_text=None, capture=False, user="root"):
    calls.append((hostname, cmd, input_text))
    for needle, code in returncodes.items():
        if needle in cmd:
            return FakeResult(returncode=code)
    return FakeResult()


def _fake_ssh_output(hostname, cmd, *a, **kw):
    calls.append((hostname, cmd, None))
    for needle, text in outputs.items():
        if needle in cmd:
            return text
    return ""


class _Died(Exception):
    pass


def _fake_die(message):
    died.append(message)
    raise _Died(message)


gl.ssh_run = _fake_ssh_run
gl.ssh_output = _fake_ssh_output
gl.ensure_podman = lambda host: True
gl.log = lambda m: logs.append(m)
gl.warn = lambda m: warnings.append(m)
gl.die = _fake_die


def reset():
    del calls[:]
    del logs[:]
    del warnings[:]
    del died[:]
    returncodes.clear()
    outputs.clear()


def sent():
    """Every command the run would have sent, as one string, for substring assertions."""
    return "\n".join(c[1] for c in calls)


# ── the mode, the image, and the list fields a lab writes either way ───────
check("the mode defaults to container, the shape that needs nothing installed on the node",
      gl.mode_of({}) == "container")
check("the mode is read case-insensitively, as every other string flag of a definition is",
      gl.mode_of({"gitleaks_mode": " Kubernetes "}) == "kubernetes")
check("the two modes the header documents are the two there are", gl.MODES == ("container", "kubernetes"))
check("an unset tag is the version, tagged the way the images are",
      gl.image_of({}) == "{}:v{}".format(gl._DEFAULT_IMAGE, gl._DEFAULT_VERSION))
check("the image and tag the lab names are the ones used",
      gl.image_of({"gitleaks_image": "docker.io/zricethezav/gitleaks", "gitleaks_image_tag": "latest"})
      == "docker.io/zricethezav/gitleaks:latest")
check("a version written with the v is not given two",
      gl.image_of({"gitleaks_version": "v8.20.0"}) == "{}:v8.20.0".format(gl._DEFAULT_IMAGE))
check("a list field written as one string counts as a list of one",
      gl._list_field({"gitleaks_repos": "https://example.com/a.git"}, "gitleaks_repos")
      == ["https://example.com/a.git"])
check("blank entries are dropped, so a trailing comma in the lab does not become a repository",
      gl._list_field({"gitleaks_repos": ["https://example.com/a.git", "", "  "]}, "gitleaks_repos")
      == ["https://example.com/a.git"])
check("a missing list field is no list at all", gl._list_field({}, "gitleaks_repos") == [])


# ── scan_arguments(): one place turns the lab's fields into flags ──────────
args = gl.scan_arguments({}, "/scan")
check("the default scan is a directory scan with the banner off and secrets redacted",
      args[:2] == ["dir", "/scan"] and "--no-banner" in args and "--redact" in args)
check("nothing is reported anywhere until the caller says where",
      "--report-path" not in args and "--config" not in args)

args = gl.scan_arguments({"gitleaks_command": "git", "gitleaks_redact": "false",
                          "gitleaks_baseline_path": "/scan/base.json"},
                         "/scan", config_path="/gitleaks.toml", report_path="/report/r.json")
check("gitleaks_command git scans the history instead of the files", args[0] == "git")
check("gitleaks_redact false leaves the secrets in the report, because the lab asked for that",
      "--redact" not in args)
check("the configuration file, the baseline and the report path are all passed through",
      args[args.index("--config") + 1] == "/gitleaks.toml"
      and args[args.index("--baseline-path") + 1] == "/scan/base.json"
      and args[args.index("--report-path") + 1] == "/report/r.json"
      and args[args.index("--report-format") + 1] == "json")


# ── _report_outcome(): a found secret stops the lab only when asked ───────
reset()
gl._report_outcome({}, 0, "a clean tree")
check("a clean scan is reported and nothing else happens", logs and not warnings and not died)

reset()
gl._report_outcome({}, 1, "a dirty tree")
check("by default a finding warns and the lab carries on, which is what a demo wants",
      warnings and not died)

reset()
try:
    gl._report_outcome({"gitleaks_fail_on_leaks": "true"}, 1, "a dirty tree")
except _Died:
    pass
check("gitleaks_fail_on_leaks true makes the same finding fail the add-on", died)


# ── container mode ────────────────────────────────────────────────────────
reset()
gl.setup_container("n1.mydemo.lab", {"gitleaks_scan_path": "/srv/code"})
check("the image is pulled before it is run",
      "podman pull {}:v{}".format(gl._DEFAULT_IMAGE, gl._DEFAULT_VERSION) in sent())
check("the directory to scan is mounted read-only, and the report directory writable",
      "-v /srv/code:/scan:ro" in sent() and "-v /var/log/gitleaks:/report" in sent())
check("the container scans what it was given, under the path it sees",
      "dir /scan" in sent() and "--report-path /report/gitleaks-report.json" in sent())
check("the report's directory is created first, since Gitleaks will not create it",
      "mkdir -p /var/log/gitleaks" in sent())
check("the container is removed after the run, so a repeat does not collide on the name",
      "podman run --rm" in sent())
check("nothing is scheduled when the lab named no schedule", gl._CRON_FILE not in sent())
check("the node is told where its report went", any("Report written to" in m for m in logs))

reset()
gl.setup_container("n1.mydemo.lab", {"gitleaks_scan_path": "/srv/code", "gitleaks_config": "/etc/gl.toml",
                                     "gitleaks_image": "docker.io/zricethezav/gitleaks",
                                     "gitleaks_image_tag": "latest"})
check("a configuration file is mounted in and named by the path the container sees",
      "-v /etc/gl.toml:/gitleaks.toml:ro" in sent() and "--config /gitleaks.toml" in sent())
check("the image the lab names is the one pulled and run",
      "docker.io/zricethezav/gitleaks:latest" in sent())

reset()
gl.setup_container("n1.mydemo.lab", {"gitleaks_scan_path": "/srv/code", "gitleaks_schedule": "30 2 * * 1"})
cron = [c for c in calls if gl._CRON_FILE in c[1] and c[2]]
check("a schedule writes the same podman command into cron.d, which is what the node already runs",
      cron and "30 2 * * 1 root podman run --rm" in cron[0][2])
check("the cron file is given a mode cron will accept", "chmod 0644 {}".format(gl._CRON_FILE) in sent())

reset()
returncodes["podman run"] = 1
gl.setup_container("n1.mydemo.lab", {"gitleaks_scan_path": "/srv/code"})
check("a scan that found something does not stop the run by itself", warnings and not died)


# ── kubernetes mode ──────────────────────────────────────────────────────
check("clone_script shallow-clones each repository under its own name",
      "git clone --depth 1 https://example.com/a.git /repos/a" in gl.clone_script(["https://example.com/a.git"]))
check("...and stops on the first failure, so a scan never reports on a tree it did not get",
      gl.clone_script(["https://example.com/a.git"]).startswith("set -e"))

reset()
templates = str(_REPO / "templates" / "addons")
gl.setup_kubernetes("n1.mydemo.lab", {"gitleaks_repos": ["https://example.com/a.git"],
                                      "gitleaks_schedule": "15 1 * * *"}, templates)
applied = [c[2] for c in calls if c[1] == "kubectl apply -f -" and c[2]]
check("the manifest is applied over stdin, never written to a file on the node", applied)
manifest = applied[0] if applied else ""
check("it is a CronJob on the schedule the lab asked for",
      "kind: CronJob" in manifest and 'schedule: "15 1 * * *"' in manifest)
check("the namespace is created first", any("namespace" in c[1] for c in calls))
check("the init container clones into the shared volume and the scan container reads it",
      "/repos" in manifest and "emptyDir" in manifest)
check("the scan arguments are the same ones the container mode builds",
      json.dumps(gl.scan_arguments({}, "/repos")) in manifest)
check("both images are pinned: the clone image and the Gitleaks one",
      gl._DEFAULT_GIT_IMAGE in manifest and gl.image_of({}) in manifest)
check("one run is started at once, so a broken configuration is found now and not at 01:15",
      "create job gitleaks-first --from=cronjob/gitleaks" in sent())
check("that first job is waited for rather than assumed", "wait --for=condition=complete" in sent())

reset()
gl.setup_kubernetes("n1.mydemo.lab", {"gitleaks_repos": ["https://example.com/a.git"]}, templates)
applied = [c[2] for c in calls if c[1] == "kubectl apply -f -" and c[2]]
check("a lab that named no schedule gets the documented default",
      applied and 'schedule: "{}"'.format(gl._DEFAULT_SCHEDULE) in applied[0])

reset()
returncodes["create job gitleaks-first"] = 1
gl.setup_kubernetes("n1.mydemo.lab", {"gitleaks_repos": ["https://example.com/a.git"]}, templates)
check("a first run that found something warns and names the log to read, without failing the lab",
      warnings and "logs job/gitleaks-first" in warnings[0] and not died)


# ── --validate: what each mode needs, before anything is run ─────────────
def errors_for(section):
    v = ac.Validator({"gitleaks": section})
    gl._validate(v)
    return "\n".join(v.errors)


check("kubernetes mode without repositories is refused, naming the field",
      "gitleaks_repos is required" in errors_for({"gitleaks_mode": "kubernetes"}))
check("container mode without a path to scan is refused",
      "gitleaks_scan_path is required" in errors_for({"gitleaks_mode": "container"}))
check("...which is also what a lab that named no mode at all is told",
      "gitleaks_scan_path is required" in errors_for({}))
check("a complete container configuration passes",
      errors_for({"gitleaks_scan_path": "/srv/code"}) == "")
check("a path to scan in kubernetes mode is refused rather than silently ignored",
      "no meaning" in errors_for({"gitleaks_mode": "kubernetes", "gitleaks_repos": ["https://e/a.git"],
                                  "gitleaks_scan_path": "/srv"}))
check("a version that is not one is refused", "not a valid version" in errors_for({"gitleaks_version": "latest"}))
check("a namespace that Kubernetes would refuse is refused here",
      "invalid namespace" in errors_for({"gitleaks_mode": "kubernetes", "gitleaks_repos": ["https://e/a.git"],
                                         "gitleaks_ns": "Not A Namespace"}))
check("a list field written as something else is refused",
      "must be a list" in errors_for({"gitleaks_repos": {"a": 1}}))


# ── --check: what each mode compares ────────────────────────────────────
reset()
chk = ac.Checker({"nodes": {}}, vm_name="n1.mydemo.lab")
gl._check(chk)
check("a lab with no gitleaks section is not a failure and asks the node nothing",
      not chk.problems and not calls)

reset()
chk = ac.Checker({"gitleaks": {"gitleaks_scan_path": "/srv/code"}}, vm_name="n1.mydemo.lab")
gl._check(chk)
check("container mode asks podman whether the image is on the node",
      "podman image exists {}".format(gl.image_of({})) in sent() and not chk.problems)

reset()
returncodes["podman image exists"] = 1
chk = ac.Checker({"gitleaks": {"gitleaks_scan_path": "/srv/code"}}, vm_name="n1.mydemo.lab")
gl._check(chk)
check("an image that is not there is reported as the difference it is", chk.problems)

reset()
outputs["jsonpath"] = "0 3 * * *"
chk = ac.Checker({"gitleaks": {"gitleaks_mode": "kubernetes", "gitleaks_repos": ["https://e/a.git"]}},
                 vm_name="n1.mydemo.lab")
gl._check(chk)
check("kubernetes mode compares the CronJob's own schedule with the lab's", not chk.problems)

reset()
outputs["jsonpath"] = "0 4 * * *"
chk = ac.Checker({"gitleaks": {"gitleaks_mode": "kubernetes", "gitleaks_repos": ["https://e/a.git"]}},
                 vm_name="n1.mydemo.lab")
gl._check(chk)
check("...and reports a CronJob running on another one", chk.problems)

reset()
chk = ac.Checker({"gitleaks": {"gitleaks_mode": "kubernetes", "gitleaks_repos": ["https://e/a.git"]}},
                 vm_name="n1.mydemo.lab")
gl._check(chk)
check("a CronJob that is not there is a failure, not a missing schedule", chk.problems)


# ── the plugin the registry reads ───────────────────────────────────────
check("it declares the two layers it installs as, which is what the palette groups it by",
      set(gl.PLUGIN["layers"]) == {"standalone-container", "kubernetes"})
check("it can be attached to a cluster or to a VM",
      set(gl.PLUGIN["targets"]) == {"container", "vm", "baremetal"})
check("it needs nothing of the cluster: the CronJob uses no ingress and no storage class",
      gl.PLUGIN["requires_cluster"] == [])
check("it implements --check", gl.PLUGIN["check"] is True)
check("it is classified as security tooling", apps.tags_for("gitleaks") == ["security", "devops"])

if failures:
    print("{} check(s) failed".format(len(failures)))
    sys.exit(1)
print("all gitleaks checks passed")
