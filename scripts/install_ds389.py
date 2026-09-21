#!/usr/bin/env python3.11
# Part of lab-in-a-box, it will install 389 Directory Server (LDAP) on
# Kubernetes
# Author/s: Raul Mahiques
# License: GPLv3
#
# Project: https://github.com/389ds/ds-container (389 Directory Server
# project, quay.io/389ds/dirsrv)
#
# The bash version of this addon (install_ds389, still present unported) was
# fundamentally broken and never did anything real — it called a function
# (load_ds389_vars) that doesn't exist anywhere in the codebase, and its own
# setup_ds389() body was a verbatim copy-paste of install_longhorn's
# installer. python_migration deliberately skipped porting it rather than
# faithfully reproducing a crash or inventing behavior (see
# MIGRATION_TODO.md, 2026-08-10). THIS is that invented behavior, written
# fresh per explicit user request (2026-09-21) rather than ported from
# anything — every technical detail below (image, env vars, ports, mount
# paths, the required chown init container) is ground-truthed against the
# 389ds/ds-container repo's own kustomize manifests and its
# howto-deploy-389ds-on-openshift.html doc, fetched raw, not guessed.
#
# Deployment shape: a real StatefulSet (not Deployment) with a per-pod
# volumeClaimTemplate, matching upstream's own explicit recommendation —
# "A StatefulSet is used instead of Deployment kind. It provides guarantees
# about the ordering and uniqueness of the Pods." An initContainer does
# `chown -R 389:389 /data` before dirsrv starts, exactly as upstream's own
# sample does — required because dscontainer otherwise can't create its own
# subdirectories on a freshly-mounted, root-owned volume. Exposed via two
# Services: a headless ClusterIP one (required as the StatefulSet's own
# serviceName) and a NodePort one for LDAP (389) and LDAPS (636) — no
# Ingress, since LDAP isn't HTTP. Upstream's OpenShift doc also covers an
# SCC/anyuid grant and a dedicated ServiceAccount — both purely
# OpenShift-specific (Security Context Constraints don't exist on plain
# Kubernetes) and deliberately omitted here, since every other addon in
# this repo targets RKE2/K3s, not OpenShift.
#
# JSON section: "ds389" — configurable keys:
#   ds389_ns              : [OPTIONAL] Kubernetes namespace                (default: ds389)
#   ds389_name             : [OPTIONAL] StatefulSet/Service base name       (default: dirsrv)
#   ds389_image             : [OPTIONAL] container image                    (default:
#                             quay.io/389ds/dirsrv:latest — the real, actively-published
#                             upstream image; :c9s is the CentOS-Stream-9-based alternative tag)
#   ds389_basedn            : [OPTIONAL] LDAP suffix/basedn — the real DS_SUFFIX_NAME env var
#                             (default: derived from the cluster's own mydomain, e.g.
#                             "mydemo.lab" -> "dc=mydemo,dc=lab"; falls back to
#                             "dc=lab,dc=local" if mydomain is somehow unavailable). Upstream
#                             itself creates NO backends/suffixes by default — this only sets
#                             the basedn recorded in the instance's dsrc file for later use by
#                             dsconf/dsctl, it does not create the suffix's actual entries.
#   ds389_dm_password        : [OPTIONAL] cn=Directory Manager's password — the real
#                             DS_DM_PASSWORD env var (default: auto-generated and printed;
#                             upstream's own default is a container-internal random password
#                             only visible in the pod's setup log, which is far less usable for
#                             automation, so this addon generates and surfaces one instead)
#   ds389_account            : [OPTIONAL] name of an encrypted credential_kind "ds389" file
#                             under /etc/lab_creation/credentials/ (see README's Credentials
#                             section) to read ds389_dm_password from instead of this section's
#                             own plaintext field — auto-discovered if exactly one "ds389"
#                             credential file exists and this is left unset. The plaintext
#                             field above remains fully valid either way.
#   ds389_storage_size       : [OPTIONAL] per-pod PVC size                  (default: 5Gi)
#   ds389_storage_class      : [OPTIONAL] StorageClass name                 (default: cluster's
#                             own default — see install_open_webui.py's own note: a bare RKE2
#                             cluster has NO default StorageClass; install one first, e.g. this
#                             project's own "longhorn" addon)
#   ds389_ldap_nodeport       : [OPTIONAL] NodePort for plaintext LDAP        (default: 30389 —
#                             upstream's own example value)
#   ds389_ldaps_nodeport      : [OPTIONAL] NodePort for LDAPS                 (default: 30636 —
#                             upstream's own example value)
#
# NOT live-tested — no real Kubernetes cluster was available in this session to deploy
# against. The manifest shape, image reference, env var names, and mount paths are all
# ground-truthed against the real upstream repo/doc (see above), but the full pipeline end to
# end has only been exercised via the mocked test suite.

