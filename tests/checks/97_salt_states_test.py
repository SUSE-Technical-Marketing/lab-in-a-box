#!/usr/bin/env python3.11
# Custom Salt states and formulas on an SMLM/Uyuni server — see 97_salt_states.sh.
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import mock

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "libs"))

import spacecmd_common as sc  # noqa: E402

failures = []


def check(desc, cond):
    if not cond:
        failures.append(desc)
        print("FAIL:", desc)


class _Run:
    """
    Records every command ensure_salt_state() runs through the server's exec_prefix. `is_tar` answers the probe
    that decides whether the downloaded file is an archive, and `fails` makes one command fail, for the paths that
    only show up when something goes wrong.
    """

    def __init__(self, stamp="", is_tar=True, fails=""):
        self.cmds = []
        self.stamp = stamp
        self.is_tar = is_tar
        self.fails = fails

    def __call__(self, hostname, exec_prefix, cmd, **kwargs):
        self.cmds.append(cmd)
        if self.fails and self.fails in cmd:
            if kwargs.get("check", True):
                raise RuntimeError("command failed: " + cmd[:40])
            return mock.Mock(returncode=1, stdout="")
        if cmd.startswith("cat "):
            return mock.Mock(returncode=0 if self.stamp else 1, stdout=self.stamp)
        if cmd.startswith("tar -tf "):
            return mock.Mock(returncode=0 if self.is_tar else 1, stdout="")
        return mock.Mock(returncode=0, stdout="")

    def joined(self):
        return "\n".join(self.cmds)


SHA = "0123456789abcdef" * 4

# ── a tarball ────────────────────────────────────────────────────────────────
runner = _Run()
with mock.patch.object(sc, "_run", runner):
    installed = sc.ensure_salt_state("smlm.lab", "mgrctl exec --", "mystate",
                                     url="https://example.com/mystate.tar.gz", sha256=SHA)
text = runner.joined()
check("a tarball is installed", installed is True)
check("it is fetched with its checksum verified first",
      "curl -fsSL -o source" in text and "sha256sum -c -" in text
      and text.index("sha256sum") < text.index("tar -xf"))
check("it is unpacked into /srv/salt/<name>", "tar -xf /tmp/lab-in-a-box-salt-mystate/source -C /srv/salt/mystate" in text)
check("what it is is decided by reading the file, not by the URL's extension",
      "tar -tf /tmp/lab-in-a-box-salt-mystate/source" in text)
check("an archive holding one top-level directory has that wrapper stripped",
      "--strip-components=1" in text and "cut -d/ -f1 | sort -u | wc -l" in text)
check("the directory is replaced, not merged into", "rm -rf /srv/salt/mystate" in text)
check("the source is stamped so a re-run can tell", ".lab-in-a-box.source" in text and SHA in text)
check("the work directory is cleaned up", "rm -rf /tmp/lab-in-a-box-salt-mystate" in text)
check("no formula metadata is touched when it is not a formula", "/srv/formula_metadata" not in text)

# ── the same source again ───────────────────────────────────────────────────
runner = _Run(stamp="https://example.com/mystate.tar.gz {}".format(SHA))
with mock.patch.object(sc, "_run", runner):
    installed = sc.ensure_salt_state("smlm.lab", "mgrctl exec --", "mystate",
                                     url="https://example.com/mystate.tar.gz", sha256=SHA)
check("the same source again installs nothing", installed is False)
check("...and nothing is fetched or removed", "curl" not in runner.joined() and "rm -rf" not in runner.joined())

# ── a changed source ────────────────────────────────────────────────────────
runner = _Run(stamp="https://example.com/OLD.tar.gz {}".format(SHA))
with mock.patch.object(sc, "_run", runner):
    installed = sc.ensure_salt_state("smlm.lab", "mgrctl exec --", "mystate",
                                     url="https://example.com/mystate.tar.gz", sha256=SHA)
check("a changed source is installed over the old one", installed is True and "curl" in runner.joined())

