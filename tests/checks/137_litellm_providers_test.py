#!/usr/bin/env python3.11
# The hosted-provider add-ons that delegate to litellm_common.install_provider() — see 137_litellm_providers.sh.
import importlib.util
import sys
from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest import mock

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "libs"))

import addon_common as ac  # noqa: E402
import k8s  # noqa: E402
import lab_creation  # noqa: E402
import litellm_common as ll  # noqa: E402

failures = []


def check(desc, cond):
    if not cond:
        failures.append(desc)
        print("FAIL:", desc)


def _load(path):
    loader = SourceFileLoader(path.name, str(path))
    module = importlib.util.module_from_spec(importlib.util.spec_from_loader(path.name, loader))
    loader.exec_module(module)
    return module


_schema = _load(_REPO / "scripts" / "lab_schema")


def run_install(section, cfg, clu="c1", domain="mydemo.lab"):
    """install_provider() against mocked SSH; returns (commands, printed, registered DNS names, fqdn)."""
    commands, printed, dns = [], [], []
    definition = {section: cfg, "kclusters": {clu: {"mydomain": domain}}}
    with mock.patch.object(ll, "ssh_run", side_effect=lambda host, cmd, **kw: commands.append(cmd)), \
            mock.patch.object(k8s, "first_server_node", return_value=("node1.mydemo.lab", "ssh")), \
            mock.patch.object(k8s, "get_vm_kcluster", return_value=clu), \
            mock.patch.object(k8s, "load_kclu_vars", return_value={"mydomain": domain}), \
            mock.patch.object(lab_creation, "setup_helm"), \
            mock.patch.object(ac, "register_service_dns", side_effect=lambda d, c, f: dns.append(f)), \
            mock.patch("builtins.print", side_effect=lambda *a, **kw: printed.append(" ".join(str(x) for x in a))):
        fqdn = ll.install_provider(definition, section)
    return commands, printed, dns, fqdn


# ── the provider table ───────────────────────────────────────────────────────

check("the table carries every provider as (label, route, console)",
      all(isinstance(v, tuple) and len(v) == 3 for v in ll.PROVIDERS.values()))
check("every route is a LiteLLM prefix, ending in the separator a model id follows it with",
      all(route.endswith("/") for _label, route, _console in ll.PROVIDERS.values()))
check("every provider says where its key comes from, over https",
      all(console.startswith("https://") for _label, _route, console in ll.PROVIDERS.values()))
try:
    ll.provider("nope")
    check("an unknown provider raises", False)
except KeyError:
    pass
check("a known provider comes back from the table", ll.provider("groq")[1] == "groq/")

# ── one add-on per provider, and no add-on outside the table ─────────────────

scripts = dict((p.name[len("install_"):], p) for p in (_REPO / "scripts").glob("install_*")
               if "litellm_common.install_provider(" in p.read_text(errors="replace"))
check("every provider in the table has its own add-on, and no add-on delegates without being in it: {}".format(
          sorted(set(ll.PROVIDERS) ^ set(scripts)) or "none missing either way"),
      sorted(scripts) == sorted(ll.PROVIDERS))
check("there are at least the 21 single-key providers this was built for", len(scripts) >= 21)

for name, path in sorted(scripts.items()):
    module = _load(path)
    check("{}: names its own provider".format(name), getattr(module, "PROVIDER", None) == name)
    check("{}: the add-on's PLUGIN name is the provider name".format(name), module.PLUGIN["name"] == name)
    check("{}: declares it needs an ingress, which the chart's values turn on".format(name),
          "ingress" in (module.PLUGIN.get("requires_cluster") or []))

    schema = _schema.parse_script(str(path))
    fields = dict((f["name"], f) for f in schema["fields"])
    check("{}: its JSON section is the provider name".format(name), schema["section"] == name)
    check("{}: documents exactly the seven fields of this shape".format(name),
          sorted(fields) == sorted("{}_{}".format(name, s) for s in
                                   ("api_key", "model", "api_base", "version", "chart", "ns", "shorthn")))
    check("{}: the key and the model are both mandatory, so nothing is guessed".format(name),
          fields["{}_api_key".format(name)]["required"] and fields["{}_model".format(name)]["required"])
    check("{}: its key field says where the key is obtained, since nothing can guess that".format(name),
          "http" in fields["{}_api_key".format(name)]["description"])
    check("{}: its model field points at LiteLLM's own provider list, since this project pins no model id".format(
              name),
          "docs.litellm.ai" in fields["{}_model".format(name)]["description"])

