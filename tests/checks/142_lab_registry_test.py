#!/usr/bin/env python3
# Unit tests for libs/lab_registry.py (which labs this automation node has deployed, and the log of what it did to
# them) and for scripts/lab_watch.py's judgement: which nodes a provider no longer has, which are merely stopped,
# and which get their address registered again. No provider is reachable from here, so every backend is a double.
# Run from 142_lab_registry.sh, in its own container — see tests/run_tests.sh.
import sys
import tempfile
from pathlib import Path
from unittest import mock

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "libs"))
sys.path.insert(0, str(_REPO / "scripts"))

import lab_registry  # noqa: E402

failures = []


def check(desc, cond):
    if not cond:
        failures.append(desc)
        print("FAIL:", desc)


def lab(nodes, domain="mydemo.lab"):
    return {"common": {"mydomain": domain}, "nodes": {name: {} for name in nodes}}


# ── lab_name(): what a lab is recorded under ────────────────────────────────
check("a lab is recorded under its definition's base name, which is what the operator already calls it",
      lab_registry.lab_name("/root/solar-system-lab.json") == "solar-system-lab")
check("...whatever the extension", lab_registry.lab_name("labs/aws.yaml") == "aws")
check("...and a bare name is already one", lab_registry.lab_name("mylab") == "mylab")


# ── the domain: stated in common, or by a kcluster ──────────────────────────
check("the zone comes from common.mydomain", lab_registry._domain(lab(["a"])) == "mydemo.lab")
check("...or from the first kcluster that names one, since a lab may state it only there",
      lab_registry._domain({"kclusters": {"c1": {"mydomain": "mydemoaws.lab"}}}) == "mydemoaws.lab")
check("...or from the old single-cluster section",
      lab_registry._domain({"cluster": {"mydomain": "old.lab"}}) == "old.lab")
check("a lab with no domain anywhere reads as \"\" rather than failing", lab_registry._domain({}) == "")


# ── record / entry / entries / forget ───────────────────────────────────────
with tempfile.TemporaryDirectory() as tmp:
    cfg = {"LAB_REGISTRY_DIR": str(Path(tmp) / "labs"), "LAB_EVENT_LOG": str(Path(tmp) / "labs.log")}
    definition_file = Path(tmp) / "solar.json"
    definition_file.write_text("{}")

    written = lab_registry.record(lab(["node101.mydemo.lab", "node102.mydemo.lab"]), str(definition_file), cfg)
    check("recording a lab writes its name, its nodes and its zone",
          written["lab"] == "solar" and written["nodes"] == ["node101.mydemo.lab", "node102.mydemo.lab"]
          and written["mydomain"] == "mydemo.lab")
    check("...and the definition's path, never a copy of its content, which would go stale the moment the "
          "operator edited the real file",
          written["definition"] == str(definition_file.resolve()) and "common" not in written)
    check("...with the schema version every other configuration file this project writes carries",
          written["schema_version"] == lab_registry.LAB_REGISTRY_SCHEMA_VERSION)
    check("one file per lab, so two parallel runs never rewrite each other's entry",
          (Path(cfg["LAB_REGISTRY_DIR"]) / "solar.json").is_file())
    check("the entry reads back", lab_registry.entry("solar", cfg) == written)
    check("a lab that was never recorded reads as {} rather than failing",
          lab_registry.entry("absent", cfg) == {})

    first_recorded = written["created"]
    again = lab_registry.record(lab(["node101.mydemo.lab"]), str(definition_file), cfg)
    check("a rerun over the same lab rewrites the nodes the definition now declares",
          again["nodes"] == ["node101.mydemo.lab"])
    check("...and keeps the moment the lab was first recorded", again["created"] == first_recorded)

    lab_registry.record(lab(["n1.other.lab"], domain="other.lab"), str(Path(tmp) / "other.json"), cfg)
    listed = [e["lab"] for e in lab_registry.entries(cfg)]
    check("every lab is listed, most recently updated first", sorted(listed) == ["other", "solar"])

    check("forgetting a lab removes its entry", lab_registry.forget(str(definition_file), cfg)
          and lab_registry.entry("solar", cfg) == {})
    check("forgetting a lab that was never recorded is not an error: every lab built before this registry "
          "existed is in that position", lab_registry.forget("/root/never-built.json", cfg) is False)

    # ── the event log ───────────────────────────────────────────────────────
    log_lines = Path(cfg["LAB_EVENT_LOG"]).read_text().splitlines()
    check("every action is logged, one line each", len(log_lines) == 4)
    check("the first line says a lab was created, names it, and carries its nodes' FQDNs",
          log_lines[0].split()[1:3] == ["created", "solar"]
          and "node101.mydemo.lab" in log_lines[0] and "node102.mydemo.lab" in log_lines[0])
    check("a rerun is logged as an update, not a second creation",
          log_lines[1].split()[1] == "updated")
    check("the teardown is logged as destroyed, with the nodes that were on it",
          log_lines[3].split()[1:3] == ["destroyed", "solar"] and "node101.mydemo.lab" in log_lines[3])
    check("each line starts with a UTC timestamp, so the log sorts as text",
          all(line.split()[0].endswith("Z") and line.split()[0][4] == "-" for line in log_lines))
    # ── drop_nodes(): a node the provider no longer has ────────────────────
    lab_registry.record(lab(["a.mydemo.lab", "b.mydemo.lab"]), str(definition_file), cfg)
    left = lab_registry.drop_nodes("solar", ["a.mydemo.lab"], cfg, action="vanished")
    check("a node that no longer exists comes off the entry, leaving the rest",
          left["nodes"] == ["b.mydemo.lab"])
    check("...and is logged under the action it was dropped for, which is the only record that it was ever there",
          any(line.split()[1:3] == ["vanished", "solar"] and "a.mydemo.lab" in line
              for line in Path(cfg["LAB_EVENT_LOG"]).read_text().splitlines()))
    check("a node that was not on the entry changes nothing",
          lab_registry.drop_nodes("solar", ["never.mydemo.lab"], cfg) == {})
    check("dropping the last node forgets the lab: an entry naming nothing would have a scheduled run load a "
          "definition every time to ask about no nodes at all",
          lab_registry.drop_nodes("solar", ["b.mydemo.lab"], cfg) == {}
          and lab_registry.entry("solar", cfg) == {})


