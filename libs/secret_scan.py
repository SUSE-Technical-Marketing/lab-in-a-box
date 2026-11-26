#!/usr/bin/env python3
# Part of lab-in-a-box — the plumbing a secret scanner add-on shares.
# Author/s: Raul Mahiques
# License: GPLv3
"""
libs/secret_scan.py: what install_gitleaks and install_betterleaks have in common.

Both run one scanner in the same two shapes:

    container    podman on each node that lists the add-on, over a directory mounted read-only, with the report
                 written to the node and, when the lab names a schedule, the same command written to cron.d.
    kubernetes   a CronJob on the kcluster: an init container clones the lab's repositories into an emptyDir and
                 the scanner's container scans them. No volume claim, so it needs nothing of the cluster.

Everything about that is the same for both tools except the tool's name, its image and how its command line is
written, which is what a Tool carries. An add-on is then its header comment (the fields), its PLUGIN, a function
that writes its command line, and a few lines of dispatch.

The field names of a tool's lab section are its name and a suffix, so "gitleaks" reads gitleaks_scan_path and
"betterleaks" reads betterleaks_scan_path. Tool.field() is the one place that is spelled.
"""

import json
import posixpath
import shlex

import addon_common as ac
from lab_creation import ssh_run, ssh_output, ensure_podman, process_template, die, log, warn

MODES = ("container", "kubernetes")
DEFAULT_GIT_IMAGE = "alpine/git"
DEFAULT_SCHEDULE = "0 3 * * *"
CRONJOB_TEMPLATE = "secret_scan/cronjob.yml.tmpl"
# Where a container sees what the node mounted into it. Fixed, so a lab's own paths never have to agree with them.
SCAN_PATH = "/scan"
REPORT_DIR = "/report"
REPOS_PATH = "/repos"


class Tool(object):
    """
    One scanner: its name (the lab section, and the prefix of every field of it), the image that runs it, the
    version that image is tagged with, and `arguments`, which writes its command line.

    arguments(cfg, path, config_path="", report_path="") returns the argument list for one scan, every path being
    one the container sees. It is the only thing the two tools genuinely disagree about: Gitleaks and Betterleaks
    v1 take "dir ... --report-path", Betterleaks v2 takes "fs ... --output".
    """

    def __init__(self, name, image, version, arguments, report, display="", git_image=DEFAULT_GIT_IMAGE):
        self.name = name
        self.image = image
        self.version = version
        self.arguments = arguments
        self.report = report
        self.display = display or name
        self.git_image = git_image

    def field(self, suffix):
        """The lab field `suffix` of this tool: field("scan_path") is "gitleaks_scan_path"."""
        return "{}_{}".format(self.name, suffix)

    def get(self, cfg, suffix, default=""):
        """This tool's `suffix` field from `cfg`, or `default` when it is unset or empty."""
        value = (cfg or {}).get(self.field(suffix))
        return default if value in ("", None) else value


def mode_of(tool, cfg):
    """The mode this configuration asks for, normalised; "container" when it asks for none."""
    return str(tool.get(cfg, "mode", "container")).strip().lower()


def list_field(tool, cfg, suffix):
    """A list-valued field as a list of non-empty strings. A single string counts as a list of one."""
    value = tool.get(cfg, suffix, [])
    if isinstance(value, str):
        value = [value]
    return [str(v).strip() for v in (value or []) if str(v).strip()]


def truth(tool, cfg, suffix, default):
    """A "true"/"false" field as a bool, `default` when it is unset."""
    return str(tool.get(cfg, suffix, default)).strip().lower() == "true"


def version_of(tool, cfg):
    """The release the lab asked for, without the "v", or the tool's own default."""
    return str(tool.get(cfg, "version", tool.version)).lstrip("v")


def image_of(tool, cfg):
    """The image and tag this lab runs. An unset tag is "v" and the version, as both tools' images are tagged."""
    return "{}:{}".format(tool.get(cfg, "image", tool.image),
                          tool.get(cfg, "image_tag", "v{}".format(version_of(tool, cfg))))


def report_path_of(tool, cfg):
    """Where the container mode writes its report on the node."""
    return str(tool.get(cfg, "report_path", tool.report))


