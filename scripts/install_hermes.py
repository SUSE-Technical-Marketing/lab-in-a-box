#!/usr/bin/env python3.11
# Part of lab-in-a-box. Installs Hermes Agent, Nous Research's personal AI agent, on Kubernetes.
# Author/s: Raul Mahiques
# License: GPLv3
#
# Project: https://github.com/NousResearch/hermes-agent (MIT)
#
# The project publishes no container image, so this addon builds one. The build runs on the automation VM with podman, and the image is
# pushed to hermes_registry. The Dockerfile is the project's own. The build is multi-stage: a pinned SQLite, Node 26, Python 3.13 and
# Playwright.
#
# Deployment: one pod with two containers. gateway is the bot. It connects outbound to the messaging platform, so it needs no Service or
# Ingress. dashboard is a local web UI for credentials and configuration. Both share one PersistentVolumeClaim at /opt/data, which holds
# the conversation history, memory, skills and session database. The dashboard uses basic authentication by default, because the project
# warns against an unauthenticated bind on a non-loopback address.
#
# JSON section: "hermes"
#   hermes_registry               : [MANDATORY] registry to push the built image to, for example registry.mydemo.lab or the registry of the
#                                   harbor addon. The automation VM must reach it to push, and the cluster must reach it to pull.
#   hermes_git_ref                : git ref of NousResearch/hermes-agent to build (default main). The project has no tagged releases, so
#                                   pin a commit SHA for reproducible builds.
#   hermes_image_tag              : tag of the built image (default: hermes_git_ref)
#   hermes_ns                     : namespace (default hermes)
#   hermes_shorthn                : dashboard ingress hostname prefix (default hermes)
#   hermes_llm_provider           : "openrouter" (default), "openai" or "anthropic". The API key is set as OPENROUTER_API_KEY,
#                                   OPENAI_API_KEY or ANTHROPIC_API_KEY to match.
#   hermes_llm_api_key            : API key for the provider. Required, unless the credential store provides it.
#   hermes_telegram_token         : Telegram bot token from @BotFather, set as TELEGRAM_BOT_TOKEN. Unset, Hermes runs without a messaging platform.
#   hermes_telegram_allowed_users : comma-separated Telegram user IDs, set as TELEGRAM_ALLOWED_USERS. Set it whenever a bot token is set.
#   hermes_account                : name of an encrypted credential file of kind "hermes" under /etc/lab_creation/credentials/ for
#                                   hermes_llm_api_key, hermes_telegram_token and hermes_dashboard_password. It is auto-discovered when exactly
#                                   one such file exists and this is unset. The plaintext fields remain valid.
#   hermes_dashboard_user         : dashboard basic-auth user (default admin)
#   hermes_dashboard_password     : dashboard basic-auth password. When it is unset everywhere, a random one is generated and printed, so
#                                   the dashboard is never deployed open.
#   hermes_storage_size           : PersistentVolumeClaim size for /opt/data (default 5Gi)
#   hermes_storage_class          : StorageClass (default: the cluster's own default). A bare RKE2 cluster has none, so install one first,
#                                   for example with the longhorn addon.

__version__ = "0d70beb"

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
