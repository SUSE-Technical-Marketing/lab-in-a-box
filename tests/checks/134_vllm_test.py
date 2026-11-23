#!/usr/bin/env python3.11
# install_vllm — see 134_vllm.sh.
import importlib.util
import sys
from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest import mock

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "libs"))


def _load(name):
    loader = SourceFileLoader(name, str(_REPO / "scripts" / name))
    module = importlib.util.module_from_spec(importlib.util.spec_from_loader(name, loader))
    loader.exec_module(module)
    return module


vl = _load("install_vllm")

import addon_common as ac  # noqa: E402

failures = []


def check(desc, cond):
    if not cond:
        failures.append(desc)
        print("FAIL:", desc)


def values(cfg, host="vllm.c1.mydemo.lab", ingress_class="nginx", hf_token=""):
    """helm_values() as a dict, which is what the assertions are about."""
    return dict(vl.helm_values(cfg, host, ingress_class, hf_token))


def run_setup(cfg, allocatable="1\n"):
    """setup_vllm() with `cfg`; returns (commands, stdin texts, environment-carried values, printed, host)."""
    calls, printed = [], []

    def fake_ssh(host, cmd, **kw):
        calls.append((cmd, kw.get("input_text") or ""))
        return mock.Mock(returncode=0, stdout="")

    with mock.patch.object(vl, "ssh_run", side_effect=fake_ssh), \
            mock.patch.object(vl, "ssh_output", return_value=allocatable), \
            mock.patch("builtins.print", side_effect=lambda *a, **kw: printed.append(" ".join(str(x) for x in a))):
        host = vl.setup_vllm("node1.mydemo.lab", "c1", "mydemo.lab", cfg, ingress_class="nginx")
    return [c for c, _ in calls], [i for _, i in calls], printed, host


# ── the chart source ─────────────────────────────────────────────────────────

check("the upstream chart is the default, so no account is needed to use this add-on",
      vl.from_appcollection({}) is False)
check("the Application Collection is opt-in, by name",
      vl.from_appcollection({"vllm_source": "appcollection"}) is True)
check("upstream installs the chart from the repo alias", vl.chart_reference({}, "vllm") == "vllm/vllm-stack")
check("a different chart name in that repo is honoured",
      vl.chart_reference({"vllm_chart": "vllm"}, "vllm") == "vllm/vllm")
check("the Collection installs its OCI chart by slug, whatever the repo alias",
      vl.chart_reference({"vllm_source": "appcollection"}, "vllm")
      == "oci://dp.apps.rancher.io/charts/vllm")

# ── the model and its name ───────────────────────────────────────────────────

check("the model has a documented default", vl.model({}) == "microsoft/Phi-3-mini-4k-instruct")
check("the served name comes from the model's own last path component",
      vl.model_name({}) == "phi-3-mini-4k-instruct")
check("a name given by the lab wins", vl.model_name({"vllm_model_name": "chat"}) == "chat")
check("the name is reduced to something Kubernetes accepts as part of an object name",
      vl.model_name({"vllm_model": "org/Llama-3.1_8B.Instruct"}) == "llama-3-1-8b-instruct")
check("a model path with a trailing slash still yields a name",
      vl.model_name({"vllm_model": "/models/mistral/"}) == "mistral")

check("the upstream image is the project's own", vl.image({}) == ("lmcache/vllm-openai", "latest"))
check("the Collection has its own build of it",
      vl.image({"vllm_source": "appcollection"}) == ("containers/vllm-openai", "0.19.0"))
check("both parts of the image can be overridden",
      vl.image({"vllm_image": "my/vllm", "vllm_image_tag": "v1"}) == ("my/vllm", "v1"))

check("the ingress hostname is the prefix, the kcluster and the domain",
      vl.fqdn({}, "c1", "mydemo.lab") == "vllm.c1.mydemo.lab")
check("a lab with no kcluster name still gets a hostname",
      vl.fqdn({"vllm_shorthn": "llm"}, "", "mydemo.lab") == "llm.mydemo.lab")

# ── what the chart is told ───────────────────────────────────────────────────

v = values({})
check("the one model entry is enabled, since the chart ships its example disabled",
      v["servingEngineSpec.modelSpec[0].enabled"] == "true")
