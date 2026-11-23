#!/usr/bin/env python3
"""
LiteLLM proxy (https://docs.litellm.ai): an OpenAI-compatible endpoint in front of one hosted model, installed from the
litellm-helm chart. Used by the anthropic, gemini, kimi and openai add-ons, and by every add-on in PROVIDERS below,
each of which is a script that names its provider and delegates the whole install to install_provider().

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

# One hosted provider per add-on that delegates to install_provider(): its name as both the add-on and its JSON
# section, then (label, LiteLLM route, where its API key comes from).
#
# The route is the prefix LiteLLM normally expects in a model id, e.g. "groq/llama-3.3-70b-versatile". It is
# documentation, not a rule: no add-on validates a model against it, because LiteLLM's own routes move and a
# provider may also be reached as an OpenAI-compatible endpoint, with an "openai/" model and <name>_api_base. Each
# add-on's <name>_model field is MANDATORY for the same reason: a model id this project guessed would fail at the
# provider, long after the install reported success. Confirm both at https://docs.litellm.ai/docs/providers.
PROVIDERS = {
    "ai21": ("AI21", "ai21/", "https://studio.ai21.com"),
    "cerebras": ("Cerebras", "cerebras/", "https://cloud.cerebras.ai"),
    "cohere": ("Cohere", "cohere/", "https://dashboard.cohere.com"),
    "dashscope": ("Alibaba Cloud DashScope", "dashscope/", "https://dashscope.console.aliyun.com"),
    "deepinfra": ("DeepInfra", "deepinfra/", "https://deepinfra.com/dash/api_keys"),
    "ernie": ("Baidu ERNIE", "ernie/", "https://console.bce.baidu.com"),
    "fireworks": ("Fireworks AI", "fireworks_ai/", "https://fireworks.ai"),
    "groq": ("Groq", "groq/", "https://console.groq.com"),
    "huggingface": ("Hugging Face Inference", "huggingface/", "https://huggingface.co/settings/tokens"),
    "hyperbolic": ("Hyperbolic", "hyperbolic/", "https://app.hyperbolic.xyz"),
    "jina": ("Jina AI embeddings", "jina_ai/", "https://jina.ai"),
    "nebius": ("Nebius AI Studio", "nebius/", "https://studio.nebius.com"),
    "novita": ("Novita AI", "novita/", "https://novita.ai"),
    "openrouter": ("OpenRouter", "openrouter/", "https://openrouter.ai/keys"),
    "perplexity": ("Perplexity", "perplexity/", "https://www.perplexity.ai/settings/api"),
    "replicate": ("Replicate", "replicate/", "https://replicate.com/account/api-tokens"),
    "sambanova": ("SambaNova Cloud", "sambanova/", "https://cloud.sambanova.ai"),
    "together": ("Together AI", "together_ai/", "https://api.together.ai"),
    "voyage": ("Voyage AI embeddings", "voyage/", "https://dash.voyageai.com"),
    "xai": ("xAI Grok", "xai/", "https://console.x.ai"),
    "zai": ("Z.ai GLM", "zai/", "https://z.ai"),
}


def provider(name: str) -> tuple:
    """(label, route, console URL) of the hosted provider `name`, from PROVIDERS."""
    if name not in PROVIDERS:
        raise KeyError("no LiteLLM provider '{}' — add it to litellm_common.PROVIDERS".format(name))
    return PROVIDERS[name]


def validate_provider(v, section: str) -> None:
    """
    Validate the JSON section of a provider add-on: its key and its model are both required, and the rest follows
    the shapes every add-on's fields do. The model is required rather than defaulted, see PROVIDERS.
    """
    v.vreq(section, "{}_api_key".format(section))
    v.vreq(section, "{}_model".format(section))
    v.vns(section)
    v.vver(section)
    v.vurl_field(section, "{}_api_base".format(section))


def install_provider(definition: dict, section: str) -> str:
    """
    Install the LiteLLM proxy for the provider add-on `section` on the lab's kcluster, register its hostname in
    DNS, and return that hostname. The whole body of such an add-on's main(), so each one is its own header,
    PLUGIN and provider name and nothing else.
    """
    import addon_common as ac
    import k8s
    from lab_creation import die, setup_helm

    label, _route, console = provider(section)
    cfg = definition.get(section, {}) or {}
    for field in ("{}_api_key".format(section), "{}_model".format(section)):
        if not cfg.get(field):
            die("{}: {} is mandatory — the {} key comes from {}, and the model is named rather than defaulted "
                "because a guessed model id fails at the provider, not here".format(section, field, label, console))

    target = k8s.first_server_node(definition)
    if not target:
        die("{}: this lab has no kcluster server node to install the proxy on".format(section))
    vm_name, _ssh_cmd = target
    clu_name = k8s.get_vm_kcluster(definition, vm_name)
    clu_cfg = k8s.load_kclu_vars(definition, clu_name) if clu_name else {}

    setup_helm(vm_name, clu_name, online=definition.get("common", {}).get("online") == "1")
    fqdn = ".".join(p for p in (str(cfg.get("{}_shorthn".format(section)) or section), clu_name,
                                clu_cfg.get("mydomain", "")) if p)
    setup_litellm_proxy(vm_name, "{}-proxy".format(section.replace("_", "-")),
                        cfg.get("{}_ns".format(section)) or section, fqdn, section,
                        str(cfg["{}_model".format(section)]), str(cfg["{}_api_key".format(section)]),
                        chart=cfg.get("{}_chart".format(section)) or "",
                        version=cfg.get("{}_version".format(section)) or "",
                        api_base=cfg.get("{}_api_base".format(section)) or "")
    print("{} is proxied at http://{}/v1 — any OpenAI client reaches it there".format(label, fqdn))
    ac.register_service_dns(definition, clu_name, fqdn)
    return fqdn


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
