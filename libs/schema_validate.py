#!/usr/bin/env python3.11
# Part of lab-in-a-box — schema-driven value validation
# Author/s: Raul Mahiques
# License: GPLv3

"""
libs/schema_validate.py: check a lab definition's values against the schema that declares them.

A schema field declares what its value may be — "type", and optionally "enum", "pattern", "min" and "max" (see
libs/lab_schema_base.py for the base sections, and each add-on's own "JSON section:" block). This module is the one
place that reads those declarations and says what is wrong with a value, so the preflight (libs/lab_creation.py), each
add-on's --validate (libs/addon_common.py) and the web UI all report the same thing.

    value_finding(field, value)                -> Finding or None, for one field's value
    section_findings(fields, cfg, where)       -> list of Finding, for one section's values
    definition_findings(definition, base)      -> list of Finding, for common / nodes / kclusters / pxe
    addon_findings(definition, section, fields)-> list of Finding, for one add-on's section and per-node overrides

A Finding is (severity, where, message): severity is "error" or "warning", where is the dotted path of the value
("nodes.node1.myip"), message says what is wrong with it. Nothing here reads a file or the network.

Only values that are present are checked: an absent or empty value is the caller's business (required-ness is checked
by the preflight itself). A value written as a "??name" placeholder is never checked — it is resolved later, so its
literal text is not what reaches the field.
"""

__version__ = "__LABVERSION__"

import collections
import re

Finding = collections.namedtuple("Finding", "severity where message")

# A value whose literal text is a placeholder for something resolved later, never the value itself.
PLACEHOLDER_PREFIX = "??"

# What a boolean field accepts, and what the scripts read as true (see setup_vm.py's overlay flag).
_TRUE = ("1", "true", "yes")
_FALSE = ("0", "false", "no")


def _absent(value) -> bool:
    """Whether `value` counts as unset: None, an empty string, an empty list or an empty mapping."""
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip() == ""
    if isinstance(value, (list, tuple, dict)):
        return len(value) == 0
    return False


def is_placeholder(value) -> bool:
    """Whether `value` is a "??name" placeholder, resolved after validation and therefore never checked here."""
    return isinstance(value, str) and value.strip().startswith(PLACEHOLDER_PREFIX)


