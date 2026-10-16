#!/usr/bin/env python3.11
# Installing an add-on's product from the SUSE Application Collection — see 98_appcollection_source.sh.
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import mock

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "libs"))
sys.path.insert(0, str(_REPO / "scripts"))

import appco_common as appco  # noqa: E402
import apps  # noqa: E402
import migrations  # noqa: E402

failures = []


def check(desc, cond):
    if not cond:
        failures.append(desc)
        print("FAIL:", desc)


# ── which source an add-on uses ──────────────────────────────────────────────
check("no field means the upstream chart, so a lab without an account is unchanged",
      appco.chart_source({}, "lh") == appco.SOURCE_UPSTREAM and appco.from_appcollection({}, "lh") is False)
check("an empty value is the upstream chart too", appco.chart_source({"lh_source": ""}, "lh") == "upstream")
check("the Application Collection is opt-in, by name",
      appco.from_appcollection({"lh_source": "appcollection"}, "lh") is True)
check("the value is read case- and space-insensitively",
      appco.from_appcollection({"lh_source": " AppCollection "}, "lh") is True)
try:
    appco.chart_source({"lh_source": "wherever"}, "lh")
    check("an unknown source is refused, not guessed at", False)
except SystemExit:
    pass

# ── the chart reference ──────────────────────────────────────────────────────
check("a chart is addressed by its Collection slug",
      appco.appcollection_chart("suse-storage") == "oci://dp.apps.rancher.io/charts/suse-storage")
check("a different registry can be used",
      appco.appcollection_chart("suse-storage", "registry.example.com/")
      == "oci://registry.example.com/charts/suse-storage")

# ── what it takes to install one ─────────────────────────────────────────────
calls = {}
with mock.patch.object(appco, "pull_secret", side_effect=lambda *a: calls.setdefault("secret", a)), \
     mock.patch.object(appco, "helm_login", side_effect=lambda *a: calls.setdefault("login", a)):
    chart, flags = appco.prepare_appcollection_chart("n1.lab", "suse-storage", "longhorn-system", "user", "token")
check("the chart is the Collection's OCI chart", chart == "oci://dp.apps.rancher.io/charts/suse-storage")
check("the pods are given the pull secret",
      "--set global.imagePullSecrets[0]=application-collection" in flags.replace("'", ""))
check("the pull secret is created in the chart's own namespace",
      calls["secret"] == ("n1.lab", "application-collection", "dp.apps.rancher.io", "user", "token",
                          "longhorn-system"))
check("helm is logged in to the registry",
      calls["login"] == ("n1.lab", "dp.apps.rancher.io", "user", "token"))

# ── longhorn, the first add-on to offer it ───────────────────────────────────
lh = apps.load_addon_module("install_longhorn", str(_REPO / "scripts"))
check("longhorn knows its slug in the Collection", lh.APPCOLLECTION_CHART == "suse-storage")

commands = []
with mock.patch.object(lh, "ssh_run", side_effect=lambda h, cmd, **k: commands.append(cmd)):
    lh.setup_lh("n1", "clu", "lab", lh_version="1.12.1")
helm = [c for c in commands if "helm upgrade" in c][0]
check("by default it installs the upstream chart", "longhorn/longhorn" in helm and "oci://" not in helm)
check("...with no pull secret", "imagePullSecrets" not in helm)

commands = []
with mock.patch.object(lh, "ssh_run", side_effect=lambda h, cmd, **k: commands.append(cmd)):
    lh.setup_lh("n1", "clu", "lab", lh_version="1.12.1",
                chart="oci://dp.apps.rancher.io/charts/suse-storage",
                extra_set=" --set global.imagePullSecrets[0]=application-collection")
helm = [c for c in commands if "helm upgrade" in c][0]
check("given the Collection's chart it installs that instead",
      "oci://dp.apps.rancher.io/charts/suse-storage" in helm and "longhorn/longhorn" not in helm)
check("...and passes the pull secret to the chart",
      "global.imagePullSecrets[0]=application-collection" in helm)
