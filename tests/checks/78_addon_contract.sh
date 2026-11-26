#!/bin/bash
# The add-on contract: scripts/check_addon.py passes on every add-on in scripts/ and on the two example add-ons in
# examples/addons/, and fails on a broken one; the examples install what they document (ssh mocked).
# Independent container — see tests/run_tests.sh.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit
_fail=0
fail() { echo "FAIL: $*"; _fail=1; }

export PATH="${PWD}/scripts:${PATH}"
export LAB_ADDON_CACHE=/tmp/addon-cache.json
mapfile -t _addons < <(git -c safe.directory="*" ls-files 'scripts/install_*')

python3.11 scripts/check_addon.py --quiet "${_addons[@]}" || fail "check_addon.py fails on an add-on in scripts/"
python3.11 scripts/check_addon.py --quiet examples/addons/install_example_chart.py examples/addons/install_example_motd.sh \
    || fail "check_addon.py fails on an example add-on"

# A broken add-on: unknown target, prose default, version matrix for a missing field, stray --validate output.
_bad=$(mktemp -d)/install_broken.sh
cat > "${_bad}" <<'ADDON'
#!/bin/bash
case "${1:-}" in
    --schema) echo '{"section": "broken", "description": "", "fields": [{"name": "x", "type": "colour", "required": "yes"},
                                                                {"name": "n", "type": "integer", "required": false, "default": "1 — chart default"}],
                    "capabilities": {"targets": ["phone"], "layers": [], "requires_kubernetes": ["openshift"], "aux_services": [],
                                     "versions": {"y_version": [{"kubernetes": {"rke2": {"min": "latest"}}}]}}}' ;;
    --capabilities) echo '{}' ;;
    --validate) echo "checking..."; exit 3 ;;
    *) exit 0 ;;
esac
ADDON
chmod 0755 "${_bad}"
_out=$(python3.11 scripts/check_addon.py "${_bad}")
[[ $? -ne 0 ]] || fail "check_addon.py passes a broken add-on"
for _expect in "schema has a schema_version" "type 'colour'" "required must be" "targets ['phone']" "requires_kubernetes ['openshift']" \
               "y_version is not a field" \
               "default '1 — chart default' is not a valid integer" "every entry needs a version" "--capabilities matches" \
               "prints only [ERROR]/[WARNING] lines" "exits non-zero only with an [ERROR] line"; do
    grep -qF -- "${_expect}" <<<"${_out}" || fail "check_addon.py does not report: ${_expect}"
done

# install_example_motd.sh: required message, per-node override, append mode.
_motd=examples/addons/install_example_motd.sh
_tmp=$(mktemp -d)
echo '{"example_motd": {}}' > "${_tmp}/lab.json"
_out=$("${_motd}" --validate "${_tmp}/lab.json"); _rc=$?
[[ ${_rc} -ne 0 && "${_out}" == "[ERROR] example_motd.example_motd_message is required" ]] \
    || fail "motd: missing message not reported by --validate (${_rc}: ${_out})"
"${_motd}" --validate <(echo '{"common": {}}') >/dev/null || fail "motd: --validate fails on a lab without its section"
mkdir -p "${_tmp}/bin"
cat > "${_tmp}/bin/ssh" <<'SSH'
#!/bin/bash
echo "ARGS: $*" >> "${SSH_LOG}"; cat >> "${SSH_LOG}"
SSH
chmod 0755 "${_tmp}/bin/ssh"
cat > "${_tmp}/lab.json" <<'JSON'
{"example_motd": {"example_motd_message": "shared"},
 "nodes": {"vm1": {"addons": [{"example_motd": {"example_motd_message": "own", "example_motd_append": "true"}}]},
           "vm2": {"addons": ["example_motd"]}}}
JSON
export SSH_LOG="${_tmp}/ssh.log"
PATH="${_tmp}/bin:${PATH}" _vm_name=vm1 "${_motd}" "${_tmp}/lab.json" >/dev/null || fail "motd: install on vm1 failed"
PATH="${_tmp}/bin:${PATH}" _vm_name=vm2 "${_motd}" "${_tmp}/lab.json" >/dev/null || fail "motd: install on vm2 failed"
grep -q "root@vm1 cat >> /etc/motd" "${SSH_LOG}" && grep -qx "own" "${SSH_LOG}" || fail "motd: vm1 override (append) not applied"
grep -q "root@vm2 cat > /etc/motd" "${SSH_LOG}" && grep -qx "shared" "${SSH_LOG}" || fail "motd: vm2 shared message not applied"

# install_example_chart.py: helm upgrade --install with version, replicas and quoted message; refuses without a kcluster.
python3.11 - <<'PY' || _fail=1
import importlib.util, sys
from unittest import mock
spec = importlib.util.spec_from_file_location("ex", "examples/addons/install_example_chart.py")
ex = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ex)
calls = []
with mock.patch.object(ex, "ssh_run", side_effect=lambda h, c, **k: calls.append(c)), \
     mock.patch.object(ex, "helm_repo_add", side_effect=lambda h, n, u: calls.append("repo " + u)):
    ex.install("n1", {"example_chart_version": "6.15.0", "example_chart_replicas": 2, "example_chart_message": "it's up"})
helm = [c for c in calls if c.startswith("helm upgrade --install podinfo podinfo/podinfo --version 6.15.0 ")]
ok = helm and "--set replicaCount=2" in helm[0] and "--set ui.message='it'\"'\"'s up'" in helm[0] \
     and any("rollout status deployment/podinfo" in c for c in calls)
if not ok:
    print("FAIL: chart: helm command", calls)
    sys.exit(1)
PY
_out=$(env -u _vm_name -u clu_name python3.11 examples/addons/install_example_chart.py "${_tmp}/lab.json" 2>&1); _rc=$?
[[ ${_rc} -ne 0 && "${_out}" == *'$_vm_name and $clu_name are not set'* ]] || fail "chart: runs without a kcluster"

[[ ${_fail} -eq 0 ]] && echo "all add-on contract checks passed"
exit ${_fail}
