#!/usr/bin/env python3.11
# install_mcpo — see 135_mcpo.sh.
import importlib.util
import json
import sys
from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest import mock

import yaml

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "libs"))
_TEMPLATES = str(_REPO / "templates" / "addons") + "/"


def _load(name):
    loader = SourceFileLoader(name, str(_REPO / "scripts" / name))
    module = importlib.util.module_from_spec(importlib.util.spec_from_loader(name, loader))
    loader.exec_module(module)
    return module


mc = _load("install_mcpo")

import addon_common as ac  # noqa: E402

failures = []


def check(desc, cond):
    if not cond:
        failures.append(desc)
        print("FAIL:", desc)


def run_setup(cfg, key="k", ingress_class="nginx"):
    """setup_mcpo() with `cfg`, rendering the real template; returns (commands, stdin texts, printed, host)."""
    calls, printed = [], []

    def fake_ssh(host, cmd, **kw):
        calls.append((cmd, kw.get("input_text") or ""))
        return mock.Mock(returncode=0, stdout="")

    with mock.patch.object(mc, "ssh_run", side_effect=fake_ssh), \
            mock.patch("builtins.print", side_effect=lambda *a, **kw: printed.append(" ".join(str(x) for x in a))):
        host = mc.setup_mcpo("node1.mydemo.lab", "c1", "mydemo.lab", cfg, _TEMPLATES,
                             ingress_class=ingress_class, key=key)
    return [c for c, _ in calls], [i for _, i in calls], printed, host


def manifests(cfg, ingress_class="nginx"):
    """The rendered Kubernetes objects, by kind."""
    _cmds, stdin, _printed, _host = run_setup(cfg, ingress_class=ingress_class)
    rendered = [t for t in stdin if "kind: Deployment" in t]
    docs = [d for d in yaml.safe_load_all(rendered[0]) if d]
    return dict((d["kind"], d) for d in docs)


_TIME = {"command": "uvx", "args": ["mcp-server-time"]}
_REMOTE = {"type": "sse", "url": "http://elsewhere:8001/sse"}

# ── the servers it serves ────────────────────────────────────────────────────

check("a lab that names no server gets one that works out of the box",
      list(mc.servers({})) == ["memory"])
check("the default server needs nothing installed, since the image carries npx",
      mc.servers({})["memory"]["command"] == "npx")
check("the servers a lab names replace the default entirely",
      mc.servers({"mcpo_servers": {"time": _TIME}}) == {"time": _TIME})

config = json.loads(mc.config_json({"mcpo_servers": {"time": _TIME, "mcp": _REMOTE}}))
check("the configuration is mcpo's own shape, under mcpServers",
      sorted(config["mcpServers"]) == ["mcp", "time"] and config["mcpServers"]["time"] == _TIME)
check("a server that already runs elsewhere keeps its type and url",
      config["mcpServers"]["mcp"] == _REMOTE)
check("the same servers always render the same configuration, so a rerun changes nothing",
      mc.config_json({"mcpo_servers": {"b": _TIME, "a": _TIME}})
      == mc.config_json({"mcpo_servers": {"a": _TIME, "b": _TIME}}))
check("a changed configuration gets a different revision, which is what rolls the pods",
      mc.config_revision(mc.config_json({})) != mc.config_revision(mc.config_json({"mcpo_servers": {"t": _TIME}})))

check("the URL of each server is its own path under the ingress",
      mc.server_urls({"mcpo_servers": {"time": _TIME, "memory": _TIME}}, "mcpo.c1.lab")
      == [("memory", "http://mcpo.c1.lab/memory"), ("time", "http://mcpo.c1.lab/time")])
check("the ingress hostname is the prefix, the kcluster and the domain",
      mc.fqdn({}, "c1", "mydemo.lab") == "mcpo.c1.mydemo.lab")
check("the hostname prefix can be changed",
      mc.fqdn({"mcpo_shorthn": "tools"}, "c1", "mydemo.lab") == "tools.c1.mydemo.lab")

# ── the key, and the flags ───────────────────────────────────────────────────

check("a key the lab set is used as it is", mc.api_key({"mcpo_api_key": "sk-lab"}) == "sk-lab")
generated = mc.api_key({})
check("a lab that sets no key still gets one, so the endpoint is never open",
      len(generated) > 20 and generated != mc.api_key({}))
check("hot reload is off unless the lab asks for it", mc.extra_args({}) == "")
check("hot reload is a flag on mcpo's own command line",
      mc.extra_args({"mcpo_hot_reload": "true"}) == " --hot-reload")