check("the model is passed as its URL",
      v["servingEngineSpec.modelSpec[0].modelURL"] == "microsoft/Phi-3-mini-4k-instruct")
check("one GPU per replica is requested by default",
      v["servingEngineSpec.modelSpec[0].requestGPU"] == "1")
check("the CPU and memory requests have defaults the chart would not otherwise get",
      v["servingEngineSpec.modelSpec[0].requestCPU"] == "6"
      and v["servingEngineSpec.modelSpec[0].requestMemory"] == "16Gi")
check("no volume is claimed unless the lab asks for one",
      not any(k.endswith("pvcStorage") for k in v))
check("no context length is sent unless the lab sets one",
      not any("maxModelLen" in k for k in v))
check("nothing is sent about a token when there is none", not any("hf_token" in k for k in v))

check("a GPU-less serving engine can be asked for explicitly",
      values({"vllm_gpu": "0"})["servingEngineSpec.modelSpec[0].requestGPU"] == "0")
check("the replica count reaches the model entry",
      values({"vllm_replicas": 3})["servingEngineSpec.modelSpec[0].replicaCount"] == "3")

v = values({"vllm_storage_size": "50Gi", "vllm_storage_class": "longhorn"})
check("a weights cache is claimed with its size, class and access mode",
      v["servingEngineSpec.modelSpec[0].pvcStorage"] == "50Gi"
      and v["servingEngineSpec.modelSpec[0].storageClass"] == "longhorn"
      and v["servingEngineSpec.modelSpec[0].pvcAccessMode[0]"] == "ReadWriteOnce")
check("the context length is sent when the lab lowers it",
      values({"vllm_max_model_len": 4096})["servingEngineSpec.modelSpec[0].vllmConfig.maxModelLen"] == "4096")

v = values({}, hf_token="hf-secret")
check("upstream is pointed at the Secret holding the token, never the token itself",
      v["servingEngineSpec.modelSpec[0].hf_token.secretName"] == "vllm-huggingface"
      and v["servingEngineSpec.modelSpec[0].hf_token.secretKey"] == "HUGGING_FACE_HUB_TOKEN"
      and "hf-secret" not in str(v))
v = values({"vllm_source": "appcollection"}, hf_token="hf-secret")
check("the Collection's chart is sent the token itself, which is the shape it documents",
      v["servingEngineSpec.modelSpec[0].hf_token"] == "hf-secret")
check("the Collection's chart is told which registry its image comes from",
      v["servingEngineSpec.modelSpec[0].registry"] == "dp.apps.rancher.io")

v = values({})
check("the router is exposed through an ingress for this lab's own hostname",
      v["routerSpec.ingress.enabled"] == "true"
      and v["routerSpec.ingress.hosts[0].host"] == "vllm.c1.mydemo.lab"
      and v["routerSpec.ingress.hosts[0].paths[0].pathType"] == "Prefix")
check("the ingress carries the kcluster's own class", v["routerSpec.ingress.className"] == "nginx")
check("no class is sent when the cluster's default controller should serve it",
      "routerSpec.ingress.className" not in values({}, ingress_class=""))

check("without an API key neither half of the chart is told to demand one",
      not any("vllmApiKey" in k for k in values({})))
v = values({"vllm_api_key": "sk-lab"})
check("an API key makes both the router and the serving engine read it from the Secret",
      v["routerSpec.vllmApiKey.secretName"] == "vllm-api-key"
      and v["servingEngineSpec.vllmApiKey.secretName"] == "vllm-api-key"
      and v["routerSpec.vllmApiKey.secretKey"] == "VLLM_API_KEY")
check("the key itself never becomes a chart value", "sk-lab" not in str(v))

check("vllm_set reaches the chart, which is how a key this add-on does not name is set",
      values({"vllm_set": {"routerSpec.routingLogic": "session"}})["routerSpec.routingLogic"] == "session")
check("vllm_set wins over what this add-on derived, since it is applied last",
      vl.helm_values({"vllm_set": {"servingEngineSpec.modelSpec[0].requestGPU": "4"}}, "h", "")[-1]
      == ("servingEngineSpec.modelSpec[0].requestGPU", "4"))

# ── the helm command ─────────────────────────────────────────────────────────

