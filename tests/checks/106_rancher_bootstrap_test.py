#!/usr/bin/env python3.11
# install_rancher's finish_bootstrap() — see 106_rancher_bootstrap.sh.
import sys
from pathlib import Path
from unittest import mock

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "libs"))

import apps  # noqa: E402

rancher = apps.load_addon_module("install_rancher", str(_REPO / "scripts"))

failures = []


def check(desc, cond):
    if not cond:
        failures.append(desc)
        print("FAIL:", desc)


class _Rancher:
    """
    Stands in for a Rancher over SSH. `bootstrap_works` is a Rancher nobody has logged into yet; `admin_works` is
    one already bootstrapped; neither is one that cannot be reached. `server_url` is what the setting ends up as.
    """

    def __init__(self, bootstrap_works=True, admin_works=False, server_url_sticks=True):
        self.cmds = []
        self.bootstrap_works = bootstrap_works
        self.admin_works = admin_works
        self.server_url_sticks = server_url_sticks
        self.server_url = ""

    def output(self, hostname, cmd, **kwargs):
        self.cmds.append(cmd)
        if "action=login" in cmd:
            if "the-bootstrap-one" in cmd and self.bootstrap_works:
                return "token-abc:secret"
            if "the-admin-one" in cmd and self.admin_works:
                return "token-xyz:secret"
            return ""
        if "settings.management.cattle.io server-url" in cmd:
            return self.server_url if self.server_url_sticks else ""
        return ""

    def run(self, hostname, cmd, **kwargs):
        self.cmds.append(cmd)
        if "settings/server-url" in cmd and "PUT" in cmd:
            self.server_url = "https://rancher.lab.example"
        return mock.Mock(returncode=0, stdout="")

    def joined(self):
        return "\n".join(self.cmds)


def _finish(fake, bootstrap="the-bootstrap-one", admin="the-admin-one"):
    with mock.patch.object(rancher, "ssh_output", side_effect=fake.output), \
         mock.patch.object(rancher, "ssh_run", side_effect=fake.run):
        return rancher.finish_bootstrap("n1", "rancher.lab.example", bootstrap, admin)


# ── a Rancher nobody has logged into yet ──────────────────────────────────
fake = _Rancher()
returned = _finish(fake)
text = fake.joined()
check("it logs in with the password Rancher generated", "action=login" in text and "the-bootstrap-one" in text)
check("it changes the password to the one the lab asked for",
      "action=changepassword" in text and "the-admin-one" in text)
check("the password it reports is the lab's own", returned == "the-admin-one")
check("it sets the server URL to the hostname Rancher serves",
      "PUT https://rancher.lab.example/v3/settings/server-url" in text
      and "https://rancher.lab.example" in text)
check("the login comes before the password change, which comes before the URL",
      text.index("action=login") < text.index("action=changepassword") < text.index("settings/server-url"))
check("the URL is read back, rather than assumed to have stuck",
      "settings.management.cattle.io server-url" in text)
check("the password travels quoted, never bare in a shell command",
      "'{\"currentPassword\"" in text or '"currentPassword"' in text)

# No new password asked for: the generated one stays, and nothing is changed.
fake = _Rancher()
returned = _finish(fake, admin="the-bootstrap-one")
check("a lab that names no password keeps the generated one",
      returned == "the-bootstrap-one" and "action=changepassword" not in fake.joined())
check("...and its server URL is still set", "settings/server-url" in fake.joined())

# ── a Rancher that is already bootstrapped ───────────────────────────────
# Confirmed live on 2026-10-17 against a real Rancher: the generated password stops working once it has been
# changed, and that is not a failure. It is also what makes a second run of the add-on safe.
fake = _Rancher(bootstrap_works=False, admin_works=True)
returned = _finish(fake)
check("it falls back to the lab's own password", returned == "the-admin-one")
check("nothing is changed on a Rancher that is already bootstrapped",
      "action=changepassword" not in fake.joined())
check("its server URL is still asserted", "settings/server-url" in fake.joined())

# ── a Rancher that cannot be reached ─────────────────────────────────────
fake = _Rancher(bootstrap_works=False, admin_works=False)
warned = []
with mock.patch.object(rancher, "warn", side_effect=lambda m: warned.append(m)):
    returned = _finish(fake)
check("it warns instead of dying, since the lab's own VMs are already built", returned == "the-admin-one")
check("the warning says what the consequence is",
      warned and "cannot be imported" in warned[0])
check("nothing is written to a Rancher it could not log in to", "settings/server-url" not in fake.joined())

# A URL that does not stick is reported: silence there would leave an import failing for no visible reason.
fake = _Rancher(server_url_sticks=False)
warned = []
with mock.patch.object(rancher, "warn", side_effect=lambda m: warned.append(m)):
    _finish(fake)
check("a server URL that did not stick is reported",
      warned and "server-url" in warned[0] and "importing a cluster will not work" in warned[0])

# ── the install does it, and the field is documented ─────────────────────
source = (_REPO / "scripts" / "install_rancher").read_text()
check("the install calls it", "finish_bootstrap(hostname, hostname_fqdn, initial_rancher_pwd," in source)
check("it no longer just prints the generated password and stops",
      "# Admin password:" in source and "# Initial password:" not in source)
check("the new field is documented in the add-on's own header", "rancher_admin_pwd" in source)
check("the field is in its schema",
      "rancher_admin_pwd" in [f["name"] for f in __import__("json").loads(
          __import__("subprocess").run([sys.executable, str(_REPO / "scripts" / "install_rancher"),
                                        "--schema", "json"], stdout=-1,
                                       universal_newlines=True).stdout)["fields"]])

if failures:
    print("{} check(s) failed".format(len(failures)))
    sys.exit(1)
print("all rancher-bootstrap checks passed")
