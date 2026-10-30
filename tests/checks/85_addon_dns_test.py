"""Add-ons that expose an ingress hostname register its DNS record; register_service_dns(); service-record cleanup."""
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "libs"))

failures = []


def check(name, cond):
    print("{}: {}".format("PASS" if cond else "FAIL", name))
    if not cond:
        failures.append(name)


# An ingress host set on the command line, a `kubectl create ingress --rule`, a chart domain, a manifest `- host:` in the
# add-on's templates, or a static web game (which always carries an ingress host).
_EXPOSES = re.compile(r"--set [\w.\-\[\]]*(host|hostname|hosts\.core|hosts\.domain)=|--rule=|render_static_webgame\(")
_REGISTERS = re.compile(r"\b(register_service_dns|add_service_dns|add_dns_to_named_rr)\(")
# nextcloud: nextcloud.host is the trusted domain (the node's own FQDN by default); the chart's ingress stays disabled, so
# there is no hostname to register.
_EXEMPT = {"nextcloud"}

for script in sorted((ROOT / "scripts").glob("install_*.py")):
    name = script.stem[len("install_"):]
    if name in _EXEMPT:
        continue
    src = script.read_text()
    tmpl_dir = ROOT / "templates" / "addons" / name
    in_templates = tmpl_dir.is_dir() and any(re.search(r"^\s*- host:", f.read_text(errors="replace"), re.M)
                                             for f in tmpl_dir.iterdir() if f.is_file())
    if _EXPOSES.search(src) or in_templates:
        check("{}: registers the DNS record of the hostname it exposes".format(script.name), bool(_REGISTERS.search(src)))

# ── register_service_dns() ──────────────────────────────────────────────────────────────────────────────────────────

import addon_common as ac  # noqa: E402
import lab_creation  # noqa: E402
import primary  # noqa: E402

calls, warnings = [], []
lab_creation.add_service_dns = lambda *a, **kw: calls.append((a, kw))
lab_creation.warn = warnings.append
primary.load_config = lambda paths=None: {"REMOTE_DNS_SERVERS": "dns2 dns3"}
definition = {"kclusters": {"c1": {"clu_type": "k3s", "mydomain": "mydemo.lab"}}}

ac.register_service_dns(definition, "c1", "ai.c1.mydemo.lab")
check("register_service_dns: one add_service_dns call", len(calls) == 1)
args, kwargs = calls[0]
check("register_service_dns: entry relative to mydomain, kcluster type and domain passed",
      args[1:] == ("c1", "k3s", "ai.c1", "mydemo.lab"))
check("register_service_dns: REMOTE_DNS_SERVERS passed as a list", kwargs.get("remote_dns_servers") == ["dns2", "dns3"])

calls.clear()
ac.register_service_dns(definition, "c1", "ai.example.com")
check("register_service_dns: a name outside mydomain is not registered", calls == [])
check("register_service_dns: and a warning names it", len(warnings) == 1 and "ai.example.com" in warnings[0])

# ── del_from_dns(): service records pointing at the node's IP ────────────────────────────────────────────────────────

import services  # noqa: E402

with tempfile.TemporaryDirectory() as tmp:
    services.NAMED_ZONE_DIR = Path(tmp)
    zone = Path(tmp) / "mydemo.lab.lan"
    zone.write_text("\n".join([
        "node1         IN  A       192.168.88.23",
        "ai.c1\tIN A  192.168.88.23",
        "ai.c1\tIN A  192.168.88.231",
        "rancher.c1\tIN A  192.168.88.23",
    ]) + "\n")
    dns = services.DNSService()
    dns.restart_named = lambda remote_servers=None: None
    dns._update_hosts_file = lambda vm_name: None
    remote = []

    class _Ran(object):
        """What ssh_run() returns: del_from_dns() probes each master before writing to it."""
        returncode = 0
        stdout = ""

    def _remote(host, cmd, check=True, capture=False):
        remote.append((host, cmd))
        return _Ran()

    dns._remote = _remote
    dns.del_from_dns("node1.mydemo.lab", "192.168.88.23", "mydemo.lab", "", remote_dns_servers=["dns2"])
    left = zone.read_text().splitlines()
    check("del_from_dns: the node's A record and its service records are removed",
          left == ["ai.c1\tIN A  192.168.88.231"])
    check("del_from_dns: remote servers get an anchored sed for the node's service records",
          any("IN A  192\\.168\\.88\\.23$/d" in cmd for _, cmd in remote))

if failures:
    print("\n{} check(s) failed".format(len(failures)))
    sys.exit(1)
print("\nall addon DNS checks passed")