check("the flag is read case-insensitively, like every other boolean field",
      mc.extra_args({"mcpo_hot_reload": "True"}) == " --hot-reload")

# ── the rendered manifests ───────────────────────────────────────────────────

objects = manifests({})
check("a Deployment, a Service and an Ingress are applied together",
      sorted(objects) == ["Deployment", "Ingress", "Service"])
container = objects["Deployment"]["spec"]["template"]["spec"]["containers"][0]
check("the image is upstream's own, at the documented tag",
      container["image"] == "ghcr.io/open-webui/mcpo:main")
check("mcpo is started against the configuration file it mounts",
      "--config /etc/mcpo/config.json" in container["args"][0])
check("the key reaches mcpo from the Secret, through the shell, not from the pod's own specification",
      '--api-key "$MCPO_API_KEY"' in container["args"][0]
      and container["env"][0]["valueFrom"]["secretKeyRef"]["name"] == "mcpo-api-key")
check("rendering the template never ran the environment reference as a command",
      "MCPO_API_KEY" in container["args"][0])
check("the configuration revision is on the pod, so a changed configuration restarts it",
      objects["Deployment"]["spec"]["template"]["metadata"]["annotations"][
          "lab-in-a-box.io/config-revision"] == mc.config_revision(mc.config_json({})))
check("the port inside the container is the one mcpo is told to listen on",
      container["ports"][0]["containerPort"] == 8000 and "--port 8000" in container["args"][0])
check("the Service answers on the port the lab asked for",
      manifests({"mcpo_port": 9000})["Service"]["spec"]["ports"][0]["port"] == 9000)
check("the ingress answers for this lab's own hostname, with the kcluster's ingress class",
      objects["Ingress"]["spec"]["rules"][0]["host"] == "mcpo.c1.mydemo.lab"
      and objects["Ingress"]["spec"]["ingressClassName"] == "nginx")
check("the ingress is left without a class when the cluster's default controller serves it",
      "ingressClassName" not in manifests({}, ingress_class="")["Ingress"]["spec"])
check("hot reload reaches the rendered command line",
      "--hot-reload" in manifests({"mcpo_hot_reload": "true"})[
          "Deployment"]["spec"]["template"]["spec"]["containers"][0]["args"][0])
check("the replica count the lab asked for is what is deployed",
      manifests({"mcpo_replicas": 2})["Deployment"]["spec"]["replicas"] == 2)

# ── installing ───────────────────────────────────────────────────────────────

cmds, stdin, printed, host = run_setup({"mcpo_servers": {"time": _TIME}}, key="sk-lab")
check("the namespace exists before anything is put into it",
      cmds[0].startswith("kubectl create namespace mcpo "))
check("the configuration file is written with a private umask before it becomes a ConfigMap",
      any(c.startswith("umask 077 && cat > /tmp/mcpo-config.json") for c in cmds))
check("what is written is the configuration itself",
      json.loads(stdin[cmds.index("umask 077 && cat > /tmp/mcpo-config.json")])["mcpServers"] == {"time": _TIME})
check("the ConfigMap is made from that file", any("create configmap mcpo-config --from-file=config.json=" in c
                                                 for c in cmds))
check("the temporary file does not stay on the node", any(c.startswith("rm -f /tmp/mcpo-config.json")
                                                          for c in cmds))
secret = next(c for c in cmds if "create secret generic" in c)
check("the key reaches the node in an environment variable, not in a command line",
      secret.startswith("SECRET_VALUE=sk-lab bash -c ")
      and '--from-literal=MCPO_API_KEY="$SECRET_VALUE"' in secret)
check("the manifests are applied from stdin", "kubectl apply -f -" in cmds)
check("the hostname of the ingress is returned, for the DNS record", host == "mcpo.c1.mydemo.lab")
check("every served server's URL is printed", any("http://mcpo.c1.mydemo.lab/time" in p for p in printed))
check("the documentation path of each server is printed, which is what makes it usable",
      any("/time/docs" in p for p in printed))
check("the bearer token is printed, since nothing can call mcpo without it",
      any("Bearer sk-lab" in p for p in printed))
check("a generated token says it was generated for this install",
      any("set mcpo_api_key" in p for p in run_setup({}, key="generated")[2]))
check("a token the lab chose prints no such note",
      not any("set mcpo_api_key" in p for p in run_setup({"mcpo_api_key": "sk-lab"}, key="sk-lab")[2]))
check("how to attach it to Open WebUI is printed, since that side is a step in its own interface",
      any("Open WebUI" in p for p in printed))