# An unstamped directory (installed by hand, or by an older version) is replaced rather than trusted.
runner = _Run(stamp="")
with mock.patch.object(sc, "_run", runner):
    check("a directory with no stamp is replaced",
          sc.ensure_salt_state("smlm.lab", "mgrctl exec --", "mystate", url="https://x/y.tgz") is True)

# A tarball URL often ends in neither .tar.gz nor .sls — a codeload or GitLab archive link, for one — so the
# source's kind cannot be read off the URL. Confirmed live on 2026-10-15: deciding by extension wrote the gzip
# bytes into init.sls.
runner = _Run(is_tar=True)
with mock.patch.object(sc, "_run", runner):
    sc.ensure_salt_state("smlm.lab", "mgrctl exec --", "fromcodeload",
                         url="https://codeload.github.com/o/r/tar.gz/refs/heads/master")
check("a tarball whose URL has no archive extension is still unpacked",
      "tar -xf" in runner.joined() and "init.sls" not in runner.joined())

# ── a single .sls ───────────────────────────────────────────────────────────
runner = _Run(is_tar=False)
with mock.patch.object(sc, "_run", runner):
    sc.ensure_salt_state("smlm.lab", "mgrctl exec --", "onefile", url="https://example.com/thing.sls")
check("a single .sls becomes the state's init.sls",
      "cp /tmp/lab-in-a-box-salt-onefile/source /srv/salt/onefile/init.sls" in runner.joined())
check("a single .sls is not unpacked", "tar -xf" not in runner.joined())

# ── a formula tarball ───────────────────────────────────────────────────────
runner = _Run()
with mock.patch.object(sc, "_run", runner):
    sc.ensure_salt_state("smlm.lab", "mgrctl exec --", "myformula",
                         url="https://example.com/myformula.tgz", formula=True)
check("a formula's form.yml is moved to /srv/formula_metadata/<name>",
      "/srv/formula_metadata/myformula/form.yml" in runner.joined())

# ── an RPM ──────────────────────────────────────────────────────────────────
runner = _Run()
with mock.patch.object(sc, "_run", runner):
    sc.ensure_salt_state("smlm.lab", "mgrctl exec --", "rpmstate",
                         rpm="https://example.com/salt-formula.rpm", formula=True)
text = runner.joined()
check("the RPM is unpacked, never installed", "rpm2cpio source | cpio -idm" in text and " rpm -i" not in text
      and "zypper" not in text)
check("its states go to /srv/salt/<name>",
      "cp -a /tmp/lab-in-a-box-salt-rpmstate/usr/share/salt-formulas/states/rpmstate/. /srv/salt/rpmstate/" in text)
check("its metadata goes to /srv/formula_metadata/<name>",
      "cp -a /tmp/lab-in-a-box-salt-rpmstate/usr/share/salt-formulas/metadata/rpmstate/. "
      "/srv/formula_metadata/rpmstate/" in text)
check("an RPM that ships no such state is an error, not a silent empty install",
      "ships no salt-formulas/states/rpmstate" in text)

# ── a run that fails leaves nothing behind ─────────────────────────────────
# Confirmed live on 2026-10-15: a wrong checksum used to leave its work directory inside the server container.
runner = _Run(fails="sha256sum")
try:
    with mock.patch.object(sc, "_run", runner):
        sc.ensure_salt_state("smlm.lab", "mgrctl exec --", "mystate", url="https://x/y.tgz", sha256=SHA)
    check("a wrong checksum stops the install", False)
except RuntimeError:
    pass
check("the work directory is removed even when the run fails",
      "rm -rf /tmp/lab-in-a-box-salt-mystate" in runner.cmds[-1])
check("nothing was installed over the existing state",
      not any("cp -a" in c or "tar -xf" in c for c in runner.cmds))