def _as_int(value):
    """`value` as an int when it is one or spells one ("8192"), else None. A bool is not an int here."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def enum_values(field: dict) -> list:
    """
    A field's declared values. An "enum" entry is the value itself, or a {"value", "label"} mapping when the web UI
    labels it (config_method's "" entry, for one).
    """
    out = []
    for entry in field.get("enum") or []:
        out.append(entry.get("value") if isinstance(entry, dict) else entry)
    return out


def _enum_finding(field: dict, value, where: str):
    """The finding for `value` against the field's declared values, or None."""
    values = enum_values(field)
    if not values or value in values or str(value) in [str(v) for v in values]:
        return None
    return Finding("error", where, "'{}' is not one of {}".format(
        value, ", ".join(str(v) for v in values if str(v) != "")))


def _number_finding(field: dict, value, where: str, kind: str):
    """The finding for an integer- or port-typed `value`: not a number, or outside the declared bounds."""
    n = _as_int(value)
    if n is None:
        return Finding("error", where, "'{}' is not {}".format(value, "a whole number" if kind == "integer" else "a port number"))
    low = field.get("min", 1 if kind == "port" else None)
    high = field.get("max", 65535 if kind == "port" else None)
    if low is not None and n < low:
        return Finding("error", where, "{} is below the minimum {}".format(n, low))
    if high is not None and n > high:
        return Finding("error", where, "{} is above the maximum {}".format(n, high))
    return None


def _boolean_finding(field: dict, value, where: str):
    """
    The finding for a boolean-typed `value`. A quoted boolean is a warning, not an error: it is accepted where the
    scripts compare the text ("true"/"yes"/"1"), but a field read with plain truthiness takes any non-empty string,
    including "false", as true.
    """
    if isinstance(value, bool):
        return None
    text = str(value).strip().lower()
    if text in _TRUE:
        return Finding("warning", where, "'{}' is a quoted boolean; write it unquoted as true".format(value))
    if text in _FALSE:
        return Finding("warning", where, "'{}' is a quoted boolean and some fields read any non-empty value as "
                                         "true; write it unquoted as false, or leave it out".format(value))
    return Finding("error", where, "'{}' is not a boolean (true or false)".format(value))


def value_finding(field: dict, value, where: str = ""):
    """
    The one Finding for `value` against schema field `field`, or None when the value satisfies it. `where` is the
    dotted path reported in the finding. An absent value and a "??name" placeholder are never a finding.
    """
    where = where or field.get("name", "")
    if _absent(value) or is_placeholder(value):
        return None
    kind = field.get("type") or "string"
    if kind in ("integer", "port"):
        return _number_finding(field, value, where, kind)
    if kind == "boolean":
        return _boolean_finding(field, value, where)
    if kind == "array":
        if not isinstance(value, (list, tuple)):
            return Finding("error", where, "'{}' is not a list".format(value))
        return None
    if kind == "object":
        if not isinstance(value, dict):
            return Finding("error", where, "'{}' is not a mapping of name to value".format(value))
        return None
    # A text kind ("string" and the kinds inferred from a field's name: version, url, namespace, password) is NOT
    # checked for being a list or a mapping. An add-on documents a structured field as [{...}] in its description
    # while its declared type stays the inferred "string" — smlm_config_channels and smlm_activation_keys, for two —
    # so a list there is correct and only the declarations below say anything a value can be checked against.
    found = _enum_finding(field, value, where)
    if found:
        return found
    pattern = field.get("pattern")
    if pattern and not re.match(pattern, str(value)):
        return Finding("error", where, "'{}' does not match the form this field takes ({})".format(value, pattern))
    return None


def section_findings(fields, cfg, where: str, skip=()) -> list:
    """
    Every Finding of mapping `cfg` against schema `fields`, each reported under "<where>.<field>". Keys `cfg` holds
    that `fields` does not declare are left alone: an add-on reads keys of its own, and a lab may carry keys this
    version does not know. `skip` names the fields the caller checks itself, so one bad value is reported once.
    """
    out = []
    by_name = {f.get("name"): f for f in (fields or []) if isinstance(f, dict) and f.get("name")}
    for name, field in by_name.items():
        if name not in (cfg or {}) or name in skip:
            continue
        found = value_finding(field, cfg[name], "{}.{}".format(where, name) if where else name)
        if found:
            out.append(found)
    return out


def _section_fields(section) -> list:
    """The flat field list of a base-schema section, whose fields may be nested in groups."""
    out = []

    def walk(node):
        if isinstance(node, list):
            for item in node:
                walk(item)
        elif isinstance(node, dict):
            if isinstance(node.get("name"), str) and isinstance(node.get("type"), str):
                out.append(node)
            else:
                for value in node.values():
                    walk(value)

    walk((section or {}).get("fields"))
    return out


def definition_findings(definition, base_schema, skip=()) -> list:
    """
    Every Finding of a lab definition's own sections against `base_schema` (libs/lab_schema_base.base_lab_schema()):
    common, each node of nodes, each kcluster of kclusters, and pxe. `skip` names the fields the caller checks itself
    (the preflight checks `backend` and `cloud_account` against the installed registries, not the schema). Add-on
    sections are each add-on's own business (addon_findings(), called from its --validate).
    """
    out = []
    sections = (base_schema or {}).get("sections") or {}
    for name in ("common", "pxe"):
        cfg = (definition or {}).get(name)
        if isinstance(cfg, dict):
            out.extend(section_findings(_section_fields(sections.get(name)), cfg, name, skip))
    for name in ("nodes", "kclusters"):
        fields = _section_fields(sections.get(name))
        for key, cfg in ((definition or {}).get(name) or {}).items():
            if isinstance(cfg, dict):
                out.extend(section_findings(fields, cfg, "{}.{}".format(name, key), skip))
    return out


def addon_findings(definition, section: str, fields) -> list:
    """
    Every Finding of add-on `section`'s own values in `definition`: its shared section, and the per-node overrides a
    node's addons[] entry carries for it ({"rancher": {...}} — see libs/apps.addon_entry_name()).
    """
    out = []
    cfg = (definition or {}).get(section)
    if isinstance(cfg, dict):
        out.extend(section_findings(fields, cfg, section))
    for node, node_cfg in ((definition or {}).get("nodes") or {}).items():
        for entry in (node_cfg or {}).get("addons") or []:
            if isinstance(entry, dict) and isinstance(entry.get(section), dict):
                out.extend(section_findings(fields, entry[section], "nodes.{}.addons.{}".format(node, section)))
    return out