check("the release name and the lab's own settings are unchanged",
      "helm upgrade -i longhorn " in helm and "--set ingress.host=longhorn.clu.lab" in helm
      and "--version 1.12.1" in helm)

# ── validation ───────────────────────────────────────────────────────────────
def _validate_out(section):
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
        json.dump({"common": {}, "nodes": {}, "longhorn": section}, fh)
    r = subprocess.run([sys.executable, str(_REPO / "scripts" / "install_longhorn"), "--validate", fh.name],
                       stdout=subprocess.PIPE, universal_newlines=True)
    Path(fh.name).unlink()
    return r.returncode, r.stdout


# With the Collection as the default, a lab that says nothing needs an account: this container has no credential
# store, so the add-on must say which fields it wants rather than pass silently.
rc, out = _validate_out({})
check("a lab that says nothing about the source asks for the account the default needs",
      rc != 0 and "lh_appcollection_user" in out)
rc, out = _validate_out({"lh_source": "nonsense"})
check("an unknown source is an error naming the valid ones",
      rc != 0 and "lh_source 'nonsense' is invalid" in out and "upstream, appcollection" in out)
rc, out = _validate_out({"lh_source": "upstream", "lh_version": "1.12.1"})
check("the upstream source needs no account", rc == 0 and out.strip() == "")

# ── the schema says so ──────────────────────────────────────────────────────
schema = json.loads(subprocess.run([sys.executable, str(_REPO / "scripts" / "install_longhorn"),
                                    "--schema", "json"], stdout=subprocess.PIPE,
                                   universal_newlines=True).stdout)
names = [f["name"] for f in schema["fields"]]
for field in ("lh_source", "lh_appcollection_user", "lh_appcollection_password", "lh_appcollection_account"):
    check("the schema declares {}".format(field), field in names)
# Not the exact version: a later change raises it again. What must hold is that this change was recorded.
record = json.loads((_REPO / "schemas" / "compatibility.json").read_text())["schemas"]
check("the schema change is recorded",
      any("lh_source" in e.get("changes", "") for e in record.get("addon:longhorn", [])))
source_field = [f for f in schema["fields"] if f["name"] == "lh_source"][0]
check("lh_source is documented as optional", not source_field.get("required"))


# ── the add-ons whose product the Collection carries ────────────────────────
# Each one's chart was checked against the --set flags it passes (2026-10-15) before its default was flipped.
FLIPPED = {"argocd": "argo-cd", "coredns": "coredns", "fluentd": "fluentd", "harbor": "harbor",
           "ollama": "ollama", "open_webui": "open-webui", "traefik": "traefik-proxy",
           "milvus": "milvus", "longhorn": "suse-storage"}
OPT_IN = {}
PREFIX = {"longhorn": "lh"}

for addon, slug in sorted(list(FLIPPED.items()) + list(OPT_IN.items())):
    prefix = PREFIX.get(addon, addon)
    mod = apps.load_addon_module("install_" + addon, str(_REPO / "scripts"))
    check("{}: knows its slug in the Collection".format(addon), mod.APPCOLLECTION_CHART == slug)

    schema = json.loads(subprocess.run([sys.executable, str(_REPO / "scripts" / ("install_" + addon)),
                                        "--schema", "json"], stdout=subprocess.PIPE,
                                       universal_newlines=True).stdout)
    names = [f["name"] for f in schema["fields"]]
    for suffix in ("_source", "_appcollection_user", "_appcollection_password", "_appcollection_account"):
        check("{}: declares {}{}".format(addon, prefix, suffix), prefix + suffix in names)
    check("{}: the schema change is recorded".format(addon),
          any("{}_source".format(prefix) in e.get("changes", "")
              for e in record.get("addon:" + addon, [])))

    default = appco.chart_source({}, prefix,
                                 appco.SOURCE_APPCOLLECTION if addon in FLIPPED else appco.SOURCE_UPSTREAM)
    check("{}: default source is {}".format(addon, "the Collection" if addon in FLIPPED else "upstream"),
          default == (appco.SOURCE_APPCOLLECTION if addon in FLIPPED else appco.SOURCE_UPSTREAM))
    check("{}: a lab can always ask for the other one".format(addon),
          appco.chart_source({prefix + "_source": "upstream"}, prefix, appco.SOURCE_APPCOLLECTION) == "upstream"
          and appco.chart_source({prefix + "_source": "appcollection"}, prefix) == "appcollection")

    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
        json.dump({"common": {}, "nodes": {}, addon: {prefix + "_source": "nope"}}, fh)
    out = subprocess.run([sys.executable, str(_REPO / "scripts" / ("install_" + addon)), "--validate", fh.name],
                         stdout=subprocess.PIPE, universal_newlines=True)
    check("{}: an unknown source is refused".format(addon),
          out.returncode != 0 and "{}_source 'nope' is invalid".format(prefix) in out.stdout)
    Path(fh.name).unlink()