__version__ = "__LABVERSION__"

PLUGIN = {
    "name": "ds389",
    "targets": ["container"],
    "layers": ["kubernetes"],
    "requires_kubernetes": ["rke2", "k3s"],
    "aux_services": [],
}

import secrets
import sys
from pathlib import Path

for _candidate in ("/usr/local/lib/lab_creation", str(Path(__file__).resolve().parent.parent / "libs")):
    if Path(_candidate).is_dir() and _candidate not in sys.path:
        sys.path.insert(0, _candidate)

import addon_common as ac  # noqa: E402
import primary  # noqa: E402
import k8s  # noqa: E402
from lab_creation import ssh_run  # noqa: E402

_DEFAULT_IMAGE = "quay.io/389ds/dirsrv:latest"

_MANIFEST_TEMPLATE = """---
apiVersion: v1
kind: Namespace
metadata:
  name: {ns}
---
apiVersion: v1
kind: Secret
metadata:
  name: {name}-dm-password
  namespace: {ns}
type: Opaque
stringData:
  DS_DM_PASSWORD: {dm_password_yaml}
---
apiVersion: v1
kind: Service
metadata:
  name: {name}-internal
  namespace: {ns}
  labels:
    app: {name}
spec:
  clusterIP: None
  selector:
    app: {name}
  ports:
    - name: ldap
      port: 3389
      targetPort: 3389
    - name: ldaps
      port: 3636
      targetPort: 3636
---
apiVersion: v1
kind: Service
metadata:
  name: {name}
  namespace: {ns}
  labels:
    app: {name}
spec:
  type: NodePort
  selector:
    app: {name}
  ports:
    - name: ldap
      port: 389
      targetPort: 3389
      nodePort: {ldap_nodeport}
    - name: ldaps
      port: 636
      targetPort: 3636
      nodePort: {ldaps_nodeport}
---
apiVersion: apps/v1
kind: StatefulSet
metadata:
  name: {name}
  namespace: {ns}
spec:
  serviceName: {name}-internal
  replicas: 1
  selector:
    matchLabels:
      app: {name}
  template:
    metadata:
      labels:
        app: {name}
    spec:
      # fsGroup only exists at the POD level (v1.PodSecurityContext), not
      # per-container (v1.SecurityContext) — a real API rejection caught
      # live-testing this addon 2026-09-21 ("strict decoding error: unknown
      # field ...containers[0].securityContext.fsGroup"), even though
      # upstream's own doc sample nests it under the container the same
      # (wrong) way. Left at pod level only (not also set on runAsUser)
      # since a pod-level runAsUser would default onto the initContainer
      # below too, which needs to run as root to chown the volume in the
      # first place.
      securityContext:
        fsGroup: 389
      initContainers:
        - name: fix-data-perms
          image: busybox
          command: ["sh", "-c", "chown -R 389:389 /data"]
          volumeMounts:
            - name: data
              mountPath: /data
      containers:
        - name: dirsrv
          image: {image}
          env:
            - name: DS_SUFFIX_NAME
              value: {basedn_yaml}
            - name: DS_DM_PASSWORD
              valueFrom:
                secretKeyRef:
                  name: {name}-dm-password
                  key: DS_DM_PASSWORD
          ports:
            - name: ldap
              containerPort: 3389
            - name: ldaps
              containerPort: 3636
          securityContext:
            runAsUser: 389
          volumeMounts:
            - name: data
              mountPath: /data
  volumeClaimTemplates:
    - metadata:
        name: data
      spec:
        accessModes: ["ReadWriteOnce"]
{storage_class_line}        resources:
          requests:
            storage: {storage_size}
"""


def _validate(v):
    v.vns("ds389")


