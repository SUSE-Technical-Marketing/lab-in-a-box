#!/usr/bin/env python3.11
# Part of lab-in-a-box, it will install Hermes Agent (Nous Research's
# self-improving personal AI agent) on Kubernetes
# Author/s: Raul Mahiques
# License: GPLv3
#
# Project: https://github.com/NousResearch/hermes-agent (Nous Research, MIT)
#
# Hermes Agent has NO published container image (its own docker-compose.yml
# uses `build: .` against the repo's real Dockerfile — confirmed live
# 2026-09-21 by reading both files directly) — so, per explicit user
# request, THIS addon builds the image itself rather than requiring an
# operator-supplied one (contrast with install_colt.py's own "operator
# must build+push their own image" stance for a similar no-published-image
# project). The build is a real multi-stage one (a pinned-SQLite compile,
# Node 26, Python 3.13, Playwright) — it runs LOCALLY on the automation VM
# (a plain subprocess `git clone` + `podman build` + `podman push`, no SSH),
# not on the target Kubernetes node, since podman is already a confirmed
# automation-VM package (see this repo's own CLAUDE.md "Dependencies"
# section) while a k8s worker node has no such guarantee and shouldn't be
# made to compete with the cluster's own workloads for a heavy build.
#
# Deployment shape: gateway (the actual bot — Telegram/Discord/Slack/etc.,
# outbound-only, no Service/Ingress needed) and dashboard (a local web
# UI for credential/config management) as two containers in ONE pod,
# sharing one PersistentVolumeClaim at /opt/data (Hermes' own real,
# confirmed mount path for ~/.hermes — its conversation history, memory,
# skills, and session database all live there). Real env var names below
# (OPENROUTER_API_KEY, TELEGRAM_BOT_TOKEN, HERMES_DASHBOARD_BASIC_AUTH_*,
# ...) are ground-truthed directly against the project's own
# website/docs/reference/environment-variables.md, fetched raw — not
# guessed. The dashboard's real "username/password provider... quickest
# option for a backend on a trusted LAN" (its own doc's exact words) is
# wired up by default rather than left open, addressing that same doc's
# own explicit warning that an unauthenticated non-loopback bind is unsafe.
#
# JSON section: "hermes" — configurable keys:
#   hermes_registry          : [MANDATORY] registry to push the built image to (e.g.
#                               "registry.mydemo.lab" or a real per-cluster registry from this
#                               project's own "harbor" addon) — must already be reachable from
#                               BOTH the automation VM (to push) and the target cluster (to pull).
#   hermes_git_ref            : [OPTIONAL] git ref (branch/tag/commit) of NousResearch/hermes-agent
#                               to build (default: "main" — the project publishes no tagged
#                               releases as of this addon's own research, only a rolling main;
#                               pin a real commit SHA here for reproducibility across re-installs).
#   hermes_image_tag          : [OPTIONAL] tag for the built image (default: hermes_git_ref itself)
#   hermes_ns                 : [OPTIONAL] namespace (default: hermes)
#   hermes_shorthn             : [OPTIONAL] dashboard ingress hostname prefix (default: hermes)
#   hermes_llm_provider        : [OPTIONAL] "openrouter" (default) | "openai" | "anthropic" — which
#                               real env var (OPENROUTER_API_KEY / OPENAI_API_KEY /
#                               ANTHROPIC_API_KEY) the API key below is injected as.
#   hermes_llm_api_key         : [MANDATORY unless hermes_account/credential store provides it]
#                               API key for hermes_llm_provider.
#   hermes_telegram_token      : [OPTIONAL] Telegram bot token (from @BotFather) — the real
#                               TELEGRAM_BOT_TOKEN env var. Leave unset to run Hermes without any
#                               messaging platform wired up yet (still deployable, just not
#                               reachable from anywhere until a platform is configured, matching
#                               Hermes' own "hermes gateway setup" being a later, separate step).
#   hermes_telegram_allowed_users : [OPTIONAL] comma-separated Telegram user IDs allowed to use the
#                               bot (the real TELEGRAM_ALLOWED_USERS env var) — strongly
#                               recommended whenever hermes_telegram_token is set; an unrestricted
#                               bot token left fully open is a real, avoidable exposure.
#   hermes_account             : [OPTIONAL] name of an encrypted credential_kind "hermes" file
#                               under /etc/lab_creation/credentials/ (see README's Credentials
#                               section) to read hermes_llm_api_key/hermes_telegram_token/
#                               hermes_dashboard_password from instead of this section's own
#                               plaintext fields — auto-discovered if exactly one "hermes"
#                               credential file exists and this is left unset. The plaintext
#                               fields above remain fully valid either way (setup_credentials.py's
#                               default-to-credential-store-but-allow-plaintext convention).
#   hermes_dashboard_user       : [OPTIONAL] dashboard basic-auth username (default: admin)
#   hermes_dashboard_password   : [OPTIONAL] dashboard basic-auth password — the real
#                               HERMES_DASHBOARD_BASIC_AUTH_PASSWORD env var. A random one is
#                               generated and printed if left unset everywhere (plaintext field,
#                               credential store, AND this field) rather than deploying an
#                               unauthenticated dashboard.
#   hermes_storage_size         : [OPTIONAL] PVC size for /opt/data (default: 5Gi)
#   hermes_storage_class        : [OPTIONAL] StorageClass name (default: cluster's own default —
#                               see install_open_webui.py's own note: a bare RKE2 cluster has NO
#                               default StorageClass; install one first, e.g. this project's own
#                               "longhorn" addon)
#
# NOT live-tested — no real Kubernetes cluster with outbound internet access (needed for the
# image build's own package/Playwright downloads) was available in this session to build+push+
# deploy against. The image-build command construction, manifest shape, and every env var name
# are ground-truthed against the real upstream repo (Dockerfile, docker-compose.yml, and the
# environment-variables reference doc, all fetched raw), not guessed — but the full pipeline
# end to end has only been exercised via the mocked test suite.