# ── validation, shared by all of them ────────────────────────────────────────


def errors(section, cfg):
    v = ac.Validator({section: cfg})
    ll.validate_provider(v, section)
    return v.errors


good = {"groq_api_key": "sk-x", "groq_model": "groq/some-model"}
check("a key and a model together are valid", errors("groq", good) == [])
check("a missing key is an error", any("groq_api_key" in e for e in errors("groq", {"groq_model": "m"})))
check("a missing model is an error, rather than a default nobody checked",
      any("groq_model" in e for e in errors("groq", {"groq_api_key": "k"})))
check("an empty section fails on both", len(errors("groq", {})) == 2)
check("a non-URL api_base is refused",
      any("groq_api_base" in e for e in errors("groq", dict(good, groq_api_base="api.groq.com"))))
check("a URL api_base is accepted",
      errors("groq", dict(good, groq_api_base="https://api.groq.com/openai/v1")) == [])
check("a namespace that Kubernetes would reject is refused",
      any("groq_ns" in e for e in errors("groq", dict(good, groq_ns="Not A Namespace"))))

# ── installing ───────────────────────────────────────────────────────────────

cmds, printed, dns, fqdn = run_install("groq", good)
check("the namespace is created first", cmds[0].startswith("kubectl create namespace groq "))
check("the endpoint is the add-on name, the kcluster and the domain", fqdn == "groq.c1.mydemo.lab")
check("the hostname is registered in DNS, since the add-on exposes it", dns == ["groq.c1.mydemo.lab"])
helm = next(c for c in cmds if c.startswith("helm upgrade --install "))
check("the release is upgraded or installed, so a rerun is idempotent",
      helm.startswith("helm upgrade --install groq-proxy "))
check("the litellm-helm chart is what is installed", "oci://ghcr.io/berriai/litellm-helm" in helm)
check("the model reaches the proxy's own model list",
      "proxy_config.model_list[0].litellm_params.model=groq/some-model" in helm.replace("'", ""))
check("the model is served under the add-on's name, which is what a client asks for",
      "proxy_config.model_list[0].model_name=groq" in helm.replace("'", ""))
check("the key is read from the Secret by the proxy, never passed as a value",
      "api_key=os.environ/PROVIDER_API_KEY" in helm.replace("'", ""))
check("no api_base is sent when the lab names none", "api_base" not in helm)
check("the ingress answers for the registered hostname",
      "ingress.hosts[0].host=groq.c1.mydemo.lab" in helm.replace("'", ""))
check("the API key never appears in a helm argument", "sk-x" not in helm)
secret = next(c for c in cmds if "create secret generic groq-proxy-provider" in c)
check("the key reaches the node in an environment variable",
      secret.startswith("PROVIDER_API_KEY=sk-x bash -c ") and '--from-literal=PROVIDER_API_KEY="$PROVIDER_API_KEY"'
      in secret)
check("a master key is created for clients, and kept if it already exists",
      any("groq-proxy-master-key" in c and "kubectl get secret" in c for c in cmds))
check("how to read that master key is printed, since nothing can call the proxy without it",
      any("masterkey" in p for p in printed))

cmds, printed, dns, fqdn = run_install("together", {
    "together_api_key": "k", "together_model": "together_ai/some-model",
    "together_api_base": "https://api.together.xyz/v1", "together_ns": "llm", "together_shorthn": "tai"})
helm = next(c for c in cmds if c.startswith("helm upgrade --install "))
check("an api_base the lab names is passed to the proxy",
      "litellm_params.api_base=https://api.together.xyz/v1" in helm.replace("'", ""))
check("the namespace and hostname prefix the lab chose are used",
      "--namespace llm" in helm and fqdn == "tai.c1.mydemo.lab")
check("the release name is derived from the add-on, not from the hostname",
      helm.startswith("helm upgrade --install together-proxy "))

cmds, printed, dns, fqdn = run_install("xai", {"xai_api_key": "k", "xai_model": "xai/grok",
                                               "xai_version": "0.1.2"})
helm = next(c for c in cmds if c.startswith("helm upgrade --install "))
check("a pinned chart version is passed through", "--version 0.1.2" in helm)

for missing in ({"groq_model": "groq/m"}, {"groq_api_key": "k"}):
    try:
        run_install("groq", missing)
        check("installing without the key or the model dies rather than deploying a broken proxy", False)
    except SystemExit:
        pass

if failures:
    print("{} check(s) failed".format(len(failures)))
    sys.exit(1)
print("all LiteLLM provider checks passed")
