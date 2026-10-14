#!/usr/bin/env python3
# Part of lab-in-a-box — migration of configuration files across breaking schema versions.
# Author/s: Raul Mahiques
# License: GPLv3
"""
libs/migrations.py: the steps that move a configuration file across a breaking schema version, and applying them.

MIGRATIONS maps a step name to a function step(data) -> list of the changes it made, as sentences ([] when there is
nothing to change). schemas/compatibility.json's breaking versions name these steps. `data` is the parsed file: a dict
for a lab definition, harvester-cluster.json or a credentials file, a list of lines for a shell configuration file.
A step changes `data` in place and only what is still in its old form, so a second run changes nothing.

A file records the versions it was written for: a lab definition in its top-level "schema_versions" object
({"lab": "1.0", "addon:<name>": "M.m", ...}), a shell configuration file in its "# Schema version:" line, a JSON or
YAML configuration file in its "schema_version" key. A schema the file does not record counts as version 1.0.
"""

import copy
import json
import re
from pathlib import Path

import apps
from lab_creation import die

_INSTALLED_COMPATIBILITY = Path("/usr/share/lab_creation/schemas/compatibility.json")
_REPO_COMPATIBILITY = Path(__file__).resolve().parents[1] / "schemas" / "compatibility.json"
_VERSION_LINE = re.compile(r"^# Schema version:\s*(\S+)\s*$")
LAB_VERSIONS_KEY = "schema_versions"

RANCHER_STABLE_URL = "https://releases.rancher.com/server-charts/stable"


def _rancher_configs(definition: dict) -> list:
    """(config, inherited) for the "rancher" section and each addons[] override of it, inherited being the section."""
    section = definition.get("rancher") if isinstance(definition.get("rancher"), dict) else {}
    out = [(section, {})] if "rancher" in definition else []
    for owner in list((definition.get("kclusters") or {}).values()) + list((definition.get("nodes") or {}).values()):
        for entry in (owner or {}).get("addons") or []:
            if isinstance(entry, dict) and isinstance(entry.get("rancher"), dict):
                out.append((entry["rancher"], section))
    return out


def linkerd_2_0(definition: dict) -> list:
    """
    addon:linkerd 2.0: the charts come from the edge channel (alias linkerd-edge, https://helm.linkerd.io/edge). Removes
    linkerd_repo_url and linkerd_rel where they name the stable channel, and a linkerd_version that is no edge chart
    version (YYYY.MM.N).
    """
    changes = []
    for cfg in _section_configs(definition, "linkerd"):
        if "helm.linkerd.io/stable" in str(cfg.get("linkerd_repo_url", "")):
            changes.append("linkerd_repo_url removed (stable channel; linkerd now installs from the edge channel)")
            del cfg["linkerd_repo_url"]
        if cfg.get("linkerd_rel") == "linkerd":
            changes.append("linkerd_rel removed (the edge repo is added as linkerd-edge)")
            del cfg["linkerd_rel"]
        if cfg.get("linkerd_version") and not re.match(r"^20[0-9]{2}\.", str(cfg["linkerd_version"])):
            changes.append("linkerd_version '{}' removed (not an edge chart version)".format(cfg.pop("linkerd_version")))
    return changes


def postgresql_2_0(definition: dict) -> list:
    """addon:postgresql 2.0: Kubernetes mode runs the SUSE postgres image instead of the Bitnami chart; removes
    postgresql_rel, postgresql_repo_url and postgresql_version (a Bitnami chart version)."""
    return _drop_keys(definition, "postgresql", ("postgresql_rel", "postgresql_repo_url", "postgresql_version"),
                      "Kubernetes mode runs registry.suse.com/suse/postgres:<postgresql_pg_version>")


def rancher_2_0(definition: dict) -> list:
    """
    addon:rancher 2.0: an unset rancher_repo_url selects Rancher Prime instead of the stable repo, and an unset
    rancher_helm_chart is rancher-prime/rancher instead of none. Where rancher_rel is set, sets rancher_repo_url to
    the stable repo and rancher_helm_chart to <rancher_rel>/rancher when they are unset.
    """
    changes = []
    for cfg, inherited in _rancher_configs(definition):
        effective = dict(inherited, **cfg)
        if not effective.get("rancher_rel"):
            continue
        if not effective.get("rancher_repo_url"):
            cfg["rancher_repo_url"] = RANCHER_STABLE_URL
            changes.append("rancher_repo_url set to {}".format(RANCHER_STABLE_URL))
        if not effective.get("rancher_helm_chart"):
            cfg["rancher_helm_chart"] = "{}/rancher".format(effective["rancher_rel"])
            changes.append("rancher_helm_chart set to {}".format(cfg["rancher_helm_chart"]))
    return changes


