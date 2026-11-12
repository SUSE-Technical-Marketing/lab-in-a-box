#!/usr/bin/env python3
# Unit tests for scripts/install_kubectl_apply — mocked ssh_run, no real SSH.
# Run from 116_kubectl_apply.sh, in its own container — see tests/run_tests.sh.
import sys
import tempfile
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "libs"))
sys.path.insert(0, str(_REPO / "scripts"))

import apps  # noqa: E402
import addon_common as ac  # noqa: E402
import k8s  # noqa: E402
import primary  # noqa: E402

ka = apps.load_addon_module("install_kubectl_apply", str(_REPO / "scripts"))  # no .py: loaded by path

failures = []


def check(desc, cond):
    if not cond:
        failures.append(desc)
        print("FAIL:", desc)


class FakeResult:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


calls = []
fake_result = FakeResult(returncode=0)


def _fake_ssh_run(hostname, cmd, check=True, input_text=None, capture=False, user="root"):
    calls.append((hostname, cmd, input_text))
    return fake_result


ka.ssh_run = _fake_ssh_run
died = []
ka.die = lambda m: died.append(m) or (_ for _ in ()).throw(SystemExit)


# ── _classify ─────────────────────────────────────────────────────────────
check("_classify: a multi-line string is inline content", ka._classify("a: 1\nb: 2") == "content")
check("_classify: an nfs:// URL is nfs", ka._classify("nfs://nfs1.mydemo.lab/exports/m.yaml") == "nfs")
check("_classify: an http(s) URL is a url", ka._classify("https://example.com/m.yaml") == "url")
check("_classify: anything else is treated as a local path", ka._classify("/some/file.yaml") == "path")


# ── apply_manifest: nfs ──────────────────────────────────────────────────────
calls.clear()
nfs_calls = []
ac.fetch_nfs_content = lambda hostname, url: nfs_calls.append((hostname, url)) or "kind: Secret\n"
ka.apply_manifest("sol.mydemo.lab", "nfs://nfs1.mydemo.lab/exports/m.yaml")
check("apply_manifest(nfs): fetches via addon_common.fetch_nfs_content",
      nfs_calls == [("sol.mydemo.lab", "nfs://nfs1.mydemo.lab/exports/m.yaml")])
check("apply_manifest(nfs): the fetched content is piped via stdin",
      "kubectl apply -f -" in calls[0][1] and calls[0][2] == "kind: Secret\n")


# ── apply_manifest: url ──────────────────────────────────────────────────────
calls.clear()
ka.apply_manifest("sol.mydemo.lab", "https://example.com/m.yaml")
check("apply_manifest(url): kubectl apply -f <url>, no stdin piped",
      calls[0][0] == "sol.mydemo.lab" and "kubectl apply -f https://example.com/m.yaml" in calls[0][1]
      and calls[0][2] is None)

calls.clear()
ka.apply_manifest("sol.mydemo.lab", "https://example.com/m.yaml", namespace="apps", prune=True, prune_label="app=x")
check("apply_manifest(url): namespace and --prune are added when requested",
      "-n apps" in calls[0][1] and "--prune -l app=x" in calls[0][1])


# ── apply_manifest: inline content ──────────────────────────────────────────
calls.clear()
ka.apply_manifest("sol.mydemo.lab", "apiVersion: v1\nkind: ConfigMap\n")
check("apply_manifest(content): piped via stdin to kubectl apply -f -",
      "kubectl apply -f -" in calls[0][1] and calls[0][2] == "apiVersion: v1\nkind: ConfigMap\n")


# ── apply_manifest: local path ──────────────────────────────────────────────
with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
    f.write("kind: Secret\n")
    tmp_path = f.name

calls.clear()
ka.apply_manifest("sol.mydemo.lab", tmp_path)
check("apply_manifest(path): reads the real local file content and pipes it in",
      calls[0][2] == "kind: Secret\n")
Path(tmp_path).unlink()

died.clear()
try:
    ka.apply_manifest("sol.mydemo.lab", "/no/such/file.yaml")
except SystemExit:
    pass
check("apply_manifest(path): a nonexistent local file dies with a clear message",
      len(died) == 1 and "/no/such/file.yaml" in died[0])


# A failing kubectl apply dies too.
died.clear()
fake_result = FakeResult(returncode=1)
try:
    ka.apply_manifest("sol.mydemo.lab", "https://example.com/m.yaml")
except SystemExit:
    pass
check("apply_manifest: a non-zero kubectl exit dies", len(died) == 1)
fake_result = FakeResult(returncode=0)


# ── _validate ────────────────────────────────────────────────────────────────
class FakeValidator:
    def __init__(self, definition):
        self.definition = definition
        self.errors = []

    def vbool(self, section, field):
        v = (self.definition.get(section, {}) or {}).get(field)
        if v and v not in ("true", "false"):
            self.errors.append("[ERROR] bad bool")


v = FakeValidator({"kubectl_apply": {}})
ka._validate(v)
check("_validate: missing kubectl_apply_manifests is an error",
      any("kubectl_apply_manifests" in e for e in v.errors))

v = FakeValidator({"kubectl_apply": {"kubectl_apply_manifests": ["a: 1"]}})
ka._validate(v)
check("_validate: a valid manifests list has no error", v.errors == [])

v = FakeValidator({"kubectl_apply": {"kubectl_apply_manifests": ["a: 1"], "kubectl_apply_prune": "true"}})
ka._validate(v)
check("_validate: prune='true' without prune_label is an error",
      any("kubectl_apply_prune_label" in e for e in v.errors))

v = FakeValidator({"kubectl_apply": {"kubectl_apply_manifests": ["a: 1"], "kubectl_apply_prune": "true",
                                      "kubectl_apply_prune_label": "app=x"}})
ka._validate(v)
check("_validate: prune='true' with a prune_label has no error", v.errors == [])


# ── main(): targets first_server_node(), applies every manifest in order ───
definition = primary.LabDefinition({
    "nodes": {"sol.mydemo.lab": {"INSTALL_RKE2_TYPE": "server"}},
    "kubectl_apply": {"kubectl_apply_manifests": ["a: 1", "b: 2"]},
}, source_path="/tmp/fake.json", fmt="json")

apply_calls = []
ka.apply_manifest = lambda hostname, manifest, **kw: apply_calls.append((hostname, manifest))
k8s.first_server_node = lambda d: ("sol.mydemo.lab", "ssh ...")
sys.argv = ["install_kubectl_apply", "/tmp/fake.json"]
primary.load_definition = lambda path: definition
ka.main()
check("main(): applies every manifest, in order, against the server node",
      apply_calls == [("sol.mydemo.lab", "a: 1"), ("sol.mydemo.lab", "b: 2")])


if failures:
    print("{} failure(s)".format(len(failures)))
    sys.exit(1)
print("All install_kubectl_apply checks passed")