cmd = vl.helm_command({}, "vllm", "vllm/vllm-stack", [("a.b", "c")])
check("the release is installed or upgraded, never plain-installed, so a rerun is idempotent",
      cmd.startswith("helm upgrade -i vllm "))
check("the namespace is created with the release", "--create-namespace" in cmd)
check("no version is pinned unless the lab pins one", "--version" not in cmd)
check("each value is passed as its own --set", "--set a.b=c" in cmd)
check("a pinned chart version is passed through",
      "--version 1.2.3" in vl.helm_command({"vllm_release": "1.2.3"}, "vllm", "c", []))
check("the Collection's own --set flags are appended as given",
      vl.helm_command({}, "vllm", "c", [], " --set global.imagePullSecrets[0]=application-collection")
      .endswith(" --set global.imagePullSecrets[0]=application-collection"))

# ── installing ───────────────────────────────────────────────────────────────

cmds, stdin, printed, host = run_setup({})
check("the namespace exists before anything is installed into it",
      cmds[0].startswith("kubectl create namespace vllm "))
check("the chart is installed", any(c.startswith("helm upgrade -i vllm ") for c in cmds))
check("the hostname of the ingress is returned, for the DNS record", host == "vllm.c1.mydemo.lab")
check("the OpenAI-compatible endpoint is printed",
      any("http://vllm.c1.mydemo.lab/v1" in p for p in printed))
check("the in-cluster endpoint is printed too, which is what another add-on would use",
      any("vllm-router-service.vllm.svc.cluster.local:80/v1" in p for p in printed))
check("an open endpoint says so", any("vllm_api_key" in p for p in printed))

cmds, stdin, printed, host = run_setup({"vllm_api_key": "sk-lab", "vllm_ns": "ai"})
secret = next(c for c in cmds if "create secret generic" in c)
check("the API key reaches the node in an environment variable, not in a command line",
      secret.startswith("SECRET_VALUE=sk-lab bash -c ")
      and '--from-literal=VLLM_API_KEY="$SECRET_VALUE"' in secret)
check("the Secret is created in the release's own namespace", "--namespace ai" in secret)
check("the namespace the lab chose is used throughout",
      all("vllm " not in c.split("namespace ")[-1] for c in cmds if "create namespace" in c))
check("an endpoint behind a key does not print the open-endpoint warning",
      not any("set vllm_api_key" in p for p in printed))

cmds, stdin, printed, host = run_setup({}, allocatable="0\n0\n")
check("a cluster with no allocatable GPU is warned about, since the pods would stay Pending",
      any("nvidia.com/gpu" in p and "WARNING" in p for p in printed))
cmds, stdin, printed, host = run_setup({"vllm_gpu": "0"}, allocatable="0\n")
check("a model that asks for no GPU raises no GPU warning", not any("WARNING" in p for p in printed))
cmds, stdin, printed, host = run_setup({}, allocatable="\n2\n")
check("one node with a GPU is enough to install without a warning",
      not any("WARNING" in p for p in printed))

with mock.patch.object(vl, "ssh_run") as run, mock.patch.object(vl, "ssh_output", return_value="1"), \
        mock.patch("builtins.print"):
    vl.setup_vllm("node1", "c1", "mydemo.lab", {"vllm_hf_token_is": "unused"}, hf_token="hf-abc")
secret = [c for c, in [(call[0][1],) for call in run.call_args_list] if "create secret generic" in c]
check("the Hugging Face token is written into its own Secret, by environment variable",
      len(secret) == 1 and secret[0].startswith("SECRET_VALUE=hf-abc bash -c ")
      and "vllm-huggingface" in secret[0])

with mock.patch.object(vl, "ssh_run") as run, mock.patch.object(vl, "ssh_output", return_value="1"), \
        mock.patch("builtins.print"):
    vl.setup_vllm("node1", "c1", "mydemo.lab", {"vllm_source": "appcollection"}, hf_token="hf-abc")
check("the Collection's chart needs no token Secret, since the token is a chart value there",
      not any("create secret generic" in call[0][1] for call in run.call_args_list))


# ── credentials ──────────────────────────────────────────────────────────────