def _yaml_dq(value):
    """
    Escape `value` as a YAML double-quoted scalar — NOT shell quoting
    (shlex.quote produces POSIX-shell-only syntax, e.g. splicing multiple
    quoted segments together for an embedded "'", which is not valid as a
    single YAML scalar). Used for the two values here (basedn, generated/
    configured DM password) that reach the rendered manifest without going
    through require_k8s_name's own strict character check.
    """
    escaped = str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return '"{}"'.format(escaped)


def _default_basedn(mydomain):
    """
    Real 389-ds default is "derived from the hostname" (upstream's own
    doc), which isn't reproducible/predictable for automation purposes —
    this addon instead derives a real, conventional basedn from the
    cluster's own mydomain (e.g. "mydemo.lab" -> "dc=mydemo,dc=lab"),
    falling back to a generic placeholder only if mydomain is unavailable.
    """
    parts = [p for p in (mydomain or "").split(".") if p]
    if not parts:
        return "dc=lab,dc=local"
    return ",".join("dc={}".format(p) for p in parts)


def render_ds389_manifest(ns, name, image, basedn, dm_password, storage_size, storage_class,
                           ldap_nodeport, ldaps_nodeport):
    storage_class_line = "        storageClassName: {}\n".format(storage_class) if storage_class else ""
    return _MANIFEST_TEMPLATE.format(
        ns=ns, name=name, image=image,
        basedn_yaml=_yaml_dq(basedn),
        dm_password_yaml=_yaml_dq(dm_password),
        storage_class_line=storage_class_line, storage_size=storage_size,
        ldap_nodeport=ldap_nodeport, ldaps_nodeport=ldaps_nodeport,
    )


def setup_ds389(hostname, mydomain, cfg):
    ns = ac.require_k8s_name(cfg, "ds389_ns", "ds389")
    name = ac.require_k8s_name(cfg, "ds389_name", "dirsrv")
    image = cfg.get("ds389_image") or _DEFAULT_IMAGE
    basedn = cfg.get("ds389_basedn") or _default_basedn(mydomain)

    creds = ac.resolve_credential(cfg, "ds389", {"ds389_dm_password": "ds389_dm_password"},
                                   account_key="ds389_account")
    dm_password = creds["ds389_dm_password"]
    if not dm_password:
        dm_password = secrets.token_urlsafe(18)
        print("  No ds389_dm_password configured anywhere — generated one: {}".format(dm_password))

    ldap_nodeport = int(cfg.get("ds389_ldap_nodeport") or 30389)
    ldaps_nodeport = int(cfg.get("ds389_ldaps_nodeport") or 30636)

    manifest = render_ds389_manifest(
        ns, name, image, basedn, dm_password,
        cfg.get("ds389_storage_size") or "5Gi", cfg.get("ds389_storage_class"),
        ldap_nodeport, ldaps_nodeport,
    )
    ssh_run(hostname, "kubectl apply -f -", input_text=manifest)

    print("389 Directory Server deployed. Namespace: {}".format(ns))
    print("Basedn: {}".format(basedn))
    print("cn=Directory Manager password: {}".format(dm_password))
    print("LDAP:  ldap://<any cluster node>:{}".format(ldap_nodeport))
    print("LDAPS: ldaps://<any cluster node>:{}".format(ldaps_nodeport))
    print("LDAPS uses a self-signed cert (upstream's own default, confirmed live 2026-09-21) — "
          "a client that verifies certs (most do by default) needs LDAPTLS_REQCERT=never or a "
          "real cert dropped in /data/tls/ (see upstream's own doc) until one is configured.")
    print("No backends/suffixes are created automatically — use dsconf/dsctl inside the pod "
          "to create them, per upstream's own documented workflow.")


def main():
    ac.handle_common_args(__file__, __version__, validate_fn=_validate, plugin=PLUGIN)

    if len(sys.argv) < 2:
        print("Usage: {} <lab.json>".format(Path(sys.argv[0]).name))
        sys.exit(1)
    json_file = sys.argv[1]
    definition = primary.load_definition(json_file)

    target = k8s.first_server_node(definition)
    if not target:
        sys.exit(1)
    vm_name, _ssh_cmd = target

    clu_name = k8s.get_vm_kcluster(definition, vm_name)
    clu_cfg = k8s.load_kclu_vars(definition, clu_name) if clu_name else {}
    cfg = definition.get("ds389", {}) or {}

    setup_ds389(vm_name, clu_cfg.get("mydomain"), cfg)


if __name__ == "__main__":
    main()
