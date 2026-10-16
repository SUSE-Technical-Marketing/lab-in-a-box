#!/usr/bin/env python3
# Unit tests for webui/lib/discovery.py's schema()/discover() attaching
# addon PLUGIN capabilities, against the real repo checkout's addon scripts
# (no fixtures needed — scripts/install_*.py are real
# files). Run from 23_discovery_capabilities.sh, in its own container — see
# tests/run_tests.sh.
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "libs"))
sys.path.insert(0, str(_REPO / "webui" / "lib"))

import discovery  # noqa: E402

failures = []


def check(desc, cond):
    if not cond:
        failures.append(desc)
        print("FAIL:", desc)


# ── _def_path(): finds a ported addon across BOTH addon_dirs(), not just
#    scripts_dir() alone (the bug this fix closes — install_mariadb only
#    lives in scripts/, not scripts/) ────────────────────────
try:
    path = discovery._def_path("install_mariadb")
    check("_def_path finds install_mariadb in the scripts/ addon dir",
          path.endswith("install_mariadb") and Path(path).is_file())
except FileNotFoundError:
    check("_def_path finds install_mariadb in the scripts/ addon dir", False)

try:
    path = discovery._def_path("install_ds389")
    check("_def_path finds install_ds389 (a Python addon script)",
          path.endswith("install_ds389") and Path(path).is_file())
except FileNotFoundError:
    check("_def_path finds install_ds389 (a Python addon script)", False)


# ── schema(): capabilities for a kubernetes-layer addon ──────────────────────
# install_mariadb supports the kubernetes layer and the OS-native layer, and it is the example for a kubernetes addon.
sc = discovery.schema("install_mariadb")
check("schema('install_mariadb') has 'kubernetes' among its capabilities.layers",
      "kubernetes" in sc.get("capabilities", {}).get("layers", []))
check("schema('install_mariadb') keeps its own schema fields (section) too",
      sc.get("section") == "mariadb")

# ── schema(): capabilities for a standalone-container addon ──────────────────
# install_grafana runs Grafana as a podman container on the host, so it is a container addon, not an OS-native package.
sc = discovery.schema("install_grafana")
check("schema('install_grafana') has a capabilities.layers of ['standalone-container']",
      sc.get("capabilities", {}).get("layers") == ["standalone-container"])

# ── discover(): every listed item carries a layers list ──────────────────────
items = discovery.discover()
check("discover() finds a reasonable number of addons (>= 30)", len(items) >= 30)
missing = [it["name"] for it in items if "layers" not in it]
check("every discover() item has a 'layers' key: {}".format(missing), missing == [])
mariadb_item = next((it for it in items if it["name"] == "install_mariadb"), None)
check("discover()'s install_mariadb entry has 'kubernetes' among its layers",
      mariadb_item is not None and "kubernetes" in mariadb_item["layers"])

# ── discover(): every listed item carries its PLUGIN targets ─────────────────
missing = [it["name"] for it in items if not it.get("targets")]
check("every discover() item has a non-empty 'targets' list: {}".format(missing), missing == [])
check("discover()'s install_mariadb entry targets container, vm and baremetal",
      mariadb_item is not None and mariadb_item["targets"] == ["container", "vm", "baremetal"])
grafana_item = next((it for it in items if it["name"] == "install_grafana"), None)
check("discover()'s install_grafana entry targets vm and baremetal only",
      grafana_item is not None and grafana_item["targets"] == ["vm", "baremetal"])


# ── validate_lab(): the real preflight, not a structural-only check ──────────
# The web UI must reject what a deploy would reject. These are the values the structural check used to wave through.
import os  # noqa: E402
import tempfile  # noqa: E402
from unittest import mock  # noqa: E402

_outdir = tempfile.mkdtemp()
os.environ["LABBUILDER_OUTPUT_DIR"] = _outdir
_good_node = {"myip": "192.168.1.50", "mymac": "aa:bb:cc:dd:ee:01", "config_method": "virt_customize"}
_common = {"SOURCE_IMAGE": "img.qcow2", "VM_MEM": "4096", "VM_DSK": "40", "VM_CPU": "2",
           "mymask": "24", "mygw": "192.168.1.1", "mydns": "192.168.1.1", "mydomain": "test.lab"}

