#!/usr/bin/env python3.11
# install_searxng, and open_webui's SearXNG wiring — see 132_searxng.sh.
import importlib.util
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


sx = _load("install_searxng")
owui = _load("install_open_webui")

import addon_common as ac  # noqa: E402

failures = []


def check(desc, cond):
    if not cond:
        failures.append(desc)
        print("FAIL:", desc)


def run_kubernetes(cfg, deployed=""):
    """setup_searxng_kubernetes() with `cfg`; returns (commands, stdin texts per command, printed, fqdn)."""
    calls, printed = [], []

    def fake_ssh(host, cmd, **kw):
        calls.append((cmd, kw.get("input_text") or ""))
        return mock.Mock(returncode=0, stdout="")

    with mock.patch.object(sx, "ssh_run", side_effect=fake_ssh), \
            mock.patch.object(sx, "ssh_output", return_value=deployed), \
            mock.patch("builtins.print", side_effect=lambda *a, **kw: printed.append(" ".join(str(x) for x in a))):
        fqdn = sx.setup_searxng_kubernetes("node1", "c1", "mydemo.lab", cfg, _TEMPLATES, ingress_class="traefik")
    return [c for c, _ in calls], [i for _, i in calls], printed, fqdn


def run_podman(cfg, deployed=""):
    """setup_searxng_podman() with `cfg`; returns (commands, stdin texts, printed, base_url)."""
    calls, printed = [], []

    def fake_ssh(host, cmd, **kw):
        calls.append((cmd, kw.get("input_text") or ""))
        return mock.Mock(returncode=0, stdout="")

    with mock.patch.object(sx, "ssh_run", side_effect=fake_ssh), \
            mock.patch.object(sx, "ssh_output", return_value=deployed), \
            mock.patch.object(sx, "ensure_podman", return_value=True), \
            mock.patch("builtins.print", side_effect=lambda *a, **kw: printed.append(" ".join(str(x) for x in a))):
        base_url = sx.setup_searxng_podman("node1.mydemo.lab", cfg)
    return [c for c, _ in calls], [i for _, i in calls], printed, base_url


def settings_of(stdins):
    """The settings.yml this install sent, parsed."""
    for text in stdins:
        if "use_default_settings" in text:
            return yaml.safe_load(text)
    return None


def manifests_of(stdins):
    """Every Kubernetes document this install applied, parsed."""
    docs = []
    for text in stdins:
        if "apiVersion" in text:
            docs.extend(d for d in yaml.safe_load_all(text) if d)
    return docs


def kind(docs, kind_name, name=None):
    """The first document of `kind_name` (and `name`), or None."""
    return next((d for d in docs
                 if d.get("kind") == kind_name and (name is None or d["metadata"]["name"] == name)), None)


# ── kubernetes mode, defaults ─────────────────────────────────────────────────
cmds, stdins, printed, fqdn = run_kubernetes({})
docs = manifests_of(stdins)
settings = settings_of(stdins)

check("kubernetes: returns the ingress hostname searxng.<kcluster>.<domain>", fqdn == "searxng.c1.mydemo.lab")
check("kubernetes: the namespace is created idempotently",
      any("kubectl create namespace searxng --dry-run=client" in c and "kubectl apply -f -" in c for c in cmds))
check("kubernetes: the settings reach the node on stdin, never on a command line",
      any("use_default_settings" in i for i in stdins)
      and not any("use_default_settings" in c for c in cmds))
check("kubernetes: the ConfigMap is created from that file, idempotently",
      any("create configmap searxng-settings --from-file=settings.yml=" in c and "--dry-run=client" in c
          for c in cmds))
check("kubernetes: one Deployment, one Service and one Ingress are applied",
      len([d for d in docs if d.get("kind") == "Deployment"]) == 1
      and kind(docs, "Service", "searxng") is not None and kind(docs, "Ingress") is not None)

dep = kind(docs, "Deployment", "searxng")
container = dep["spec"]["template"]["spec"]["containers"][0]
check("kubernetes: one replica of the upstream image's latest tag",
      dep["spec"]["replicas"] == 1 and container["image"] == "docker.io/searxng/searxng:latest")
check("kubernetes: the container port is the image's own 8080", container["ports"][0]["containerPort"] == 8080)
check("kubernetes: readiness is the instance's own /healthz",
      container["readinessProbe"]["httpGet"]["path"] == "/healthz")