__version__ = "__LABVERSION__"

PLUGIN = {
    "name": "hermes",
    "targets": ["container"],
    "layers": ["kubernetes"],
    "requires_kubernetes": ["rke2", "k3s"],
    "aux_services": [],
}

import secrets
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

for _candidate in ("/usr/local/lib/lab_creation", str(Path(__file__).resolve().parent.parent / "libs")):
    if Path(_candidate).is_dir() and _candidate not in sys.path:
        sys.path.insert(0, _candidate)

import addon_common as ac  # noqa: E402
import primary  # noqa: E402
import k8s  # noqa: E402
from lab_creation import ssh_run, die  # noqa: E402

_REPO_URL = "https://github.com/NousResearch/hermes-agent.git"

# Ground-truthed against website/docs/reference/environment-variables.md
# ("## LLM Providers" section) — real, confirmed env var names, not guessed.
_PROVIDER_ENV_VARS = {
    "openrouter": "OPENROUTER_API_KEY",
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
}

_MANIFEST_TEMPLATE = """---
apiVersion: v1
kind: Namespace
metadata:
  name: {ns}
---
apiVersion: v1
kind: Secret
metadata:
  name: hermes-secrets
  namespace: {ns}
type: Opaque
stringData:
{secret_data}
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: hermes-data
  namespace: {ns}
spec:
  accessModes:
    - ReadWriteOnce
{storage_class_line}  resources:
    requests:
      storage: {storage_size}
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: hermes
  namespace: {ns}
spec:
  replicas: 1
  selector:
    matchLabels:
      app: hermes
  template:
    metadata:
      labels:
        app: hermes
    spec:
      containers:
        - name: gateway
          image: {image}
          command: ["gateway", "run"]
          envFrom:
            - secretRef:
                name: hermes-secrets
          volumeMounts:
            - name: data
              mountPath: /opt/data
        - name: dashboard
          image: {image}
          command: ["dashboard", "--host", "0.0.0.0", "--no-open"]
          ports:
            - containerPort: 9119
              name: web
          envFrom:
            - secretRef:
                name: hermes-secrets
          volumeMounts:
            - name: data
              mountPath: /opt/data
      volumes:
        - name: data
          persistentVolumeClaim:
            claimName: hermes-data
---
apiVersion: v1
kind: Service
metadata:
  name: hermes-dashboard
  namespace: {ns}
spec:
  ports:
    - port: 9119
      name: web
  selector:
    app: hermes
---
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: hermes-dashboard
  namespace: {ns}
spec:
  rules:
    - host: {host}
      http:
        paths:
          - backend:
              service:
                name: hermes-dashboard
                port:
                  name: web
            path: /
            pathType: Prefix
"""


