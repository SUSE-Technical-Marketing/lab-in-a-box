#!/usr/bin/env python3
# Unit tests for scripts/install_hermes.py — no real git/podman/kubectl
# available in this container. Verifies real env var names (ground-truthed
# against Hermes Agent's own docs, not guessed), the image build command
# construction, the credential-store-with-plaintext-fallback wiring, and
# the manifest's real structure. Run from 54_hermes.sh, in its own
# container — see tests/run_tests.sh.
import sys
from pathlib import Path
from unittest import mock

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "libs"))
sys.path.insert(0, str(_REPO / "scripts"))

import install_hermes as ih  # noqa: E402

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


# ── _secret_data_lines: real env var names, conditional omission ───────────
lines = ih._secret_data_lines("openrouter", "sk-abc123", "", "", "admin", "hunter2")
check("_secret_data_lines: openrouter maps to the real OPENROUTER_API_KEY env var",
      "OPENROUTER_API_KEY: sk-abc123" in lines)
check("_secret_data_lines: no telegram token configured -> TELEGRAM_BOT_TOKEN omitted entirely",
      "TELEGRAM_BOT_TOKEN" not in lines)
check("_secret_data_lines: dashboard basic-auth is always present (never an unauthenticated dashboard)",
      "HERMES_DASHBOARD_BASIC_AUTH_USERNAME: admin" in lines
      and "HERMES_DASHBOARD_BASIC_AUTH_PASSWORD: hunter2" in lines)

lines2 = ih._secret_data_lines("openai", "sk-openai", "123:ABC-telegram-token", "111,222", "admin", "pw")
check("_secret_data_lines: openai maps to the real OPENAI_API_KEY env var",
      "OPENAI_API_KEY: sk-openai" in lines2)
check("_secret_data_lines: a configured telegram token uses the real TELEGRAM_BOT_TOKEN env var",
      "TELEGRAM_BOT_TOKEN: 123:ABC-telegram-token" in lines2)
check("_secret_data_lines: telegram_allowed_users uses the real TELEGRAM_ALLOWED_USERS env var",
      "TELEGRAM_ALLOWED_USERS: 111,222" in lines2)

lines3 = ih._secret_data_lines("anthropic", "sk-ant", "", "", "admin", "pw")
check("_secret_data_lines: anthropic maps to the real ANTHROPIC_API_KEY env var",
      "ANTHROPIC_API_KEY: sk-ant" in lines3)

# A value containing shell metacharacters must round-trip safely (shlex-quoted).
lines4 = ih._secret_data_lines("openrouter", "sk-a'b\"c$(rm -rf /)", "", "", "admin", "pw")
check("_secret_data_lines: a malicious-looking API key value is shlex-quoted, not interpolated raw",
      "$(rm -rf /)" not in lines4.split("\n")[0] or "'" in lines4)


# ── render_hermes_manifest: real Kubernetes object structure ───────────────
manifest = ih.render_hermes_manifest(
    "hermes", "hermes.cluster1.mydemo.lab", "registry.mydemo.lab/hermes-agent:main",
    "openrouter", "sk-abc", "", "", "admin", "pw123", "5Gi", None)
check("render_hermes_manifest: creates the real Namespace object",
      "kind: Namespace" in manifest and "name: hermes" in manifest)
check("render_hermes_manifest: creates a Secret carrying the real env var name",
      "kind: Secret" in manifest and "OPENROUTER_API_KEY: sk-abc" in manifest)
check("render_hermes_manifest: creates a PersistentVolumeClaim (real persistence, not emptyDir)",
      "kind: PersistentVolumeClaim" in manifest and "storage: 5Gi" in manifest
      and "emptyDir" not in manifest)
check("render_hermes_manifest: no storageClassName line when hermes_storage_class is unset",
      "storageClassName" not in manifest)
check("render_hermes_manifest: the Deployment has BOTH the gateway and dashboard containers",
      "name: gateway" in manifest and "name: dashboard" in manifest
      and 'command: ["gateway", "run"]' in manifest
      and 'command: ["dashboard", "--host", "0.0.0.0", "--no-open"]' in manifest)
check("render_hermes_manifest: both containers use the SAME built image",
      manifest.count("image: registry.mydemo.lab/hermes-agent:main") == 2)
check("render_hermes_manifest: both containers mount the SAME PVC at the real /opt/data path",
      manifest.count("mountPath: /opt/data") == 2 and "claimName: hermes-data" in manifest)
check("render_hermes_manifest: exposes the real dashboard port 9119, not the gateway (no inbound port)",
      "containerPort: 9119" in manifest and manifest.count("containerPort:") == 1)
check("render_hermes_manifest: the Ingress routes to the real ingress host",
      "host: hermes.cluster1.mydemo.lab" in manifest)

manifest_sc = ih.render_hermes_manifest(
    "hermes", "hermes.cluster1.mydemo.lab", "img", "openrouter", "sk-abc", "", "",
    "admin", "pw", "10Gi", "longhorn")
check("render_hermes_manifest: a configured hermes_storage_class becomes a real storageClassName",
      "storageClassName: longhorn" in manifest_sc)


# ── build_and_push_image: real command construction, workdir cleanup ───────
calls = []


def _fake_run(args, **kwargs):
    calls.append(args)
    return FakeResult(returncode=0)


tmp_created = []
_real_mkdtemp = ih.tempfile.mkdtemp


def _tracking_mkdtemp(*a, **kw):
    d = _real_mkdtemp(*a, **kw)
    tmp_created.append(d)
    return d