check("kubernetes: the settings ConfigMap is mounted at /etc/searxng",
      any(m["mountPath"] == "/etc/searxng" for m in container["volumeMounts"])
      and any(v.get("configMap", {}).get("name") == "searxng-settings"
              for v in dep["spec"]["template"]["spec"]["volumes"]))
check("kubernetes: without searxng_storage_size the favicon cache is an emptyDir",
      any("emptyDir" in v for v in dep["spec"]["template"]["spec"]["volumes"])
      and kind(docs, "PersistentVolumeClaim") is None)

ing = kind(docs, "Ingress")
check("kubernetes: the ingress serves the hostname with the class the kcluster uses",
      ing["spec"]["rules"][0]["host"] == "searxng.c1.mydemo.lab"
      and ing["spec"]["ingressClassName"] == "traefik")
check("kubernetes: the ingress sends the hostname to the searxng Service",
      ing["spec"]["rules"][0]["http"]["paths"][0]["backend"]["service"] == {"name": "searxng",
                                                                            "port": {"number": 8080}})
check("kubernetes: the Service answers on searxng_port", kind(docs, "Service", "searxng")["spec"]["ports"][0]["port"] == 8080)
check("kubernetes: nothing of Valkey is deployed by default",
      kind(docs, "Deployment", "searxng-valkey") is None and "valkey" not in settings)
check("kubernetes: a first install does not restart the pods", not any("rollout restart" in c for c in cmds))
check("kubernetes: says where the JSON API answers", any("format=json" in p for p in printed))

# ── the settings this lab gets ────────────────────────────────────────────────
check("settings: a partial override on top of the image's own defaults", settings["use_default_settings"] is True)
check("settings: html and json are served, so both the UI and an API client work",
      settings["search"]["formats"] == ["html", "json"])
check("settings: the public URL is the ingress hostname",
      settings["server"]["base_url"] == "http://searxng.c1.mydemo.lab/")
check("settings: a secret key is generated, not the upstream placeholder",
      len(settings["server"]["secret_key"]) == 64 and settings["server"]["secret_key"] != "ultrasecretkey")
check("settings: the limiter, public_instance and image_proxy are off",
      settings["server"]["limiter"] is False and settings["server"]["public_instance"] is False
      and settings["server"]["image_proxy"] is False)
check("settings: the upstream defaults of safe_search, autocomplete, language and theme",
      settings["search"]["safe_search"] == 0 and settings["search"]["autocomplete"] == "duckduckgo"
      and settings["search"]["default_lang"] == "auto" and settings["ui"]["default_theme"] == "simple")
check("settings: /metrics stays closed until searxng_open_metrics sets a password",
      "open_metrics" not in settings["general"])

# ── every field set ───────────────────────────────────────────────────────────
cmds, stdins, printed, fqdn = run_kubernetes({
    "searxng_ns": "search", "searxng_shorthn": "find", "searxng_port": "9090", "searxng_replicas": "3",
    "searxng_image": "ghcr.io/searxng/searxng", "searxng_version": "2026.10.17-541c6c3cb",
    "searxng_instance_name": "Lab search", "searxng_secret": "deadbeef", "searxng_storage_size": "2Gi",
    "searxng_formats": ["html", "json", "rss"], "searxng_safe_search": "2", "searxng_autocomplete": "google",
    "searxng_default_lang": "en", "searxng_default_theme": "simple", "searxng_image_proxy": "true",
    "searxng_open_metrics": "metrics-pw", "searxng_base_url": "https://search.example.com/",
    "searxng_settings_extra": {"outgoing": {"request_timeout": 6.0}, "server": {"method": "POST"}},
})
docs, settings = manifests_of(stdins), settings_of(stdins)
dep = kind(docs, "Deployment", "searxng")
check("overrides: the hostname follows searxng_shorthn", fqdn == "find.c1.mydemo.lab")
check("overrides: namespace, replicas and image reach the Deployment",
      dep["metadata"]["namespace"] == "search" and dep["spec"]["replicas"] == 3
      and dep["spec"]["template"]["spec"]["containers"][0]["image"]
      == "ghcr.io/searxng/searxng:2026.10.17-541c6c3cb")