def _section_configs(definition: dict, name: str) -> list:
    """The "<name>" section and each addons[] override of it."""
    out = [definition[name]] if isinstance(definition.get(name), dict) else []
    for owner in list((definition.get("kclusters") or {}).values()) + list((definition.get("nodes") or {}).values()):
        for entry in (owner or {}).get("addons") or []:
            if isinstance(entry, dict) and isinstance(entry.get(name), dict):
                out.append(entry[name])
    return out


def _drop_keys(definition: dict, name: str, keys: tuple, reason: str) -> list:
    """Remove `keys` from the "<name>" section and its addons[] overrides; one change line per removed key."""
    changes = []
    for cfg in _section_configs(definition, name):
        for key in keys:
            if key in cfg:
                del cfg[key]
                changes.append("{} removed ({})".format(key, reason))
    return changes


def appcollection_2_0(definition: dict) -> list:
    """addon:appcollection 2.0: charts come from the OCI registry appcollection_registry; removes appcollection_rel and
    appcollection_repo_url."""
    return _drop_keys(definition, "appcollection", ("appcollection_rel", "appcollection_repo_url"),
                      "charts come from appcollection_registry")


def keycloak_2_0(definition: dict) -> list:
    """
    addon:keycloak 2.0: the chart is codecentric/keycloakx instead of bitnami/keycloak. Removes keycloak_rel and
    keycloak_repo_url where they name Bitnami, and keycloak_version there (a Bitnami chart version).
    """
    changes = []
    for cfg in _section_configs(definition, "keycloak"):
        if cfg.get("keycloak_rel") == "bitnami" or "bitnami" in str(cfg.get("keycloak_repo_url", "")):
            for key in ("keycloak_rel", "keycloak_repo_url", "keycloak_version"):
                if key in cfg:
                    del cfg[key]
                    changes.append("{} removed (Bitnami chart; keycloak now installs codecentric/keycloakx)".format(key))
    return changes


def complianceascode_2_0(definition: dict) -> list:
    """
    addon:complianceascode 2.0: installs Rancher's compliance operator (rancher-compliance / rancher-cis-benchmark) from
    https://charts.rancher.io instead of the ComplianceAsCode operator. Removes complianceascode_ns (the namespace is the
    chart's own), the old repo keys and version, and a complianceascode_profile that is no ClusterScanProfile name.
    """
    changes = _drop_keys(definition, "complianceascode", ("complianceascode_ns",), "the namespace is the chart's own")
    for cfg in _section_configs(definition, "complianceascode"):
        if cfg.get("complianceascode_rel") == "compliance-operator":
            changes.append("complianceascode_rel removed (Rancher's repo is added as rancher-charts)")
            del cfg["complianceascode_rel"]
        if "openshift.github.io" in str(cfg.get("complianceascode_repo_url", "")):
            changes.append("complianceascode_repo_url removed (charts come from https://charts.rancher.io)")
            del cfg["complianceascode_repo_url"]
        if cfg.get("complianceascode_version") and "+up" not in str(cfg["complianceascode_version"]):
            changes.append("complianceascode_version '{}' removed (not a Rancher chart version)".format(
                cfg.pop("complianceascode_version")))
        if cfg.get("complianceascode_profile") and not str(cfg["complianceascode_profile"]).endswith("-profile"):
            changes.append("complianceascode_profile '{}' removed (the operator's default profile is used)".format(
                cfg.pop("complianceascode_profile")))
    return changes


def kagent_2_0(definition: dict) -> list:
    """addon:kagent 2.0: the charts come from the OCI registry kagent_registry; removes kagent_rel and kagent_repo_url."""
    return _drop_keys(definition, "kagent", ("kagent_rel", "kagent_repo_url"), "kagent installs from kagent_registry")


def _litellm_2_0(name: str):
    def step(definition: dict) -> list:
        return _drop_keys(definition, name, ("{}_rel".format(name), "{}_repo_url".format(name)),
                          "the proxy installs from {}_chart".format(name))
    step.__doc__ = ("addon:{0} 2.0: the LiteLLM proxy installs from the OCI chart {0}_chart; removes {0}_rel and "
                    "{0}_repo_url.".format(name))
    return step


def trento_2_0(definition: dict) -> list:
    """
    addon:trento 2.0: installs from the OCI chart trento_chart, removing trento_rel and trento_repo_url. trento_admin is
    the admin user name; an e-mail address there (the 1.x default was admin@lab.local) is removed.
    """
    changes = _drop_keys(definition, "trento", ("trento_rel", "trento_repo_url"), "Trento installs from trento_chart")
    for cfg in _section_configs(definition, "trento"):
        if "@" in str(cfg.get("trento_admin", "")):
            changes.append("trento_admin '{}' removed (it is now the admin user name, default admin)".format(cfg.pop("trento_admin")))
    return changes