def _raises(call):
    try:
        call()
    except ValueError:
        return True
    return False


check("an action nobody defined is refused rather than written",
      _raises(lambda: lab_registry.log_event("exploded", "solar")))
check("a log that cannot be written never fails the lab that was built: this is called from the middle of create "
      "and destroy paths",
      lab_registry.log_event("created", "solar", (), {"LAB_EVENT_LOG": "/proc/one/cannot/write/here.log"})
      is False)


# ── lab_watch.py: what the providers say, and what is done about it ─────────
class _Backend(object):
    """A provider that holds `states`: {vm name: state}, with a name absent meaning the VM does not exist."""

    def __init__(self, states, raises=()):
        self.states, self.raises = states, set(raises)

    def vm_exists(self, vm_name):
        if vm_name in self.raises:
            raise RuntimeError("the account's credentials expired")
        return vm_name in self.states

    def vm_state(self, vm_name):
        return self.states[vm_name]


sys.modules.setdefault("setup_vm", mock.MagicMock())
import lab_watch  # noqa: E402

STATES = {"up.mydemo.lab": "running", "off.mydemo.lab": "stopped"}
with mock.patch.object(lab_watch.backends, "get_backend", return_value=_Backend(STATES)):
    seen = lab_watch.node_states({}, {}, ["up.mydemo.lab", "off.mydemo.lab", "deleted.mydemo.lab"])
check("a node the provider still has reports the provider's own word for its state",
      seen["up.mydemo.lab"] == "running" and seen["off.mydemo.lab"] == "stopped")
check("a node the provider no longer has is gone, which is how a VM deleted outside this tooling is noticed",
      seen["deleted.mydemo.lab"] == "gone")

with mock.patch.object(lab_watch.backends, "get_backend",
                       return_value=_Backend(STATES, raises=["up.mydemo.lab"])), \
        mock.patch.object(lab_watch, "warn", side_effect=lambda m: None):
    guarded = lab_watch.node_states({}, {}, ["up.mydemo.lab"])
check("a provider that cannot be asked reports unknown, never gone: an expired credential or an API that is down "
      "must not be read as \"somebody deleted this lab\"", guarded["up.mydemo.lab"] == "unknown")


class _NotImplementedState(_Backend):
    def vm_state(self, vm_name):
        raise NotImplementedError