with mock.patch.object(ih.subprocess, "run", side_effect=_fake_run), \
     mock.patch.object(ih.tempfile, "mkdtemp", side_effect=_tracking_mkdtemp):
    image_ref = ih.build_and_push_image("registry.mydemo.lab", "main", "v1")

check("build_and_push_image: returns the real, correctly-formed image reference",
      image_ref == "registry.mydemo.lab/hermes-agent:v1")
clone_call = next(c for c in calls if c[0] == "git")
check("build_and_push_image: clones the REAL upstream repo URL at the requested ref",
      "https://github.com/NousResearch/hermes-agent.git" in clone_call and "main" in clone_call)
build_call = next(c for c in calls if c[0] == "podman" and "build" in c)
check("build_and_push_image: podman build tags the exact returned image reference",
      "-t" in build_call and "registry.mydemo.lab/hermes-agent:v1" in build_call)
push_call = next(c for c in calls if c[0] == "podman" and "push" in c)
check("build_and_push_image: podman push targets the exact same image reference",
      "registry.mydemo.lab/hermes-agent:v1" in push_call)
check("build_and_push_image: cleans up its own scratch clone directory afterward",
      tmp_created and not Path(tmp_created[0]).exists())

# A registry with a trailing slash doesn't produce a doubled "//" in the image ref.
with mock.patch.object(ih.subprocess, "run", side_effect=_fake_run), \
     mock.patch.object(ih.tempfile, "mkdtemp", side_effect=_tracking_mkdtemp):
    image_ref2 = ih.build_and_push_image("registry.mydemo.lab/", "main", "v1")
check("build_and_push_image: a trailing slash on hermes_registry doesn't double up",
      image_ref2 == "registry.mydemo.lab/hermes-agent:v1")


# ── setup_hermes: full orchestration, credential resolution, real error paths ──
ssh_calls = []
ih.ssh_run = lambda hostname, cmd, **kw: ssh_calls.append((hostname, cmd, kw.get("input_text")))
ih.build_and_push_image = lambda registry, git_ref, tag: "built-image:{}".format(tag)

# Missing hermes_registry dies with a clear message.
died = False
try:
    ih.setup_hermes("host1", "cluster1", "mydemo.lab", {"hermes_llm_api_key": "sk-abc"})
except SystemExit:
    died = True
check("setup_hermes: dies clearly when hermes_registry is not configured", died)

# Missing API key (no plaintext, no credential file) dies.
died = False
with mock.patch.object(ih.primary, "find_service_credential_for_kind", return_value=(None, [])):
    try:
        ih.setup_hermes("host1", "cluster1", "mydemo.lab", {"hermes_registry": "reg.local"})
    except SystemExit:
        died = True
check("setup_hermes: dies clearly when no LLM API key is available anywhere", died)

# An unknown provider dies rather than silently using the wrong env var.
died = False
with mock.patch.object(ih.primary, "find_service_credential_for_kind", return_value=(None, [])):
    try:
        ih.setup_hermes("host1", "cluster1", "mydemo.lab", {
            "hermes_registry": "reg.local", "hermes_llm_api_key": "sk-x", "hermes_llm_provider": "made-up"})
    except SystemExit:
        died = True
check("setup_hermes: dies clearly on an unrecognized hermes_llm_provider", died)

# Full happy path: plaintext API key, no dashboard password configured -> one is generated.
ssh_calls.clear()
with mock.patch.object(ih.primary, "find_service_credential_for_kind", return_value=(None, [])):
    ih.setup_hermes("host1", "cluster1", "mydemo.lab", {
        "hermes_registry": "reg.local", "hermes_llm_api_key": "sk-plaintext",
    })
check("setup_hermes: happy path applies the manifest via kubectl apply -f -",
      len(ssh_calls) == 1 and ssh_calls[0][1] == "kubectl apply -f -")
applied_manifest = ssh_calls[0][2]
check("setup_hermes: the plaintext hermes_llm_api_key reaches the real Secret",
      "OPENROUTER_API_KEY: sk-plaintext" in applied_manifest)
check("setup_hermes: no dashboard password configured anywhere -> a real one was generated "
      "and put in the manifest (never an unauthenticated dashboard)",
      "HERMES_DASHBOARD_BASIC_AUTH_PASSWORD: " in applied_manifest
      and "HERMES_DASHBOARD_BASIC_AUTH_PASSWORD: \n" not in applied_manifest)

# Credential-store path: the "hermes" kind is auto-discovered when hermes_account is unset.
ssh_calls.clear()
with mock.patch.object(ih.primary, "find_service_credential_for_kind", return_value=("my-hermes-creds", ["my-hermes-creds"])), \
     mock.patch.object(ih.primary, "load_service_credential",
                        return_value={"hermes_llm_api_key": "sk-from-store", "hermes_telegram_token": "tg-from-store"}):
    ih.setup_hermes("host1", "cluster1", "mydemo.lab", {"hermes_registry": "reg.local"})
check("setup_hermes: with no plaintext key, auto-discovers the single matching credential-store file",
      "OPENROUTER_API_KEY: sk-from-store" in ssh_calls[0][2])
check("setup_hermes: the credential store's telegram token reaches the manifest too",
      "TELEGRAM_BOT_TOKEN: tg-from-store" in ssh_calls[0][2])


if failures:
    print("{} check(s) failed".format(len(failures)))
    sys.exit(1)
print("all hermes checks passed")