def validate(tool, v):
    """
    The checks both add-ons' --validate makes: the version and namespace shapes, the booleans, the list field,
    and what each mode cannot do without. An add-on adds whatever only it knows on top.
    """
    cfg = v.definition.get(tool.name, {}) or {}
    mode = mode_of(tool, cfg)
    v.vver(tool.name)
    v.vns(tool.name)
    v.vbool(tool.name, tool.field("redact"))
    v.vbool(tool.name, tool.field("fail_on_leaks"))

    value = cfg.get(tool.field("repos"))
    if value not in ("", None) and not isinstance(value, (list, str)):
        v.errors.append("[ERROR] {}.{} must be a list of strings".format(tool.name, tool.field("repos")))

    if mode == "kubernetes" and not list_field(tool, cfg, "repos"):
        v.errors.append("[ERROR] {}.{} is required when {} is 'kubernetes' (the CronJob has nothing to scan "
                        "without it)".format(tool.name, tool.field("repos"), tool.field("mode")))
    if mode == "container" and not cfg.get(tool.field("scan_path")):
        v.errors.append("[ERROR] {}.{} is required when {} is 'container' (the container scans the directory "
                        "mounted into it)".format(tool.name, tool.field("scan_path"), tool.field("mode")))
    if mode == "kubernetes" and cfg.get(tool.field("scan_path")):
        v.errors.append("[ERROR] {}.{} has no meaning when {} is 'kubernetes': the CronJob scans {}, which it "
                        "clones itself".format(tool.name, tool.field("scan_path"), tool.field("mode"),
                                               tool.field("repos")))


def report_outcome(tool, cfg, returncode, what):
    """
    What a finished scan means. Both tools exit 1 for leaks found and for an error alike, so the outcome is
    reported as the one thing that is certain, and <tool>_fail_on_leaks decides whether it stops the lab.
    """
    if returncode == 0:
        log("- {}: no secrets found".format(what))
        return
    message = "{}: {} exited {} — it found secrets, or the scan itself failed. The report and the log above say " \
              "which.".format(what, tool.display, returncode)
    if truth(tool, cfg, "fail_on_leaks", "false"):
        die(message)
    warn("- " + message)


def cron_file(tool):
    """The cron.d file this tool's scheduled scan is written to."""
    return "/etc/cron.d/{}".format(tool.name)


def install_cron(tool, hostname, command, schedule):
    """Write cron.d so `command` repeats on `schedule`, and report that it did."""
    content = ("# Written by lab-in-a-box install_{}\n"
               "SHELL=/bin/sh\n"
               "PATH=/usr/local/sbin:/usr/local/bin:/sbin:/bin:/usr/sbin:/usr/bin\n"
               "{} root {}\n").format(tool.name, schedule, command)
    ssh_run(hostname, "cat > {}".format(shlex.quote(cron_file(tool))), input_text=content)
    ssh_run(hostname, "chmod 0644 {}".format(shlex.quote(cron_file(tool))))
    log("- Scheduled it on {} as '{}' in {}".format(hostname, schedule, cron_file(tool)))


def container_command(tool, cfg):
    """
    The podman command one scan runs, built from the lab's own fields. The image's entrypoint is the scanner, so
    the arguments are the tool's command line, written against the paths the container sees rather than the node's.
    """
    report = report_path_of(tool, cfg)
    mounts = ["-v {}:{}:ro".format(shlex.quote(str(tool.get(cfg, "scan_path", ""))), SCAN_PATH),
              "-v {}:{}".format(shlex.quote(posixpath.dirname(report) or "/"), REPORT_DIR)]
    config_path = str(tool.get(cfg, "config", ""))
    in_container_config = ""
    if config_path:
        in_container_config = "/{}.toml".format(tool.name)
        mounts.append("-v {}:{}:ro".format(shlex.quote(config_path), in_container_config))
    args = tool.arguments(cfg, SCAN_PATH, config_path=in_container_config,
                          report_path=posixpath.join(REPORT_DIR, posixpath.basename(report)))
    return "podman run --rm {} {} {}".format(" ".join(mounts), shlex.quote(image_of(tool, cfg)),
                                             " ".join(shlex.quote(a) for a in args))


def run_container(tool, hostname, cfg):
    """Pull the image on `hostname` and run one scan of the directory the lab names, then schedule it if asked."""
    image = image_of(tool, cfg)
    report = report_path_of(tool, cfg)
    ensure_podman(hostname)
    ssh_run(hostname, "mkdir -p {}".format(shlex.quote(posixpath.dirname(report) or "/")))
    ssh_run(hostname, "podman pull {}".format(shlex.quote(image)))
    command = container_command(tool, cfg)
    result = ssh_run(hostname, command, check=False)
    report_outcome(tool, cfg, result.returncode,
                   "{} on {}, from {}".format(tool.get(cfg, "scan_path", ""), hostname, image))
    log("- Report written to {} on {}".format(report, hostname))
    schedule = str(tool.get(cfg, "schedule", "")).strip()
    if schedule:
        install_cron(tool, hostname, command, schedule)