r = discovery.validate_lab({"common": dict(_common), "nodes": {"vm1": dict(_good_node)}})
check("validate_lab: a valid lab passes", r["ok"] is True)
check("validate_lab: the report is returned without colour codes", "\x1b[" not in r["output"])

# A node with no myip is not a broken lab any more: the run allocates one from the network the definition already
# describes, and the preflight names the address it will be (libs/ipalloc.py).
r = discovery.validate_lab({"common": dict(_common), "nodes": {"vm1": {"config_method": "virt_customize"}}})
check("validate_lab: a node with no myip is told which address it will be given",
      r["ok"] is True and "will be allocated from" in r["output"])

# Without a gateway and a mask there is nothing to allocate from, and the field is still required.
r = discovery.validate_lab({"common": {k: v for k, v in _common.items() if k not in ("mygw", "mymask")},
                            "nodes": {"vm1": {"config_method": "virt_customize"}}})
check("validate_lab: a node with no myip and no network to take one from still fails",
      r["ok"] is False and "myip" in r["output"])

r = discovery.validate_lab({"common": dict(_common),
                            "nodes": {"vm1": dict(_good_node), "vm2": dict(_good_node, mymac="aa:bb:cc:dd:ee:02")}})
check("validate_lab: two nodes sharing one IP fail", r["ok"] is False and "already assigned" in r["output"])

r = discovery.validate_lab({"common": dict(_common), "nodes": {"vm1": dict(_good_node, backend="nosuch")}})
check("validate_lab: an invalid backend fails", r["ok"] is False and "nosuch" in r["output"])

r = discovery.validate_lab({"common": dict(_common),
                            "nodes": {"vm1": dict(_good_node, addons=["nosuchaddon"])}})
check("validate_lab: an add-on with no install script fails",
      r["ok"] is False and "nosuchaddon" in r["output"])

r = discovery.validate_lab({"common": dict(_common),
                            "nodes": {"vm1": dict(_good_node, kcluster="nope")}, "kclusters": {}})
check("validate_lab: a node referencing an undefined kcluster still fails",
      r["ok"] is False and "nope" in r["output"])

# host_checks=False: nothing may reach a hypervisor or the network from the web server's user.
_lc = discovery._lab_creation_lib()
with mock.patch.object(_lc.subprocess, "run") as _run, \
        mock.patch.object(_lc, "run_libvirt_tool") as _virsh:
    discovery.validate_lab({"common": dict(_common), "nodes": {"vm1": dict(_good_node)}})
_cmds = [c.args[0] for c in _run.call_args_list if c.args and isinstance(c.args[0], list)]
check("validate_lab: no ping and no virsh call", not _virsh.called
      and not any(c and c[0] in ("ping", "virsh", "ssh") for c in _cmds))
check("validate_lab: the add-on --validate step still runs through the lab file it wrote",
      all(os.path.basename(c[-1]).startswith("lab-builder-validate-") for c in _cmds if "--validate" in c))
check("validate_lab: the temporary lab file is removed",
      [f for f in os.listdir(_outdir) if f.startswith("lab-builder-validate-")] == [])

# ── credential_types(): what a credential consists of, without a login ──────
ct = discovery.credential_types()
check("credential_types: both kinds, keyed as the UI's kind values", sorted(ct) == ["cloud", "service"])
check("credential_types: aws is a cloud provider with AWS_REGION required",
      any(f["name"] == "AWS_REGION" and f["required"] for f in ct["cloud"].get("aws", [])))
check("credential_types: scc is a service credential whose password is secret",
      any(f["name"] == "scc_password" and f["secret"] for f in ct["service"].get("scc", [])))
check("credential_types: every field carries name, required and secret only",
      all(sorted(f) == ["name", "required", "secret"]
          for kind in ct.values() for fields in kind.values() for f in fields))

import api as webui_api  # noqa: E402

st, body = webui_api.dispatch("credential-types", "GET", {}, b"", user=None, https=False)
check("api: credential-types answers without a user and without HTTPS", st == 200 and sorted(body) == ["cloud", "service"])

if failures:
    print("{} check(s) failed".format(len(failures)))
    sys.exit(1)
print("all discovery_capabilities checks passed")