cmds, stdin, printed, host = run_setup({"mcpo_ns": "tools"})
check("the namespace the lab chose is used for the namespace, the ConfigMap and the Secret",
      cmds[0].startswith("kubectl create namespace tools ")
      and any("-n tools create configmap" in c for c in cmds)
      and any("--namespace tools" in c for c in cmds if "create secret" in c))


# ── validation ───────────────────────────────────────────────────────────────

def errors(cfg):
    v = ac.Validator({"mcpo": cfg})
    mc._validate(v)
    return v.errors


check("_validate: the defaults alone are valid", errors({}) == [])
check("_validate: a working set of servers is valid",
      errors({"mcpo_servers": {"time": _TIME, "mcp": _REMOTE}}) == [])
check("_validate: the servers have to be an object, not a list",
      any("mcpo_servers" in e for e in errors({"mcpo_servers": ["time"]})))
check("_validate: an empty object is refused, since it would serve nothing",
      any("mcpo_servers" in e for e in errors({"mcpo_servers": {}})))
check("_validate: a server that is not an object is refused",
      any("time" in e for e in errors({"mcpo_servers": {"time": "uvx mcp-server-time"}})))
check("_validate: a server with neither a command nor a type is refused",
      any("command" in e for e in errors({"mcpo_servers": {"time": {"args": ["x"]}}})))
check("_validate: an unknown transport type is refused",
      any("type" in e for e in errors({"mcpo_servers": {"m": {"type": "websocket", "url": "u"}}})))
check("_validate: a remote server with no url is refused",
      any("url" in e for e in errors({"mcpo_servers": {"m": {"type": "sse"}}})))
check("_validate: args have to be a list",
      any("args" in e for e in errors({"mcpo_servers": {"t": {"command": "uvx", "args": "x"}}})))
check("_validate: env has to be an object",
      any("env" in e for e in errors({"mcpo_servers": {"t": {"command": "uvx", "env": ["A=b"]}}})))
check("_validate: headers have to be an object",
      any("headers" in e for e in errors({"mcpo_servers": {"m": {"type": "sse", "url": "u",
                                                                 "headers": ["A: b"]}}})))
check("_validate: a port that is not one is refused", any("mcpo_port" in e for e in errors({"mcpo_port": "http"})))
check("_validate: hot reload is a boolean",
      any("mcpo_hot_reload" in e for e in errors({"mcpo_hot_reload": "yes"})))
check("_validate: a replica count that is not a number is refused",
      any("mcpo_replicas" in e for e in errors({"mcpo_replicas": "two"})))


# ── --check ──────────────────────────────────────────────────────────────────

class FakeChecker:
    def __init__(self, definition):
        self.definition = definition
        self.vm_name = "node1.mydemo.lab"
        self.oks = []
        self.fails = []

    def ok(self, what):
        self.oks.append(what)

    def fail(self, what, expected=None, found=None):
        self.fails.append(what)

    def expect(self, what, expected, found):
        (self.oks if str(expected) == str(found) else self.fails).append(what)


def run_check(cfg, outputs):
    c = FakeChecker({"mcpo": cfg})
    with mock.patch.object(mc, "ssh_output", side_effect=lambda host, cmd, **kw: outputs.pop(0)):
        mc._check(c)
    return c


served = json.dumps({"mcpServers": {"memory": {"command": "npx"}}})
c = run_check({}, ["1", "ghcr.io/open-webui/mcpo:main", served, "mcpo.c1.mydemo.lab"])
check("--check: a healthy install passes every check", c.fails == [] and len(c.oks) == 4)
c = run_check({}, ["0", "ghcr.io/open-webui/mcpo:main", served, "h"])
check("--check: no ready pod fails", any("ready mcpo pods" in f for f in c.fails))
c = run_check({}, ["1", "ghcr.io/open-webui/mcpo:other", served, "h"])
check("--check: an image other than the one the lab asked for fails",
      any("image" in f for f in c.fails))
c = run_check({"mcpo_servers": {"time": _TIME}}, ["1", "ghcr.io/open-webui/mcpo:main", served, "h"])
check("--check: serving other servers than the lab asked for fails",
      any("MCP servers" in f for f in c.fails))
c = run_check({}, ["1", "ghcr.io/open-webui/mcpo:main", "not json", "h"])
check("--check: a ConfigMap that is not valid JSON fails rather than raising",
      any("MCP servers" in f for f in c.fails))
c = run_check({}, ["1", "ghcr.io/open-webui/mcpo:main", served, ""])
check("--check: a missing ingress fails", any("ingress" in f for f in c.fails))

if failures:
    print("{} check(s) failed".format(len(failures)))
    sys.exit(1)
print("all mcpo checks passed")