def clone_script(repos):
    """
    The init container's command: clone each repository shallowly under its own name. A clone that fails stops the
    job rather than letting the scan report a clean tree it never saw.
    """
    lines = ["set -e"]
    for url in repos:
        name = posixpath.basename(str(url).rstrip("/"))
        if name.endswith(".git"):
            name = name[:-4]
        lines.append("git clone --depth 1 {} {}".format(
            shlex.quote(str(url)), shlex.quote(posixpath.join(REPOS_PATH, name or "repo"))))
    return "\n".join(lines)


def install_cronjob(tool, hostname, cfg, templ_addons_loc):
    """
    Install the CronJob on the cluster `hostname` is a server of, and start one run at once so that a broken
    configuration is found now rather than at the first scheduled run. The report is the job's own output.
    """
    namespace = ac.require_k8s_name(cfg, tool.field("ns"), tool.name)
    repos = list_field(tool, cfg, "repos")
    schedule = str(tool.get(cfg, "schedule", DEFAULT_SCHEDULE))
    render = {
        "scan_name": tool.name,
        "scan_ns": namespace,
        "scan_schedule": schedule,
        "scan_image": image_of(tool, cfg),
        "scan_git_image": str(tool.get(cfg, "git_image", tool.git_image)),
        "scan_repos_path": REPOS_PATH,
        # JSON is valid YAML, so both of these go in as one flow value each and need no quoting of their own.
        "scan_args_json": json.dumps(tool.arguments(cfg, REPOS_PATH)),
        "scan_clone_json": json.dumps(clone_script(repos)),
    }
    ssh_run(hostname, ac.namespace_command(namespace))
    template = "{}/{}".format(str(templ_addons_loc).rstrip("/"), CRONJOB_TEMPLATE)
    ssh_run(hostname, "kubectl apply -f -", input_text=process_template(template, render))
    log("- CronJob '{}' installed in namespace '{}', scanning {} on '{}'".format(
        tool.name, namespace, ", ".join(repos), schedule))

    first = "{}-first".format(tool.name)
    ssh_run(hostname, "kubectl -n {} delete job {} --ignore-not-found".format(
        shlex.quote(namespace), shlex.quote(first)), check=False)
    result = ssh_run(hostname, "kubectl -n {ns} create job {job} --from=cronjob/{name} && "
                               "kubectl -n {ns} wait --for=condition=complete --timeout=600s job/{job}".format(
                                   ns=shlex.quote(namespace), job=shlex.quote(first), name=shlex.quote(tool.name)),
                     check=False)
    if result.returncode == 0:
        log("- Its first run found no secrets. Later runs: kubectl -n {} logs job/<name>".format(namespace))
    else:
        report_outcome(tool, cfg, result.returncode,
                       "the first run of the CronJob (kubectl -n {} logs job/{})".format(namespace, first))


def check(tool, chk):
    """
    What is deployed against the lab's own section: in the container mode the image on the node, and in the
    kubernetes mode the CronJob and the schedule it runs on.
    """
    if tool.name not in chk.definition:
        chk.ok("this lab has no {} section, so there is nothing to check".format(tool.name))
        return
    cfg = chk.definition.get(tool.name, {}) or {}

    if mode_of(tool, cfg) == "kubernetes":
        namespace = chk.get(tool.name, tool.field("ns"), tool.name)
        found = ssh_output(chk.vm_name, "kubectl -n {} get cronjob {} -o jsonpath={{.spec.schedule}} 2>/dev/null".format(
            shlex.quote(namespace), shlex.quote(tool.name))).strip()
        if not found:
            chk.fail("the {} CronJob in namespace '{}'".format(tool.name, namespace), "present", "absent")
        else:
            chk.expect("the {} CronJob's schedule".format(tool.name),
                       tool.get(cfg, "schedule", DEFAULT_SCHEDULE), found)
        return

    image = image_of(tool, cfg)
    if ssh_run(chk.vm_name, "podman image exists {}".format(shlex.quote(image)), check=False).returncode == 0:
        chk.ok("the image {} is on {}".format(image, chk.vm_name))
    else:
        chk.fail("the image {} on {}".format(image, chk.vm_name), "present", "absent")
