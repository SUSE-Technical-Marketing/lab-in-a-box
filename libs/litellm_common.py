#!/usr/bin/env python3
"""
LiteLLM proxy (https://docs.litellm.ai): an OpenAI-compatible endpoint in front of one hosted model, installed from the
litellm-helm chart. Used by the anthropic, gemini, kimi and openai add-ons.

The proxy runs without a database (no virtual keys or spend tracking). Clients authenticate with the master key kept in
the <release>-master-key secret; the provider's API key is read from the <release>-provider secret.
"""
# Part of lab-in-a-box
# Author/s: Raul Mahiques
# License: GPLv3

import secrets
import shlex

from lab_creation import ssh_run

DEFAULT_CHART = "oci://ghcr.io/berriai/litellm-helm"
PORT = 4000


def _env_cmd(var: str, value: str, cmd: str) -> str:
    """`cmd` run with `value` in environment variable `var`, so the value appears in no command's arguments."""
    return "{}={} bash -c {}".format(var, shlex.quote(value), shlex.quote(cmd))


def provider_secret(hostname: str, ns: str, release: str, api_key: str) -> None:
    """Create or update <release>-provider with PROVIDER_API_KEY."""
    ssh_run(hostname, _env_cmd("PROVIDER_API_KEY", api_key,
                               "kubectl create secret generic {}-provider -n {} --from-literal=PROVIDER_API_KEY=\"$PROVIDER_API_KEY\" "
                               "--dry-run=client -o yaml | kubectl apply -f -".format(release, shlex.quote(ns))))


def master_key_secret(hostname: str, ns: str, release: str) -> None:
    """Create <release>-master-key with a random sk-… key when it does not exist; an existing key is kept."""
    name = "{}-master-key".format(release)
    ssh_run(hostname, _env_cmd("LITELLM_MASTER_KEY", "sk-" + secrets.token_hex(16),
                               "kubectl get secret {0} -n {1} >/dev/null 2>&1 || kubectl create secret generic {0} -n {1} "
                               "--from-literal=masterkey=\"$LITELLM_MASTER_KEY\"".format(name, shlex.quote(ns))))


def chart_values(release: str, fqdn: str, model_name: str, model: str, api_base: str = "") -> list:
    """--set flags: one model reading its key from the provider secret, no database, the master-key secret, an ingress."""
    pairs = [
        ("db.deployStandalone", "false"),
        ("migrationJob.enabled", "false"),
        ("masterkeySecretName", "{}-master-key".format(release)),
        ("masterkeySecretKey", "masterkey"),
        ("environmentSecrets[0]", "{}-provider".format(release)),
        ("proxy_config.model_list[0].model_name", model_name),
        ("proxy_config.model_list[0].litellm_params.model", model),
        ("proxy_config.model_list[0].litellm_params.api_key", "os.environ/PROVIDER_API_KEY"),
    ]
    if api_base:
        pairs.append(("proxy_config.model_list[0].litellm_params.api_base", api_base))
    pairs += [
        ("ingress.enabled", "true"),
        ("ingress.className", ""),
        ("ingress.hosts[0].host", fqdn),
        ("ingress.hosts[0].paths[0].path", "/"),
        ("ingress.hosts[0].paths[0].pathType", "Prefix"),
    ]
    return ["--set {}".format(shlex.quote("{}={}".format(k, v))) for k, v in pairs]


def setup_litellm_proxy(hostname: str, release: str, ns: str, fqdn: str, model_name: str, model: str, api_key: str,
                        chart: str = "", version: str = "", api_base: str = "") -> None:
    """Install or upgrade the proxy `release` in `ns`, serving `model` as `model_name` at http://<fqdn>/v1."""
    ssh_run(hostname, "kubectl create namespace {0} --dry-run=client -o yaml | kubectl apply -f -".format(shlex.quote(ns)))
    provider_secret(hostname, ns, release, api_key)
    master_key_secret(hostname, ns, release)
    ver_arg = " --version {}".format(shlex.quote(version)) if version else ""
    ssh_run(hostname, "helm upgrade --install {} {} --namespace {}{} --wait --timeout 10m {}".format(
        release, shlex.quote(chart or DEFAULT_CHART), shlex.quote(ns), ver_arg,
        " ".join(chart_values(release, fqdn, model_name, model, api_base))))
    print("LiteLLM proxy installed. Namespace: {}".format(ns))
    print("OpenAI-compatible endpoint: http://{}/v1  (model: {})".format(fqdn, model_name))
    print("API key: kubectl -n {} get secret {}-master-key -o jsonpath='{{.data.masterkey}}' | base64 -d".format(ns, release))
    print("Usage: curl http://{}/v1/chat/completions -H \"Authorization: Bearer <API key>\" -H 'Content-Type: application/json' "
          "-d '{{\"model\": \"{}\", \"messages\": [{{\"role\": \"user\", \"content\": \"Hello\"}}]}}'".format(fqdn, model_name))
