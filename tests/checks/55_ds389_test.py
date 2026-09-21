#!/usr/bin/env python3
# Unit tests for scripts/install_ds389.py — no real kubectl available in
# this container. Verifies the real image/env-var-name/mount-path/port
# ground-truthing against 389ds/ds-container's own kustomize manifests and
# howto-deploy-389ds-on-openshift.html doc, the YAML-safe (not shell-safe)
# quoting of attacker-influenceable values, the basedn-from-mydomain
# derivation, and the credential-store-with-plaintext-fallback wiring. Run
# from 55_ds389.sh, in its own container — see tests/run_tests.sh.
import sys
from pathlib import Path
from unittest import mock

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "libs"))
sys.path.insert(0, str(_REPO / "scripts"))

import install_ds389 as ids  # noqa: E402

failures = []


def check(desc, cond):
    if not cond:
        failures.append(desc)
        print("FAIL:", desc)


# ── _default_basedn: real, conventional derivation from mydomain ───────────
check("_default_basedn derives a real dc=,dc= chain from a real domain",
      ids._default_basedn("mydemo.lab") == "dc=mydemo,dc=lab")
check("_default_basedn handles a multi-label domain",
      ids._default_basedn("cluster1.mydemo.lab") == "dc=cluster1,dc=mydemo,dc=lab")
check("_default_basedn falls back to a generic placeholder when mydomain is empty/unavailable",
      ids._default_basedn("") == "dc=lab,dc=local" and ids._default_basedn(None) == "dc=lab,dc=local")


# ── _yaml_dq: real YAML-safe quoting, not shell quoting ─────────────────────
check("_yaml_dq escapes an embedded double quote",
      ids._yaml_dq('p@ss"word') == '"p@ss\\"word"')
check("_yaml_dq escapes an embedded backslash",
      ids._yaml_dq("a\\b") == '"a\\\\b"')
check("_yaml_dq produces a single well-formed double-quoted scalar for a value with a single "
      "quote (this is exactly where shell quoting would have produced invalid, spliced YAML)",
      ids._yaml_dq("o'clock") == '"o\'clock"')


# ── render_ds389_manifest: real Kubernetes object structure, ground-truthed
#    against 389ds/ds-container's own kustomize + doc ───────────────────────
manifest = ids.render_ds389_manifest(
    "ds389", "dirsrv", "quay.io/389ds/dirsrv:latest", "dc=mydemo,dc=lab", "s3cr3t",
    "5Gi", None, 30389, 30636)
check("render_ds389_manifest: creates the real Namespace object",
      "kind: Namespace" in manifest and "name: ds389" in manifest)
check("render_ds389_manifest: creates a Secret carrying the real DS_DM_PASSWORD env var name",
      "kind: Secret" in manifest and 'DS_DM_PASSWORD: "s3cr3t"' in manifest)
check("render_ds389_manifest: the DM password reaches the container via secretKeyRef, not inline",
      "secretKeyRef" in manifest and "key: DS_DM_PASSWORD" in manifest)
check("render_ds389_manifest: the real DS_SUFFIX_NAME env var carries the basedn",
      'name: DS_SUFFIX_NAME' in manifest and '"dc=mydemo,dc=lab"' in manifest)
check("render_ds389_manifest: uses the real upstream image quay.io/389ds/dirsrv:latest",
      "image: quay.io/389ds/dirsrv:latest" in manifest and manifest.count("image:") == 2)
check("render_ds389_manifest: uses a real StatefulSet (real persistence guarantees), not a "
      "Deployment", "kind: StatefulSet" in manifest and "kind: Deployment" not in manifest)
check("render_ds389_manifest: a per-pod volumeClaimTemplate is used (real, upstream-recommended "
      "persistence), not an emptyDir", "volumeClaimTemplates" in manifest and "emptyDir" not in manifest)
check("render_ds389_manifest: no storageClassName line when ds389_storage_class is unset",
      "storageClassName" not in manifest)
check("render_ds389_manifest: the required chown-to-389 init container is present (upstream's own "
      "documented requirement — dscontainer can't create subdirectories on a root-owned volume "
      "otherwise)",
      "fix-data-perms" in manifest and "chown -R 389:389 /data" in manifest)
check("render_ds389_manifest: the real container ports 3389 (LDAP) and 3636 (LDAPS) are exposed",
      "containerPort: 3389" in manifest and "containerPort: 3636" in manifest)