def _validate(v):
    v.vns("hermes")
    v.vreq_or_credential("hermes", "hermes_llm_api_key", "hermes", account_field="hermes_account")


def build_and_push_image(registry, git_ref, image_tag):
    """
    Clones NousResearch/hermes-agent at `git_ref` and builds+pushes its own
    real Dockerfile via a LOCAL (not SSH) podman build/push — see this
    module's own header comment for why this runs on the automation VM
    rather than a cluster node. Returns the full image reference
    ("<registry>/hermes-agent:<tag>").

    Real subprocess calls, not ssh_run() — this addon's own process is
    already running on the automation VM, so there's no remote host to
    reach for this step. Streams build output live (no output capturing)
    since this is a long-running, genuinely useful-to-watch operation —
    matches this project's own general stance on visible progress for
    long operations rather than a silent wait.
    """
    image_ref = "{}/hermes-agent:{}".format(registry.rstrip("/"), image_tag)
    workdir = tempfile.mkdtemp(prefix="hermes-agent-build-")
    try:
        print("- Cloning NousResearch/hermes-agent @ {} …".format(git_ref))
        subprocess.run(["git", "clone", "--depth", "1", "--branch", git_ref, _REPO_URL, workdir], check=True)

        print("- Building {} (real multi-stage build — SQLite compile, Node 26, Python 3.13, "
              "Playwright — this takes a while) …".format(image_ref))
        subprocess.run(["podman", "build", "-t", image_ref, workdir], check=True)

        print("- Pushing {} …".format(image_ref))
        subprocess.run(["podman", "push", image_ref], check=True)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    return image_ref


def _secret_data_lines(provider, api_key, telegram_token, telegram_allowed_users,
                        dashboard_user, dashboard_password):
    """
    Builds the Secret's stringData block — real env var names only (see
    module header). Every line here becomes an actual container env var
    via the Deployment's envFrom/secretRef, so a key not needed (e.g. no
    Telegram token configured) is simply omitted rather than written empty
    — Hermes itself treats an unset var and an empty one differently in
    some paths (e.g. TELEGRAM_ALLOWED_USERS empty vs. absent), so omission
    is the correct/safe choice, not a shortcut.
    """
    lines = ['  {}: {}'.format(_PROVIDER_ENV_VARS[provider], shlex.quote(api_key))]
    if telegram_token:
        lines.append('  TELEGRAM_BOT_TOKEN: {}'.format(shlex.quote(telegram_token)))
    if telegram_allowed_users:
        lines.append('  TELEGRAM_ALLOWED_USERS: {}'.format(shlex.quote(telegram_allowed_users)))
    lines.append('  HERMES_DASHBOARD_BASIC_AUTH_USERNAME: {}'.format(shlex.quote(dashboard_user)))
    lines.append('  HERMES_DASHBOARD_BASIC_AUTH_PASSWORD: {}'.format(shlex.quote(dashboard_password)))
    return "\n".join(lines) + "\n"


def render_hermes_manifest(ns, host, image, provider, api_key, telegram_token, telegram_allowed_users,
                            dashboard_user, dashboard_password, storage_size, storage_class):
    storage_class_line = "  storageClassName: {}\n".format(storage_class) if storage_class else ""
    return _MANIFEST_TEMPLATE.format(
        ns=ns, host=host, image=image,
        secret_data=_secret_data_lines(provider, api_key, telegram_token, telegram_allowed_users,
                                        dashboard_user, dashboard_password),
        storage_class_line=storage_class_line, storage_size=storage_size,
    )