maps = []
with mock.patch.object(vl.ac, "resolve_credential",
                       side_effect=lambda cfg, kind, field_map, **kw: maps.append((kind, field_map, kw)) or
                       {"huggingface_token": "hf-from-store", "appcollection_user": "u",
                        "appcollection_password": "p"}):
    token = vl.hf_token({})
check("the token comes back from the credential store", token == "hf-from-store")
check("the token is looked up by the credential file's own field name, which is the direction "
      "resolve_credential() reads a file in",
      maps[0] == ("huggingface", {"huggingface_token": "vllm_hf_token"},
                  {"account_key": "vllm_hf_token_account"}))

with mock.patch.object(vl.ac, "resolve_credential", return_value={}):
    check("a lab with no credential at all serves an open model, with no token", vl.hf_token({}) == "")
check("the Application Collection account is resolved by the shared library function, which every add-on uses",
      vl.APPCOLLECTION_PREFIX == "vllm")


# ── validation ───────────────────────────────────────────────────────────────

def errors(cfg):
    v = ac.Validator({"vllm": cfg})
    vl._validate(v)
    return v.errors


check("_validate: the defaults alone are valid", errors({}) == [])
check("_validate: an unknown chart source is refused",
      any("vllm_source" in e for e in errors({"vllm_source": "wherever"})))
check("_validate: a replica count that is not a number is refused",
      any("vllm_replicas" in e for e in errors({"vllm_replicas": "two"})))
check("_validate: a GPU count that is not a number is refused",
      any("vllm_gpu" in e for e in errors({"vllm_gpu": "all"})))
check("_validate: vllm_set has to be an object of chart values",
      any("vllm_set" in e for e in errors({"vllm_set": "routerSpec.routingLogic=session"})))
check("_validate: a StorageClass with no volume to apply it to is refused, rather than silently ignored",
      any("vllm_storage_class" in e for e in errors({"vllm_storage_class": "longhorn"})))
check("_validate: a StorageClass with a volume is fine",
      errors({"vllm_storage_class": "longhorn", "vllm_storage_size": "50Gi"}) == [])
check("_validate: a token both inline and in the credential store is refused",
      any("vllm_hf_token" in e for e in errors({"vllm_hf_token": "x", "vllm_hf_token_account": "hf"})))
check("_validate: a model name that reduces to nothing is refused",
      any("vllm_model_name" in e for e in errors({"vllm_model_name": "___"})))
check("_validate: a non-URL chart repository is refused",
      any("vllm_repo_url" in e for e in errors({"vllm_repo_url": "vllm-project.github.io"})))


# ── --check ──────────────────────────────────────────────────────────────────

class FakeChecker:
    def __init__(self, definition, outputs):
        self.definition = definition
        self.vm_name = "node1.mydemo.lab"
        self.outputs = outputs
        self.oks = []
        self.fails = []

    def ok(self, what):
        self.oks.append(what)

    def fail(self, what, expected=None, found=None):
        self.fails.append(what)

    def expect(self, what, expected, found):
        (self.oks if str(expected) == str(found) else self.fails).append(what)


def run_check(cfg, outputs):
    c = FakeChecker({"vllm": cfg}, outputs)
    with mock.patch.object(vl, "ssh_output", side_effect=lambda host, cmd, **kw: outputs.pop(0)):
        vl._check(c)
    return c


c = run_check({}, ["1", "Running\n", "vllm.c1.mydemo.lab"])
check("--check: a healthy install passes every check", c.fails == [] and len(c.oks) == 3)
c = run_check({}, ["0", "", ""])
check("--check: a router with no ready replica fails", any("router" in f for f in c.fails))
c = run_check({}, ["1", "Pending\n", "vllm.c1.mydemo.lab"])
check("--check: a serving pod that is not Running is not counted as one",
      any("serving pods" in f for f in c.fails))
c = run_check({"vllm_replicas": 2}, ["1", "Running\nRunning\n", "h"])
check("--check: the replica count the lab asked for is what is expected",
      not any("serving pods" in f for f in c.fails))
c = run_check({}, ["1", "Running\n", ""])
check("--check: a missing ingress fails", any("ingress" in f for f in c.fails))

if failures:
    print("{} check(s) failed".format(len(failures)))
    sys.exit(1)
print("all vllm checks passed")