check("render_ds389_manifest: runs as the real dirsrv uid/gid 389, not root",
      "runAsUser: 389" in manifest and "fsGroup: 389" in manifest)
check("render_ds389_manifest: a headless internal Service (clusterIP: None) backs the "
      "StatefulSet's own serviceName, as upstream's own doc requires",
      "clusterIP: None" in manifest and "serviceName: dirsrv-internal" in manifest)
check("render_ds389_manifest: the external Service is a real NodePort at the configured ports "
      "(30389/30636, upstream's own example values)",
      "type: NodePort" in manifest and "nodePort: 30389" in manifest and "nodePort: 30636" in manifest)

manifest_sc = ids.render_ds389_manifest(
    "ds389", "dirsrv", "img", "dc=x,dc=y", "pw", "10Gi", "longhorn", 30389, 30636)
check("render_ds389_manifest: a configured ds389_storage_class becomes a real storageClassName",
      "storageClassName: longhorn" in manifest_sc)


# ── setup_ds389: full orchestration, credential resolution ─────────────────
ssh_calls = []
ids.ssh_run = lambda hostname, cmd, **kw: ssh_calls.append((hostname, cmd, kw.get("input_text")))

# Happy path, all defaults: no plaintext password anywhere -> one is generated;
# no mydomain configured -> falls back to the generic basedn.
ssh_calls.clear()
with mock.patch.object(ids.primary, "find_service_credential_for_kind", return_value=(None, [])):
    ids.setup_ds389("host1", None, {})
check("setup_ds389: happy path applies the manifest via kubectl apply -f -",
      len(ssh_calls) == 1 and ssh_calls[0][1] == "kubectl apply -f -")
applied_manifest = ssh_calls[0][2]
check("setup_ds389: no ds389_dm_password configured anywhere -> a real one was generated and "
      "put in the manifest", 'DS_DM_PASSWORD: "' in applied_manifest
      and 'DS_DM_PASSWORD: ""' not in applied_manifest)
check("setup_ds389: no mydomain available -> falls back to the generic basedn",
      '"dc=lab,dc=local"' in applied_manifest)
check("setup_ds389: defaults to the real upstream image",
      "quay.io/389ds/dirsrv:latest" in applied_manifest)

# Real mydomain -> real derived basedn reaches the manifest.
ssh_calls.clear()
with mock.patch.object(ids.primary, "find_service_credential_for_kind", return_value=(None, [])):
    ids.setup_ds389("host1", "mydemo.lab", {"ds389_dm_password": "plaintext-pw"})
check("setup_ds389: a real mydomain produces the real derived basedn in the manifest",
      '"dc=mydemo,dc=lab"' in ssh_calls[0][2])
check("setup_ds389: the plaintext ds389_dm_password reaches the real Secret",
      'DS_DM_PASSWORD: "plaintext-pw"' in ssh_calls[0][2])

# Credential-store path: the "ds389" kind is auto-discovered when ds389_account is unset.
ssh_calls.clear()
with mock.patch.object(ids.primary, "find_service_credential_for_kind",
                        return_value=("my-ds389-creds", ["my-ds389-creds"])), \
     mock.patch.object(ids.primary, "load_service_credential",
                        return_value={"ds389_dm_password": "pw-from-store"}):
    ids.setup_ds389("host1", "mydemo.lab", {})
check("setup_ds389: with no plaintext password, auto-discovers the single matching "
      "credential-store file", 'DS_DM_PASSWORD: "pw-from-store"' in ssh_calls[0][2])

# Custom namespace/name/ports are honored end to end.
ssh_calls.clear()
with mock.patch.object(ids.primary, "find_service_credential_for_kind", return_value=(None, [])):
    ids.setup_ds389("host1", "mydemo.lab", {
        "ds389_ns": "custom-ldap", "ds389_name": "myds389",
        "ds389_ldap_nodeport": "31389", "ds389_ldaps_nodeport": "31636",
    })
check("setup_ds389: a custom namespace/name/ports reach the real manifest",
      "namespace: custom-ldap" in ssh_calls[0][2] and "name: myds389" in ssh_calls[0][2]
      and "nodePort: 31389" in ssh_calls[0][2] and "nodePort: 31636" in ssh_calls[0][2])


if failures:
    print("{} check(s) failed".format(len(failures)))
    sys.exit(1)
print("all ds389 checks passed")
