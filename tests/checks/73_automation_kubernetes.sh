#!/bin/bash
# setup_lab_automation.sh with _automation_node=kubernetes: runs the real script with kubectl, systemctl, virsh, curl
# and the network commands replaced by stubs, then checks the manifests it applies (LoadBalancer and macvlan),
# the commands it runs inside the pod and the seed init container's script.
# Independent container — see tests/run_tests.sh.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit
_repo=$PWD
_fail=0
fail() { echo "FAIL: $*"; _fail=1; }

_t=$(mktemp -d)
_bin="${_t}/bin" _log="${_t}/calls.log"
mkdir -p "${_bin}" "${_t}/lab"
export STUB_LOG="${_log}" STUB_DIR="${_t}"

cat > "${_bin}/kubectl" <<'EOF'
#!/bin/bash
echo "kubectl $*" >> "${STUB_LOG}"
case " $* " in
    *" apply -f - "*) cat > "${STUB_DIR}/manifests.yaml" ;;
    *" exec -i "*" tar -C /var/tmp/lab-in-a-box -xf - "*) cat > /dev/null ;;
    *" exec -i "*" tar -C / "*) cat > /dev/null ;;
    *" exec -i "*) cat >> "${STUB_DIR}/stdin.log" ;;
    *" cat /root/.ssh/id_rsa.pub "*) echo "ssh-rsa PODKEY root@automation" ;;
