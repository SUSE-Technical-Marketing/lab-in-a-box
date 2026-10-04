#!/usr/bin/env python3.11
# Part of lab-in-a-box. Installs 389 Directory Server (LDAP) on Kubernetes.
# Author/s: Raul Mahiques
# License: GPLv3
#
# Project: https://github.com/389ds/ds-container (389 Directory Server project, quay.io/389ds/dirsrv)
#
# The technical details, which are the image, the environment variables, the ports, the mount paths and the required chown init container,
# come from the ds-container repository's Kustomize manifests and its OpenShift deployment guide.
#
# Deployment: a StatefulSet with a volumeClaimTemplate for each pod, as the upstream guide recommends. An initContainer runs chown -R 389:389
# /data before dirsrv starts. This is required, because dscontainer cannot create its subdirectories on a freshly mounted, root-owned volume.
# The pod is exposed by two Services: a headless ClusterIP one, which the StatefulSet requires as its serviceName, and a NodePort one for LDAP
# (389) and LDAPS (636). No Ingress is used, because LDAP is not HTTP. The OpenShift-specific parts of the guide, the SCC grant and the
# dedicated ServiceAccount, are omitted, because Security Context Constraints do not exist on plain Kubernetes.
#
# JSON section: "ds389"
#   ds389_ns              : Kubernetes namespace (default ds389)
#   ds389_name            : StatefulSet and Service base name (default dirsrv)
#   ds389_image           : container image (default quay.io/389ds/dirsrv:latest). The c9s tag is the CentOS Stream 9 based alternative.
#   ds389_basedn          : LDAP suffix, set as DS_SUFFIX_NAME (default derived from the cluster's mydomain, for example mydemo.lab becomes
#                           dc=mydemo,dc=lab; falls back to dc=lab,dc=local). The image creates no suffix by default. This value is recorded
#                           in the instance's dsrc file for dsconf and dsctl, and the suffix's entries are not created.
#   ds389_dm_password     : password of cn=Directory Manager, set as DS_DM_PASSWORD (default: generated and printed). The image's own default is
#                           a random password visible only in the pod's log, so the addon generates one instead.
#   ds389_account         : name of an encrypted credential file of kind "ds389" under /etc/lab_creation/credentials/ to read ds389_dm_password
#                           from. It is auto-discovered when exactly one such file exists and this is unset.
#   ds389_storage_size    : per-pod PersistentVolumeClaim size (default 5Gi)
#   ds389_storage_class   : StorageClass (default: the cluster's own default). A bare RKE2 cluster has none, so install one first, for example
#                           with the longhorn addon.
#   ds389_ldap_nodeport   : NodePort for plaintext LDAP (default 30389, the upstream example value)
#   ds389_ldaps_nodeport  : NodePort for LDAPS (default 30636, the upstream example value)

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
      # fsGroup is a pod-level field (v1.PodSecurityContext). Kubernetes rejects it
      # under containers[].securityContext, so it is set only here. The pod-level
      # runAsUser is not set, because the initContainer below runs as root to
      # chown the volume.
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
    print("LDAPS uses a self-signed cert (upstream's default) — "
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