check("overrides: searxng_port is the Service's and the ingress backend's port",
      kind(docs, "Service", "searxng")["spec"]["ports"][0]["port"] == 9090
      and kind(docs, "Ingress")["spec"]["rules"][0]["http"]["paths"][0]["backend"]["service"]["port"]["number"]
      == 9090)
check("overrides: searxng_storage_size claims a volume and mounts it as the cache",
      kind(docs, "PersistentVolumeClaim", "searxng-cache")["spec"]["resources"]["requests"]["storage"] == "2Gi"
      and any(v.get("persistentVolumeClaim", {}).get("claimName") == "searxng-cache"
              for v in dep["spec"]["template"]["spec"]["volumes"]))
check("overrides: the namespace of every document follows searxng_ns",
      all(d["metadata"]["namespace"] == "search" for d in docs))
check("overrides: searxng_secret is used as it is, instead of a generated one",
      settings["server"]["secret_key"] == "deadbeef")
check("overrides: the search, UI and server fields reach settings.yml",
      settings["general"]["instance_name"] == "Lab search" and settings["general"]["open_metrics"] == "metrics-pw"
      and settings["search"]["formats"] == ["html", "json", "rss"]
      and settings["search"]["safe_search"] == 2 and settings["search"]["autocomplete"] == "google"
      and settings["search"]["default_lang"] == "en" and settings["server"]["image_proxy"] is True
      and settings["server"]["base_url"] == "https://search.example.com/")
check("overrides: searxng_settings_extra is merged over everything, keeping its siblings",
      settings["outgoing"]["request_timeout"] == 6.0 and settings["server"]["method"] == "POST"
      and settings["server"]["secret_key"] == "deadbeef")

# ── Valkey, which only the limiter and bot detection need ─────────────────────
for field in ("searxng_valkey", "searxng_limiter", "searxng_public_instance"):
    cmds, stdins, _, _ = run_kubernetes({field: "true"})
    docs, settings = manifests_of(stdins), settings_of(stdins)
    valkey = kind(docs, "Deployment", "searxng-valkey")
    check("valkey: {} 'true' deploys the companion and points the instance at it".format(field),
          valkey is not None
          and valkey["spec"]["template"]["spec"]["containers"][0]["image"] == "docker.io/valkey/valkey:9-alpine"
          and kind(docs, "Service", "searxng-valkey") is not None
          and settings["valkey"]["url"] == "valkey://searxng-valkey.searxng.svc.cluster.local:6379/0")

cmds, stdins, _, _ = run_kubernetes({"searxng_limiter": "true",
                                     "searxng_valkey_url": "valkey://valkey.mydemo.lab:6379/0"})
docs, settings = manifests_of(stdins), settings_of(stdins)
check("valkey: searxng_valkey_url is used instead, and no companion is deployed",
      settings["valkey"]["url"] == "valkey://valkey.mydemo.lab:6379/0"
      and kind(docs, "Deployment", "searxng-valkey") is None)
cmds, stdins, _, _ = run_kubernetes({"searxng_valkey_version": "8-alpine",
                                     "searxng_valkey_image": "registry.mydemo.lab/valkey",
                                     "searxng_valkey": "true"})
docs = manifests_of(stdins)
check("valkey: its image and tag are configurable",
      kind(docs, "Deployment", "searxng-valkey")["spec"]["template"]["spec"]["containers"][0]["image"]
      == "registry.mydemo.lab/valkey:8-alpine")

# ── engines ───────────────────────────────────────────────────────────────────
_, stdins, _, _ = run_kubernetes({"searxng_engines_remove": "google, bing"})
check("engines: searxng_engines_remove drops them from the defaults",
      settings_of(stdins)["use_default_settings"] == {"engines": {"remove": ["google", "bing"]}})
_, stdins, _, _ = run_kubernetes({"searxng_engines_keep_only": ["duckduckgo"]})
check("engines: searxng_engines_keep_only keeps only those",
      settings_of(stdins)["use_default_settings"] == {"engines": {"keep_only": ["duckduckgo"]}})

# ── a re-run keeps the secret of the instance it updates ──────────────────────
deployed = ("use_default_settings: true\nserver:\n  base_url: http://searxng.c1.mydemo.lab/\n"
            "  secret_key: 0123456789abcdef\n")