# ── the orchestrator ────────────────────────────────────────────────────────
calls = []
with mock.patch.object(sc, "ensure_salt_state", side_effect=lambda h, e, name, **kw: calls.append((name, kw))):
    sc.ensure_salt_states("smlm.lab", "mgrctl exec --", {"smlm_salt_states": [
        {"name": "a", "url": "https://x/a.tgz"},
        {"name": "b", "rpm": "https://x/b.rpm", "formula": True},
        {"name": "c", "url": "https://x/c.tgz", "formula": "true"},
    ]}, "smlm")
check("every entry is installed, in order", [c[0] for c in calls] == ["a", "b", "c"])
check("an rpm entry is passed as an rpm", calls[1][1]["rpm"] == "https://x/b.rpm" and calls[1][1]["formula"] is True)
check("formula accepts true and \"true\"", calls[2][1]["formula"] is True and calls[0][1]["formula"] is False)
with mock.patch.object(sc, "ensure_salt_state") as never:
    try:
        sc.ensure_salt_states("smlm.lab", "mgrctl exec --", {"smlm_salt_states": [{"url": "https://x/a.tgz"}]}, "smlm")
        check("an entry with no name is refused", False)
    except SystemExit:
        check("an entry with no name is refused before anything runs", not never.called)
calls = []
with mock.patch.object(sc, "ensure_salt_state", side_effect=lambda h, e, name, **kw: calls.append(name)):
    sc.ensure_salt_states("smlm.lab", "mgrctl exec --", {}, "smlm")
check("a lab with no salt states runs nothing", calls == [])

# ── both add-ons expose it ──────────────────────────────────────────────────
record = json.loads((_REPO / "schemas" / "compatibility.json").read_text())["schemas"]
for addon, prefix in (("smlm", "smlm"), ("uyuni", "uyuni")):
    schema = json.loads(subprocess.run([sys.executable, str(_REPO / "scripts" / ("install_" + addon)),
                                        "--schema", "json"], stdout=subprocess.PIPE,
                                       universal_newlines=True).stdout)
    names = [f["name"] for f in schema["fields"]]
    check("{}: the field is in its schema".format(addon), "{}_salt_states".format(prefix) in names)
    # The exact version is not pinned here: any later change to the add-on raises it again. What must hold is that
    # the schema change was RECORDED, which is what schemas/compatibility.json is for.
    check("{}: the schema change is recorded".format(addon),
          any("{}_salt_states".format(prefix) in e.get("changes", "")
              for e in record.get("addon:" + addon, [])))
    source = (_REPO / "scripts" / ("install_" + addon)).read_text()
    check("{}: the provisioning step runs it".format(addon),
          'rps("salt states", sc.ensure_salt_states, hostname, exec_prefix, cfg, "{}")'.format(prefix) in source)

    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
        json.dump({"common": {}, "nodes": {}, prefix: {"{}_salt_states".format(prefix): [
            {"name": "good", "url": "https://example.com/g.tgz", "sha256": SHA},
            {"name": "nope", "url": "https://x/y.tgz", "rpm": "https://x/y.rpm"},
            {"name": "local", "url": "/not/a/url.tgz"},
            {"name": "badsum", "url": "https://x/y.tgz", "sha256": "xyz"},
        ]}}, fh)
    out = subprocess.run([sys.executable, str(_REPO / "scripts" / ("install_" + addon)), "--validate", fh.name],
                         stdout=subprocess.PIPE, universal_newlines=True).stdout
    lines = [ln for ln in out.splitlines() if "_salt_states" in ln]
    check("{}: a good entry is not flagged".format(addon), not any("[good]" in ln for ln in lines))
    check("{}: url and rpm together is flagged".format(addon), any("[nope]" in ln for ln in lines))
    check("{}: a non-URL source is flagged".format(addon), any("[local]" in ln for ln in lines))
    check("{}: a malformed checksum is flagged".format(addon), any("[badsum]" in ln for ln in lines))
    Path(fh.name).unlink()

if failures:
    print("{} check(s) failed".format(len(failures)))
    sys.exit(1)
print("all salt-state checks passed")