esac
exit 0
EOF
cat > "${_bin}/curl" <<'EOF'
#!/bin/bash
while [[ $# -gt 0 ]]; do [[ "$1" == --output ]] && { : > "$2"; shift; }; shift; done
[[ -t 1 ]] || echo v3.99.0
EOF
cat > "${_bin}/ip" <<'EOF'
#!/bin/bash
[[ "$*" == "-4 -br addr show br0" ]] && echo "br0              UP             192.0.2.2/24"
exit 0
EOF
for _c in systemctl virsh nc tput ssh-keygen hostname podman; do
    printf '#!/bin/bash\necho "%s $*" >> "${STUB_LOG}"\n[[ "%s $*" == "virsh "*" desc "* ]] && exit 1\nexit 0\n' "${_c}" "${_c}" > "${_bin}/${_c}"
done
chmod 0755 "${_bin}"/*
mkdir -p /root/.ssh && : > /root/.ssh/id_rsa && echo "ssh-ed25519 HOSTKEY root@host" > /root/.ssh/id_rsa.pub

# Runs setup_lab_automation.sh with lab.cfg plus the extra KEY=value lines given as arguments.
run_setup() {
    : > "${_log}"; rm -f "${_t}/manifests.yaml"
    {
        cat <<'EOF'
_automation_node="kubernetes"
_k8s_kubeconfig="/etc/k8s/admin.conf"
_network_mode="bridge"
_bridge_name="br0"
_bridge_nic=""
_myip="192.0.2.10"
_mynet="192.0.2.0/24"
_mygw="192.0.2.1"
_mydns="192.0.2.53"
_mynetrev="2.0.192"
_mydomain="mydemo.lab"
AUTOMATION_HOSTNAME="automation.mydemo.lab"
MYREG="registry.mydemo.lab"
ROOT_SSH_PUB_KEY="ssh-ed25519 OPERATORKEY op@laptop"
root_pwd="s3cret-pw"
_qemu_addr="qemu:///system"
_virt_srv="root@192.0.2.2"
EOF
        printf '%s\n' "$@"
    } > "${_t}/lab/lab.cfg"
    (cd "${_t}/lab" && PATH="${_bin}:${PATH}" LAB_PYTHON=python3.11 \
        GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=safe.directory GIT_CONFIG_VALUE_0='*' \
        bash "${_repo}/setup_demo_server/setup_lab_automation.sh" > "${_t}/run.log" 2>&1)
}

# ── LoadBalancer (default) ────────────────────────────────────────────────────
run_setup
if grep -q "ERROR" "${_t}/run.log" || ! grep -q "Reconfigure host to use new VM as DNS server" "${_t}/run.log"; then
    tail -30 "${_t}/run.log"; fail "setup_lab_automation.sh failed in kubernetes mode"
fi
grep -q "^kubectl --kubeconfig /etc/k8s/admin.conf apply -f -$" "${_log}" || fail "the manifests are not applied with the configured kubeconfig"
grep -q "^kubectl --kubeconfig /etc/k8s/admin.conf -n lab-automation rollout status statefulset/lab-automation" "${_log}" \
    || fail "setup does not wait for the StatefulSet rollout"
grep -q "^kubectl --kubeconfig /etc/k8s/admin.conf -n lab-automation exec lab-automation-0 -c automation -- bash /etc/lab_creation/node/automation-node-lib.sh configure$" "${_log}" \
    || fail "the pod is not configured with automation-node-lib.sh configure"
grep -q "^kubectl .* exec -i lab-automation-0 -c automation -- tar -C / --no-overwrite-dir -xf -$" "${_log}" \
    || fail "the node's inputs are not copied into the pod"
grep -q "^podman create\|^virt-install" "${_log}" && fail "kubernetes mode created a container or VM on the host"
grep -q "s3cret-pw" "${_log}" && fail "the root password appears on a command line"
grep -q "PODKEY" /root/.ssh/authorized_keys || fail "the pod's key is not authorized on the host"
cp "${_t}/manifests.yaml" "${_t}/lb.yaml"

run_setup '_k8s_network="macvlan"' '_k8s_macvlan_master="eth1"' '_k8s_storage_class="longhorn"' \
    '_k8s_namespace="labs"' '_automation_image="registry.example/lab-automation-node:1"'
grep -q "ERROR" "${_t}/run.log" && { tail -20 "${_t}/run.log"; fail "setup_lab_automation.sh failed with _k8s_network=macvlan"; }
cp "${_t}/manifests.yaml" "${_t}/macvlan.yaml"

run_setup '_k8s_network="macvlan"'
grep -q "_k8s_network=macvlan needs _k8s_macvlan_master" "${_t}/run.log" \
    || fail "_k8s_network=macvlan without _k8s_macvlan_master is not rejected"

python3.11 - "${_t}" <<'PY' || _fail=1
import json
import os
import subprocess
import sys
import tempfile

import yaml

t = sys.argv[1]
failures = []


def check(desc, cond):
    if not cond:
        failures.append(desc)
        print("FAIL:", desc)


def load(name):
    with open(os.path.join(t, name)) as f:
        return {d["kind"]: d for d in yaml.safe_load_all(f) if d}


DATA = {"etc-lab-creation": "/etc/lab_creation", "etc-lab-builder": "/etc/lab-builder", "etc-lab-mcp": "/etc/lab-mcp",
        "etc-ssh": "/etc/ssh", "named": "/var/lib/named", "root": "/root", "provisioning": "/srv/www/htdocs/lab_creation",
        "helm": "/srv/www/htdocs/helm"}

lb = load("lb.yaml")
check("LoadBalancer manifests are a Namespace, a StatefulSet and a Service",
      sorted(lb) == ["Namespace", "Service", "StatefulSet"])
sts = lb["StatefulSet"]
pod = sts["spec"]["template"]["spec"]
node = pod["containers"][0]
check("the namespace defaults to lab-automation", lb["Namespace"]["metadata"]["name"] == "lab-automation"
      and sts["metadata"]["namespace"] == "lab-automation")
check("the image defaults to ghcr.io/rmahique/lab-automation-node:latest",
      node["image"] == "ghcr.io/rmahique/lab-automation-node:latest")
check("the automation container is privileged", node["securityContext"]["privileged"] is True)
check("systemd in the pod knows it runs in a container", {"name": "container", "value": "oci"} in node["env"])
check("the pod resolves through its own named, then _mydns",
      pod["dnsPolicy"] == "None" and pod["dnsConfig"]["nameservers"] == ["127.0.0.1", "192.0.2.53"])
mounts = {m["mountPath"]: m.get("subPath") for m in node["volumeMounts"] if m["name"] == "data"}
check("every persistent path is a subPath of the data volume", mounts == {p: n for n, p in DATA.items()})
claim = sts["spec"]["volumeClaimTemplates"][0]["spec"]
check("the volume defaults to 20Gi on the cluster's default storage class",
      claim["resources"]["requests"]["storage"] == "20Gi" and "storageClassName" not in claim)
svc = lb["Service"]
check("the Service is a LoadBalancer at _myip, without MetalLB's annotation (MetalLB rejects both together)",
      svc["spec"]["type"] == "LoadBalancer" and svc["spec"]["loadBalancerIP"] == "192.0.2.10"
      and not svc["metadata"].get("annotations"))
check("the Service exposes DNS (UDP and TCP), SSH, HTTP and HTTPS",
      sorted((p["port"], p["protocol"]) for p in svc["spec"]["ports"])
      == [(22, "TCP"), (53, "TCP"), (53, "UDP"), (80, "TCP"), (443, "TCP")])
check("no Multus annotation without macvlan", not sts["spec"]["template"]["metadata"].get("annotations"))

mv = load("macvlan.yaml")
check("macvlan manifests replace the Service with a NetworkAttachmentDefinition",
      sorted(mv) == ["Namespace", "NetworkAttachmentDefinition", "StatefulSet"])
nad = json.loads(mv["NetworkAttachmentDefinition"]["spec"]["config"])
check("the macvlan interface sits on _k8s_macvlan_master with static addressing",
      nad["type"] == "macvlan" and nad["master"] == "eth1" and nad["ipam"]["type"] == "static")
nets = json.loads(mv["StatefulSet"]["spec"]["template"]["metadata"]["annotations"]["k8s.v1.cni.cncf.io/networks"])
check("the pod gets _myip and the automation MAC on the macvlan interface",
      nets == [{"name": "lab-automation-lan", "ips": ["192.0.2.10/24"], "mac": "52:54:00:00:02:0a"}])
mvs = mv["StatefulSet"]
check("_k8s_namespace, _automation_image and _k8s_storage_class are used",
      mvs["metadata"]["namespace"] == "labs"
      and mvs["spec"]["template"]["spec"]["containers"][0]["image"] == "registry.example/lab-automation-node:1"
      and mvs["spec"]["volumeClaimTemplates"][0]["spec"]["storageClassName"] == "longhorn")

# The seed init container fills an empty subPath from the image and leaves a filled one alone.
seed = pod["initContainers"][0]
script = seed["command"][2]
root = tempfile.mkdtemp()
os.makedirs(root + "/img/a")
os.makedirs(root + "/img/b")
open(root + "/img/a/default.cfg", "w").write("from image\n")
open(root + "/img/b/default.cfg", "w").write("from image\n")
os.makedirs(root + "/data/b")
open(root + "/data/b/user.cfg", "w").write("user data\n")
env = dict(os.environ, DATA="a:{0}/img/a b:{0}/img/b".format(root))
subprocess.run(["sh", "-c", script.replace("/data/", root + "/data/")], env=env, check=True)
check("the seed copies the image's files into an empty volume path", os.path.isfile(root + "/data/a/default.cfg"))
check("the seed leaves a volume path that has data alone",
      os.listdir(root + "/data/b") == ["user.cfg"])
check("the seed lists every persistent path in DATA",
      sorted(e.split(":")[0] for e in [v["value"] for v in seed["env"] if v["name"] == "DATA"][0].split())
      == sorted(DATA))
sys.exit(1 if failures else 0)
PY

exit "${_fail}"
