#!/usr/bin/env python3
# Unit tests for libs/provisioning.py and its callers (setup_helm, the libvirt backend's install_iso arguments, the
# install_helm.sh that automation-node-lib.sh writes). Run from 70_provisioning_https.sh.
import re
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "libs"))

import lab_creation  # noqa: E402
import primary  # noqa: E402
import provisioning as prov  # noqa: E402

failures = []


def check(desc, cond):
    if not cond:
        failures.append(desc)
        print("FAIL:", desc)


# ── base_url / tls_verify ─────────────────────────────────────────────────────
check("base_url defaults to https://<host>", prov.base_url("192.168.88.250") == "https://192.168.88.250")
check("base_url uses the override, without a trailing slash",
      prov.base_url("192.168.88.250", "http://10.0.0.5/") == "http://10.0.0.5")
check("tls_verify is off by default", not prov.tls_verify("") and not prov.tls_verify(None) and not prov.tls_verify("0"))
check("tls_verify accepts 1/true/yes/on", all(prov.tls_verify(v) for v in ("1", "true", "YES", " on ")))

# ── installer_args ────────────────────────────────────────────────────────────
https = "https://192.168.88.250/lab_creation/install_iso/vm1.x"
expected = {
    "autoyast": ("autoyast=" + https, "ssl.certs=0"),
    "kickstart": ("inst.ks=" + https, "inst.noverifyssl"),
    "preseed": ("url=" + https, "debian-installer/allow_unauthenticated_ssl=true"),
}
for itype, (url_arg, no_verify) in expected.items():
    args = prov.installer_args(itype, https, verify=False).split()
    check("{}: the answer file URL is passed".format(itype), url_arg in args)
    check("{}: HTTPS without verification adds {}".format(itype, no_verify), no_verify in args)
    check("{}: HTTPS with verification adds no insecure flag".format(itype),
          no_verify not in prov.installer_args(itype, https, verify=True).split())
    check("{}: plain HTTP adds no insecure flag".format(itype),
          no_verify not in prov.installer_args(itype, "http://h/f", verify=False).split())
check("kickstart keeps inst.sshd, inst.text and TERM=vt100",
      {"inst.sshd", "inst.text", "TERM=vt100"} <= set(prov.installer_args("kickstart", https, False).split()))
check("only kickstart gets TERM=vt100 (the Debian installer has no vt100 terminfo entry)",
      all("TERM=" not in prov.installer_args(t, https, False) for t in ("autoyast", "preseed")))
check("preseed keeps auto=true priority=critical",
      {"auto=true", "priority=critical"} <= set(prov.installer_args("preseed", https, False).split()))

# ── curl_tls_option ───────────────────────────────────────────────────────────
check("curl gets -k for HTTPS without verification", prov.curl_tls_option("https://h", False) == "-k")
check("curl gets no option with verification or over HTTP",
      prov.curl_tls_option("https://h", True) == "" and prov.curl_tls_option("http://h", False) == "")

# ── setup_helm ────────────────────────────────────────────────────────────────
commands = []
lab_creation.ssh_run = lambda host, cmd, **kw: commands.append((host, cmd))
lab_creation.log = lambda *a, **k: None
primary.load_config = lambda paths=None: {}
lab_creation.setup_helm("node1", "c1", automation_host="automation")
check("setup_helm fetches install_helm.sh over HTTPS with -k by default",
      commands[-1][1].startswith("curl -k https://automation/helm/install_helm.sh | "))
check("setup_helm passes the base URL and curl option to install_helm.sh",
      "LAB_PROVISIONING_URL=https://automation LAB_CURL_TLS=-k bash -" in commands[-1][1])
primary.load_config = lambda paths=None: {"PROVISIONING_BASE_URL": "http://10.0.0.5"}
lab_creation.setup_helm("node1", "c1", automation_host="automation")
check("setup_helm honours PROVISIONING_BASE_URL and drops -k over HTTP",
      commands[-1][1].startswith("curl http://10.0.0.5/helm/install_helm.sh | ")
      and "LAB_CURL_TLS= bash -" in commands[-1][1])
primary.load_config = lambda paths=None: {"PROVISIONING_TLS_VERIFY": "1"}
lab_creation.setup_helm("node1", "c1", automation_host="automation")
check("setup_helm drops -k when PROVISIONING_TLS_VERIFY=1", " -k " not in commands[-1][1])

# ── install_helm.sh written by automation-node-lib.sh ────────────────────────
script = (_REPO / "setup_demo_server" / "automation-node-lib.sh").read_text()
bodies = re.findall(r"install_helm\.sh\"? <<\s?HELMSCRIPT\n(.*?)\nHELMSCRIPT", script, re.S)
check("automation-node-lib.sh writes install_helm.sh in one place (write_helm_scripts)", len(bodies) == 1)
for body in bodies:
    check("install_helm.sh downloads from LAB_PROVISIONING_URL with LAB_CURL_TLS, falling back to plain HTTP",
          '\\${LAB_CURL_TLS-} "\\${LAB_PROVISIONING_URL:-http://${AUTOMATION_HOSTNAME}}/helm/' in body)

if failures:
    print("{} check(s) failed".format(len(failures)))
    sys.exit(1)
print("all provisioning unit checks passed")