_, stdins, printed, _ = run_kubernetes({}, deployed=deployed)
check("re-run: the deployed secret key is reused, so sessions survive",
      settings_of(stdins)["server"]["secret_key"] == "0123456789abcdef")
cmds, _, printed, _ = run_kubernetes({}, deployed=deployed)
check("re-run: the pods are restarted, because the settings changed",
      any("kubectl -n searxng rollout restart deployment/searxng" in c for c in cmds))

# ── podman mode ───────────────────────────────────────────────────────────────
cmds, stdins, printed, base_url = run_podman({})
run = next((c for c in cmds if c.startswith("podman run -d --name searxng ")), "")
check("podman: the public URL is the node's own hostname and searxng_port",
      base_url == "http://node1.mydemo.lab:8080/" and settings_of(stdins)["server"]["base_url"] == base_url)
check("podman: the settings are written to /etc/searxng/settings.yml over stdin",
      any(c == "cat > /etc/searxng/settings.yml" for c in cmds)
      and any("use_default_settings" in i for i in stdins))
check("podman: the container is replaced, restarts always and publishes the port",
      any(c.startswith("podman rm -f searxng ") for c in cmds)
      and "--restart=always" in run and "-p 8080:8080" in run
      and run.endswith("docker.io/searxng/searxng:latest"))
check("podman: the configuration and the favicon cache are bind-mounted",
      "-v /etc/searxng:/etc/searxng:Z" in run and "-v /var/lib/searxng:/var/cache/searxng:Z" in run)
check("podman: a systemd unit makes it survive a reboot",
      any(c == "cat > /etc/systemd/system/searxng.service" for c in cmds)
      and any("systemctl enable searxng.service" in c for c in cmds))
check("podman: no Valkey and no podman network by default",
      not any("searxng-valkey" in c for c in cmds) and not any("podman network" in c for c in cmds))
check("podman: says the node itself has to allow the port", any("firewall" in p for p in printed))

cmds, stdins, _, _ = run_podman({"searxng_limiter": "true", "searxng_port": "8888",
                                 "searxng_data_dir": "/srv/searxng"})
run = next((c for c in cmds if c.startswith("podman run -d --name searxng ")), "")
valkey_run = next((c for c in cmds if c.startswith("podman run -d --name searxng-valkey ")), "")
check("podman: the limiter brings a companion Valkey on a podman network both containers join",
      any("podman network exists searxng || podman network create searxng" in c for c in cmds)
      and "--network searxng" in valkey_run and "--network searxng" in run
      and settings_of(stdins)["valkey"]["url"] == "valkey://searxng-valkey:6379/0")
check("podman: the Valkey container is persistent and survives a reboot",
      "-v searxng-valkey-data:/data" in valkey_run
      and any(c == "cat > /etc/systemd/system/searxng-valkey.service" for c in cmds))
check("podman: searxng_port and searxng_data_dir reach the container",
      "-p 8888:8080" in run and "-v /srv/searxng:/var/cache/searxng:Z" in run)

_, stdins, _, _ = run_podman({}, deployed="server:\n  secret_key: \"0123456789abcdef\"\n")
check("podman: a re-run reuses the secret key already on the node",
      settings_of(stdins)["server"]["secret_key"] == "0123456789abcdef")

# ── where the lab places it decides the mode ──────────────────────────────────
on_kcluster = {"kclusters": {"c1": {"addons": ["searxng"]}}, "nodes": {}}
on_node = {"kclusters": {"c1": {"addons": ["rancher"]}},
           "nodes": {"vm1.mydemo.lab": {"addons": [{"searxng": {"searxng_port": "9000"}}]}}}
check("mode: a kcluster listing searxng installs it on the cluster",
      sx._deployment(on_kcluster, {}) == "kubernetes")
check("mode: only a node listing searxng installs it with podman",
      sx._deployment(on_node, {}) == "podman")
check("mode: searxng_deployment overrides both ways",
      sx._deployment(on_kcluster, {"searxng_deployment": "podman"}) == "podman"
      and sx._deployment(on_node, {"searxng_deployment": "kubernetes"}) == "kubernetes")

# ── a list field takes a list or a string, with the same result ───────────────
check("fields: a comma-separated string and a list mean the same",
      sx._as_list("html, json") == sx._as_list(["html", "json"]) == ["html", "json"]
      and sx._as_list("") == [] and sx._as_list(None) == [])

# ── validation ────────────────────────────────────────────────────────────────