def setup_hermes(hostname, clu_name, mydomain, cfg):
    ns = ac.require_k8s_name(cfg, "hermes_ns", "hermes")
    shorthn = ac.require_k8s_name(cfg, "hermes_shorthn", "hermes")
    host = "{}.{}.{}".format(shorthn, clu_name, mydomain)

    provider = cfg.get("hermes_llm_provider") or "openrouter"
    if provider not in _PROVIDER_ENV_VARS:
        die("hermes_llm_provider '{}' is not one of {} — no other provider's real env var name "
            "has been ground-truthed for this addon yet".format(provider, sorted(_PROVIDER_ENV_VARS)))

    creds = ac.resolve_credential(cfg, "hermes", {
        "hermes_llm_api_key": "hermes_llm_api_key",
        "hermes_telegram_token": "hermes_telegram_token",
        "hermes_dashboard_password": "hermes_dashboard_password",
    }, account_key="hermes_account")

    api_key = creds["hermes_llm_api_key"]
    if not api_key:
        die("hermes_llm_api_key is required (directly, via hermes_account, or via a single "
            "auto-discovered 'hermes' credential file)")

    telegram_token = creds["hermes_telegram_token"]
    telegram_allowed_users = cfg.get("hermes_telegram_allowed_users") or ""
    if telegram_token and not telegram_allowed_users:
        print("  WARNING: hermes_telegram_token is set but hermes_telegram_allowed_users is not — "
              "the bot will respond to ANY Telegram user who finds it. Strongly recommended to set "
              "hermes_telegram_allowed_users too.")

    dashboard_user = cfg.get("hermes_dashboard_user") or "admin"
    dashboard_password = creds["hermes_dashboard_password"]
    if not dashboard_password:
        dashboard_password = secrets.token_urlsafe(18)
        print("  No dashboard password configured anywhere — generated one: {}".format(dashboard_password))

    registry = cfg.get("hermes_registry")
    if not registry:
        die("hermes_registry is required — no published image exists for Hermes Agent, this addon "
            "builds and pushes its own, and needs somewhere reachable from both the automation VM "
            "and the target cluster to push it to")
    git_ref = cfg.get("hermes_git_ref") or "main"
    image_tag = cfg.get("hermes_image_tag") or git_ref

    image = build_and_push_image(registry, git_ref, image_tag)

    manifest = render_hermes_manifest(
        ns, host, image, provider, api_key, telegram_token, telegram_allowed_users,
        dashboard_user, dashboard_password,
        cfg.get("hermes_storage_size") or "5Gi", cfg.get("hermes_storage_class"),
    )
    ssh_run(hostname, "kubectl apply -f -", input_text=manifest)

    print("Hermes Agent deployed. Namespace: {}".format(ns))
    print("Dashboard available at: http://{} (user: {})".format(host, dashboard_user))
    if not telegram_token:
        print("No messaging platform configured yet — set hermes_telegram_token (or configure one "
              "via the dashboard) to actually reach the agent from anywhere.")


def main():
    ac.handle_common_args(__file__, __version__, validate_fn=_validate, plugin=PLUGIN)

    if len(sys.argv) < 2:
        print("Usage: {} <lab.json>".format(Path(sys.argv[0]).name))
        sys.exit(1)
    json_file = sys.argv[1]
    definition = primary.load_definition(json_file)

    target = k8s.first_server_node(definition)
    if not target:
        sys.exit(1)
    vm_name, _ssh_cmd = target

    clu_name = k8s.get_vm_kcluster(definition, vm_name)
    clu_cfg = k8s.load_kclu_vars(definition, clu_name) if clu_name else {}
    cfg = definition.get("hermes", {}) or {}

    setup_hermes(vm_name, clu_name, clu_cfg.get("mydomain"), cfg)


if __name__ == "__main__":
    main()