# ── milvus configures standalone mode per chart ─────────────────────────────
# zilliztech/milvus-helm puts MinIO in standalone mode and turns Pulsar off. The Collection's chart has neither
# key: its standalone deployment carries its own message queue, so the single-replica subcharts are what to set.
mv = apps.load_addon_module("install_milvus", str(_REPO / "scripts"))
for from_collection, expected, absent in ((False, {"minio.mode=standalone", "pulsarv3.enabled=false"},
                                           {"minio.replicas=1", "kafka.enabled=false"}),
                                          (True, {"minio.replicas=1", "kafka.enabled=false"},
                                           {"minio.mode=standalone", "pulsarv3.enabled=false"})):
    seen = []
    with mock.patch.object(mv, "ssh_run", side_effect=lambda h, c, **k: seen.append(c)):
        mv.setup_milvus("n1", milvus_rel=appco.oci_charts() if from_collection else None,
                        from_appcollection=from_collection)
    helm = next(c for c in seen if "helm upgrade" in c)
    flags = {f.split()[0] for f in helm.split("--set ")[1:]}
    which = "the Collection's chart" if from_collection else "the upstream chart"
    check("milvus: standalone mode is configured for {}".format(which), expected <= flags)
    check("milvus: the other chart's keys are not passed to {}".format(which), not (absent & flags))
    check("milvus: cluster.enabled is set once, not twice", len(helm.split("--set cluster.enabled")) == 2)

# ── a flipped add-on is breaking, so its migration keeps an old lab working ──
for addon in sorted(FLIPPED):
    prefix = PREFIX.get(addon, addon)
    entries = [e for e in record.get("addon:" + addon, []) if e.get("version") == "2.0"]
    check("{}: 2.0 is recorded as breaking with its migration".format(addon),
          len(entries) == 1 and entries[0].get("breaking") is True
          and entries[0].get("migration") == "{}_2_0".format(addon))
    check("{}: the migration step exists".format(addon), "{}_2_0".format(addon) in migrations.MIGRATIONS)

    step = migrations.MIGRATIONS["{}_2_0".format(addon)]
    # a lab that said nothing meant the upstream chart, and keeps it
    lab = {"kclusters": {"c1": {"addons": [addon]}}, "nodes": {}}
    changes = step(lab)
    check("{}: the migration writes the source the lab meant".format(addon),
          changes and lab[addon]["{}_source".format(prefix)] == "upstream")
    check("{}: running it again changes nothing".format(addon), step(lab) == [])
    # a lab that names a source keeps it, and one that does not use the add-on is untouched
    named = {"kclusters": {"c1": {"addons": [addon]}}, "nodes": {},
             addon: {"{}_source".format(prefix): "appcollection"}}
    check("{}: a lab that already names a source is left alone".format(addon), step(named) == [])
    unused = {"kclusters": {"c1": {"addons": ["rancher"]}}, "nodes": {}}
    check("{}: a lab that does not use it is untouched".format(addon),
          step(unused) == [] and addon not in unused)

if failures:
    print("{} check(s) failed".format(len(failures)))
    sys.exit(1)
print("all Application Collection source checks passed")