with mock.patch.object(lab_watch.backends, "get_backend", return_value=_NotImplementedState(STATES)):
    check("a backend that cannot report a power state says unknown rather than failing the run",
          lab_watch.node_states({}, {}, ["up.mydemo.lab"])["up.mydemo.lab"] == "unknown")


def watch(nodes, states, dry_run=False):
    """check_lab() over one entry, returning (ok, dropped nodes, reconciled nodes)."""
    dropped, reconciled = [], []
    with tempfile.TemporaryDirectory() as tmp:
        definition_file = Path(tmp) / "solar.json"
        definition_file.write_text("{}")
        entry = {"lab": "solar", "definition": str(definition_file), "nodes": nodes, "mydomain": "mydemo.lab"}
        with mock.patch.object(lab_watch.primary, "load_definition", return_value=lab(nodes)), \
                mock.patch.object(lab_watch.migrations, "ensure_current", lambda d, f: f), \
                mock.patch.object(lab_watch.backends, "get_backend", return_value=_Backend(states)), \
                mock.patch.object(lab_watch.lab_registry, "drop_nodes",
                                  side_effect=lambda l, n, c, action="removed": dropped.extend(n)), \
                mock.patch.object(lab_watch.reconcile, "reconcile",
                                  side_effect=lambda d, c, r, vm_names=None: reconciled.extend(vm_names or []) or {}), \
                mock.patch.object(lab_watch.reconcile, "drift", return_value={}), \
                mock.patch.object(lab_watch, "log", lambda m: None), \
                mock.patch.object(lab_watch, "warn", lambda m: None):
            ok = lab_watch.check_lab(entry, {}, dry_run=dry_run, quiet=True)
    return ok, dropped, reconciled


ok, dropped, reconciled = watch(["up.mydemo.lab", "off.mydemo.lab", "deleted.mydemo.lab"], STATES)
check("the node its provider no longer has comes off the registry", dropped == ["deleted.mydemo.lab"])
check("a stopped node is left stopped: starting it is vm_power.py's job and a deliberate one",
      "off.mydemo.lab" not in dropped and "off.mydemo.lab" not in reconciled)
check("only a node that is there and running has its address registered again: a stopped one would be written at "
      "the address it had before it stopped, and a deleted one has no address at all",
      reconciled == ["up.mydemo.lab"])
check("a check that did what it set out to do reports success", ok is True)

ok, dropped, reconciled = watch(["up.mydemo.lab", "deleted.mydemo.lab"], STATES, dry_run=True)
check("--dry-run drops nothing and registers nothing", dropped == [] and reconciled == [])
check("...and still reports success", ok is True)

with tempfile.TemporaryDirectory() as tmp:
    with mock.patch.object(lab_watch, "warn", lambda m: None):
        missing = lab_watch.check_lab({"lab": "solar", "definition": str(Path(tmp) / "gone.json"),
                                       "nodes": ["a.mydemo.lab"]}, {})
check("a lab whose definition is gone is reported as a failure rather than quietly skipped: its nodes cannot be "
      "checked at all", missing is False)

# A node the registry holds that the definition no longer declares is the operator's own edit, not drift.
_, dropped, reconciled = watch([], {})
check("an entry with no nodes left asks no provider anything", dropped == [] and reconciled == [])


# ── the wiring: the registry is written and removed by the commands ────────
setup_lab_source = (_REPO / "scripts" / "setup_lab.py").read_text()
destroy_lab_source = (_REPO / "scripts" / "destroy_lab.py").read_text()
check("setup_lab.py records the lab before it builds anything: a run that fails halfway has still created VMs, "
      "and a lab nothing knows about is one no scheduled check walks",
      "lab_registry.record(definition, lab_file, config)" in setup_lab_source
      and setup_lab_source.index("lab_registry.record(")
      < setup_lab_source.index("setup_lab(definition, config, defaults, lab_file, keep=keep"))
check("destroy_lab.py takes it off again once its nodes are gone",
      "lab_registry.forget(lab_file, config)" in destroy_lab_source)
check("the scheduled check is installed, like every other command an operator or a timer runs",
      "lab_watch.py" in (_REPO / "install_automation_node_scripts.sh").read_text())
check("...and its timer is enabled, because the drift it repairs happens whether or not anybody is watching",
      "systemctl enable --now lab-watch.timer" in (_REPO / "install_automation_node_scripts.sh").read_text())

if failures:
    print("{} check(s) failed".format(len(failures)))
    sys.exit(1)
print("all lab-registry checks passed")