MIGRATIONS = {
    "anthropic_2_0": _litellm_2_0("anthropic"),
    "appcollection_2_0": appcollection_2_0,
    "complianceascode_2_0": complianceascode_2_0,
    "gemini_2_0": _litellm_2_0("gemini"),
    "kagent_2_0": kagent_2_0,
    "keycloak_2_0": keycloak_2_0,
    "kimi_2_0": _litellm_2_0("kimi"),
    "linkerd_2_0": linkerd_2_0,
    "openai_2_0": _litellm_2_0("openai"),
    "postgresql_2_0": postgresql_2_0,
    "rancher_2_0": rancher_2_0,
    "trento_2_0": trento_2_0,
}


def load_record(path: Path = None) -> dict:
    """Schema name -> its entries in schemas/compatibility.json (the repository's, else the installed one)."""
    if path is None:
        path = _REPO_COMPATIBILITY if _REPO_COMPATIBILITY.is_file() else _INSTALLED_COMPATIBILITY
    return json.loads(Path(path).read_text())["schemas"]


def _version_key(version: str) -> tuple:
    return tuple(int(p) for p in version.split("."))


def schemas_of(kind: str, data) -> list:
    """The schema names file `data` of kind `kind` ("lab" or a config:<file> schema name) is written against."""
    if kind != "lab":
        return [kind]
    names = set(apps.collect_addon_names(data)) | {k for k in data if isinstance(data.get(k), dict)}
    return ["lab"] + sorted("addon:" + n for n in names - {"common", "nodes", "kclusters", LAB_VERSIONS_KEY})


def recorded_versions(kind: str, data) -> dict:
    """Schema name -> the version file `data` of kind `kind` records for it; schemas it does not record are left out."""
    if kind == "lab":
        return dict(data.get(LAB_VERSIONS_KEY) or {})
    if isinstance(data, list):
        found = next((m.group(1) for m in map(_VERSION_LINE.match, data) if m), None)
        return {kind: found} if found else {}
    return {kind: data["schema_version"]} if data.get("schema_version") else {}


def pending(kind: str, data, record: dict) -> list:
    """(schema, version, step name) of every migration file `data` still needs, oldest version first per schema."""
    recorded = recorded_versions(kind, data)
    out = []
    for name in schemas_of(kind, data):
        have = _version_key(recorded.get(name, "1.0"))
        for entry in record.get(name, []):
            if entry.get("migration") and _version_key(entry["version"]) > have:
                out.append((name, entry["version"], entry["migration"]))
    return out


def stamp(kind: str, data, record: dict):
    """Record in file `data` of kind `kind` the latest version of every schema it is written against."""
    latest = {name: record[name][-1]["version"] for name in schemas_of(kind, data) if record.get(name)}
    if kind == "lab":
        data[LAB_VERSIONS_KEY] = latest
    elif isinstance(data, list):
        line = "# Schema version: {}".format(latest[kind])
        at = next((i for i, text in enumerate(data) if _VERSION_LINE.match(text)), None)
        if at is None:
            data.insert(0, line)
        else:
            data[at] = line
    else:
        data["schema_version"] = latest[kind]


def migrate(kind: str, data, record: dict) -> list:
    """Apply every pending migration to file `data` of kind `kind`; '<schema> <version>: <change>' per change made."""
    changes = []
    for name, version, step in pending(kind, data, record):
        changes += ["{} {}: {}".format(name, version, c) for c in MIGRATIONS[step](data)]
    return changes


def needed_changes(kind: str, data, record: dict) -> list:
    """The changes migrate() would make to file `data` of kind `kind`, leaving `data` unchanged."""
    return migrate(kind, copy.deepcopy(data), record)


def require_current(definition: dict, path: str) -> None:
    """Die when lab definition `definition`, read from `path`, still needs a migration, naming each change."""
    record = load_record()
    changes = needed_changes("lab", definition, record)
    if not changes:
        return
    current = copy.deepcopy(definition)
    stamp("lab", current, record)
    die("{} is written for older schema versions and needs these changes:\n  {}\n"
        "Run: migrate_config.py {}   (writes the migrated copy next to it)\n"
        "If it is written for the current versions, add to it: \"{}\": {}".format(
            path, "\n  ".join(changes), path, LAB_VERSIONS_KEY, json.dumps(current[LAB_VERSIONS_KEY])))
