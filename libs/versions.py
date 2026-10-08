"""
Add-on version matrix: the versions an add-on declares it can install, and the checks run against a lab.

An add-on declares it in PLUGIN["versions"] (published in its --schema output as capabilities.versions): a mapping from
a version field of its JSON section to a list of entries, newest first:

    "versions": {
        "rancher_version": [
            {"version": "2.13",
             "kubernetes": {"rke2": {"min": "1.32", "max": "1.34"}, "k3s": {"min": "1.32", "max": "1.34"}},
             "os": ["sle15sp7"]},
        ],
    }

  version     exact version or prefix: "2.13" matches 2.13 and 2.13.x. Compared after normalize().
  kubernetes  optional: the kcluster types the version supports, each with an optional inclusive Kubernetes
              minor range (min, max).
  os          optional: the VM_OSVARIANT values of the nodes it runs on.

Every part but "version" is optional, and so is the key itself: what is not declared is not checked. The checks give
warnings only; a lab outside the matrix can still be deployed.
"""
import re
import shlex
from typing import Dict, Iterable, List, Optional, Tuple


def normalize(value: object) -> str:
    """`value` as a bare version: surrounding spaces, a leading "--version" flag and a leading "v" removed."""
    v = re.sub(r"^--version[=\s]+", "", str(value if value is not None else "").strip())
    return v[1:] if v[:1] in ("v", "V") and v[1:2].isdigit() else v


def helm_version_flag(value: object) -> str:
    """The helm "--version <v>" argument for a chart version field ("" when empty); accepts "2.13.3" or "--version 2.13.3"."""
    v = re.sub(r"^--version[=\s]+", "", str(value if value is not None else "").strip())
    return "--version {}".format(shlex.quote(v)) if v else ""


def _numbers(version: str) -> Tuple[int, ...]:
    """The leading dotted numbers of `version` ("1.32.5+rke2r1" gives (1, 32, 5)); () when it starts with none."""
    m = re.match(r"\d+(?:\.\d+)*", normalize(version))
    return tuple(int(x) for x in m.group(0).split(".")) if m else ()


def matches(declared: str, value: str) -> bool:
    """True when `value` is the declared version or a release of it ("2.13" matches "2.13" and "2.13.3")."""
    d, v = normalize(declared), normalize(value)
    return bool(d) and (v == d or v.startswith(d + "."))


def entry_for(entries: Iterable[dict], value: str) -> Optional[dict]:
    """The first entry of `entries` whose version matches `value`, or None."""
    return next((e for e in entries or [] if matches(str(e.get("version", "")), value)), None)


def kubernetes_minor(clu_rel: str) -> Tuple[int, ...]:
    """The (major, minor) Kubernetes version of a kcluster's clu_rel, or () for a channel such as stable or latest."""
    return _numbers(clu_rel)[:2] if re.match(r"^v?\d+\.\d+", str(clu_rel or "").strip()) else ()


def suggestions(versions: Dict[str, List[dict]], field: str) -> List[str]:
    """The declared versions of `field`, newest first."""
    return [str(e.get("version")) for e in (versions or {}).get(field) or [] if e.get("version")]


def issues(versions: Dict[str, List[dict]], cfg: dict, clu_type: str = "", clu_rel: str = "",
           os_variants: Iterable[str] = ()) -> List[str]:
    """
    Why the add-on config `cfg` falls outside the version matrix `versions`, as sentences. An empty version field is
    "latest" and is not checked. `clu_type`/`clu_rel` describe the kcluster it is installed on ("" when none) and
    `os_variants` the VM_OSVARIANT values of the nodes it runs on (unset ones left out).
    """
    out = []
    kube = kubernetes_minor(clu_rel)
    for field, entries in sorted((versions or {}).items()):
        value = (cfg or {}).get(field)
        if value in (None, ""):
            continue
        entry = entry_for(entries, str(value))
        if entry is None:
            out.append("{} '{}' is not in the version matrix ({})".format(
                field, value, ", ".join(suggestions(versions, field)) or "none declared"))
            continue
        declared = entry["version"]
        supported = entry.get("kubernetes")
        if supported and clu_type:
            if clu_type not in supported:
                out.append("{} {} is not declared for clu_type '{}' (declared: {})".format(
                    field, declared, clu_type, ", ".join(sorted(supported))))
            elif kube:
                rng = supported[clu_type] or {}
                low, high = _numbers(rng.get("min", "")), _numbers(rng.get("max", ""))
                if (low and kube < low[:2]) or (high and kube > high[:2]):
                    out.append("{} {} supports Kubernetes {}–{} on {}, the kcluster's clu_rel is '{}'".format(
                        field, declared, rng.get("min", "any"), rng.get("max", "any"), clu_type, clu_rel))
        allowed = entry.get("os")
        if allowed:
            for variant in sorted(set(os_variants)):
                if variant not in allowed:
                    out.append("{} {} is not declared for VM_OSVARIANT '{}' (declared: {})".format(
                        field, declared, variant, ", ".join(allowed)))
    return out