def errors(cfg):
    v = ac.Validator({"searxng": cfg})
    sx._validate(v)
    return v.errors


check("validate: an empty section and both deployments are accepted",
      errors({}) == [] and all(errors({"searxng_deployment": d}) == [] for d in sx._DEPLOYMENTS))
check("validate: an unknown deployment is rejected", len(errors({"searxng_deployment": "docker"})) == 1)
check("validate: an invalid namespace and a non-numeric port are rejected",
      len(errors({"searxng_ns": "Search_1"})) == 1 and len(errors({"searxng_port": "http"})) == 1)
check("validate: the booleans only take true/false",
      len(errors({"searxng_limiter": "yes"})) == 1 and len(errors({"searxng_valkey": "1"})) == 1
      and len(errors({"searxng_public_instance": "True"})) == 1
      and len(errors({"searxng_image_proxy": "off"})) == 1)
check("validate: the two engine lists are mutually exclusive",
      len(errors({"searxng_engines_remove": "google", "searxng_engines_keep_only": "bing"})) == 1)
check("validate: an unknown result format is rejected, a known one is not",
      len(errors({"searxng_formats": "html, xml"})) == 1 and errors({"searxng_formats": ["html", "rss"]}) == [])
check("validate: searxng_settings_extra must be a mapping",
      len(errors({"searxng_settings_extra": "server.method: POST"})) == 1
      and errors({"searxng_settings_extra": {"server": {"method": "POST"}}}) == [])

# ── open_webui's own web search ───────────────────────────────────────────────
check("open_webui: no searxng section leaves web search off",
      owui.searxng_query_url({"ollama": {}}) == "")
check("open_webui: a searxng section on the kcluster gives the in-cluster Service URL",
      owui.searxng_query_url({"searxng": {}})
      == "http://searxng.searxng.svc.cluster.local:8080/search?q=<query>")
check("open_webui: searxng_ns and searxng_port reach that URL",
      owui.searxng_query_url({"searxng": {"searxng_ns": "search", "searxng_port": "9090"}})
      == "http://searxng.search.svc.cluster.local:9090/search?q=<query>")
check("open_webui: a podman-mode searxng is not guessed at, it is set explicitly",
      owui.searxng_query_url({"searxng": {"searxng_deployment": "podman"}}) == ""
      and owui.searxng_query_url({"searxng": {"searxng_deployment": "podman"}},
                                 "http://node1.mydemo.lab:8080/search?q=<query>")
      == "http://node1.mydemo.lab:8080/search?q=<query>")
check("open_webui: an explicit URL wins over the derived one",
      owui.searxng_query_url({"searxng": {}}, "http://elsewhere.lab/search?q=<query>")
      == "http://elsewhere.lab/search?q=<query>")

owui_cmds = []
with mock.patch.object(owui, "ssh_run", side_effect=lambda host, cmd, **kw: owui_cmds.append(cmd)), \
        mock.patch("builtins.print"):
    owui.setup_open_webui("node1", "c1", "mydemo.lab", {"searxng": {}})
helm = next(c for c in owui_cmds if c.startswith("helm upgrade"))
check("open_webui: web search, the engine and the query URL reach the chart as environment variables",
      "--set extraEnvVars[0].name=ENABLE_WEB_SEARCH" in helm and "--set extraEnvVars[0].value=true" in helm
      and "--set extraEnvVars[1].name=WEB_SEARCH_ENGINE" in helm
      and "--set extraEnvVars[1].value=searxng" in helm
      and "--set extraEnvVars[2].name=SEARXNG_QUERY_URL" in helm
      and "'http://searxng.searxng.svc.cluster.local:8080/search?q=<query>'" in helm)

owui_cmds = []
with mock.patch.object(owui, "ssh_run", side_effect=lambda host, cmd, **kw: owui_cmds.append(cmd)), \
        mock.patch("builtins.print"):
    owui.setup_open_webui("node1", "c1", "mydemo.lab", {})
helm = next(c for c in owui_cmds if c.startswith("helm upgrade"))
check("open_webui: a lab without searxng gets no web-search environment at all",
      "extraEnvVars" not in helm and "WEB_SEARCH" not in helm)

if failures:
    print("{} check(s) failed".format(len(failures)))
    sys.exit(1)
print("all searxng checks passed")
