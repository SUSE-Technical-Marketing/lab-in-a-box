#!/bin/bash
# The lab-builder's server actions end to end: runs the real install_automation_node_scripts.sh with the Apache web UI,
# starts Apache and checks that Save to server, saved labs, credentials and Create lab need a login and HTTPS, that a
# login works through lab-builder-passwd, that the CGI reaches lab-builder-helper only through its sudoers rule, that
# credential values never come back, and that Create lab runs setup_lab.py (a stub) as a job. Then the same login on
# run-local.py (service mode). Independent container — see tests/run_tests.sh.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit
_fail=0
fail() { echo "FAIL: $*"; _fail=1; }

echo test-node > /etc/hostname
_webui_mode=apache _backup=off _tls_ips="192.0.2.10" bash install_automation_node_scripts.sh >/tmp/install.log 2>&1 \
    || { tail -30 /tmp/install.log; fail "install_automation_node_scripts.sh failed"; }
[[ -x /usr/local/sbin/lab-builder-helper && -x /usr/local/sbin/lab-builder-passwd ]] || fail "the lab-builder tools are not installed"
[[ "$(stat -c '%a %U %G' /etc/lab-builder/htpasswd)" == "640 root www" ]] || fail "the login file is not 0640 root:www: $(stat -c '%a %U %G' /etc/lab-builder/htpasswd)"
grep -qx "wwwrun ALL=(root) NOPASSWD: /usr/local/sbin/lab-builder-helper" /etc/sudoers.d/lab-builder \
    || fail "the sudoers rule for lab-builder-helper is missing"

# setup_lab.py stub for Create lab: prints its arguments, exits 0.
printf '#!/bin/bash\necho "setup_lab stub: $*"\n' > /usr/local/bin/setup_lab.py

/usr/sbin/start_apache2 -k start >/tmp/apache.log 2>&1 || { cat /tmp/apache.log; fail "Apache did not start"; }
for _i in $(seq 20); do curl -s -o /dev/null http://127.0.0.1/ && break; sleep 0.5; done
_url=https://127.0.0.1/lab-builder

# GET/POST $1 (action, query) on endpoint $2 with curl options $3...; prints "<status> <body>".
call() {
    local _q="$1" _ep="$2"; shift 2
    curl -sk -o /tmp/body -w '%{http_code}' "$@" "${_url}/${_ep}?action=${_q}"; echo " $(cat /tmp/body)"
}

[[ "$(call auth api)" == *'"login_configured": false'* ]] || fail "auth does not say that no login is set up"
[[ "$(call save api -X POST -d '{}')" == 401* ]] || fail "Save to server works without a login"
[[ "$(call labs admin)" == 401* ]] || fail "the login endpoint answers without a login"
[[ "$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1/lab-builder/admin?action=labs)" == 301 ]] \
    || fail "the login endpoint is not redirected to HTTPS before the login is asked for"

printf 'secret-pw1\nsecret-pw1\n' | lab-builder-passwd alice >/dev/null || fail "lab-builder-passwd failed"
[[ "$(lab-builder-passwd --list)" == alice ]] || fail "lab-builder-passwd --list does not list alice"
[[ "$(stat -c '%a %G' /etc/lab-builder/htpasswd)" == "640 www" ]] || fail "lab-builder-passwd changed the login file's mode or group"
[[ "$(call auth api)" == *'"login_configured": true'* ]] || fail "auth does not see the login"
[[ "$(call labs admin -u alice:wrong-pw)" == 401* ]] || fail "a wrong password is accepted"
_a=(-u alice:secret-pw1)
[[ "$(call labs admin "${_a[@]}")" == '200 {"labs": []}' ]] || fail "listing saved labs with a login failed: $(call labs admin "${_a[@]}")"
[[ "$(call save admin "${_a[@]}" -X POST -d '{"filename": "demo", "config": {"common": {}}}')" == 200* ]] || fail "Save to server with a login failed"
[[ "$(stat -c %U /srv/www/lab-builder/labs/demo.json)" == wwwrun ]] || fail "the saved lab is not written by the web server user"
[[ "$(call 'lab&name=demo.json' admin "${_a[@]}")" == *'"common": {}'* ]] || fail "opening a saved lab failed"

# Credentials, through sudo and lab-builder-helper.
_r=$(call credentials admin "${_a[@]}")
[[ "${_r}" == 200*'"aws"'* ]] || fail "listing credentials failed: ${_r}"
_r=$(call credentials-add admin "${_a[@]}" -X POST \
    -d '{"kind": "cloud", "type": "aws", "account": "main", "fields": {"AWS_REGION": "eu-west-1", "AWS_SECRET_ACCESS_KEY": "TOPSECRET1"}, "passphrase": null}')
[[ "${_r}" == '200 {"file": "aws-main.yaml"}' ]] || fail "adding a credential failed: ${_r}"
[[ "$(stat -c '%a %U' /etc/lab_creation/credentials/aws-main.yaml)" == "600 root" ]] || fail "the credential file is not 0600 root"
_r=$(call credentials admin "${_a[@]}")
grep -q TOPSECRET1 <<<"${_r}" && fail "a credential value came back to the browser"
[[ "${_r}" == *'"plaintext_secrets": ["AWS_SECRET_ACCESS_KEY"]'* ]] || fail "the plaintext secret is not reported: ${_r}"
[[ "$(call credentials-add admin "${_a[@]}" -X POST -d '{"kind": "cloud", "type": "aws", "account": "main", "fields": {"AWS_REGION": "x"}, "passphrase": null}')" == 400*already* ]] \
    || fail "adding a credential overwrote an existing file"
[[ "$(call credentials-add admin "${_a[@]}" -X POST -d '{"kind": "cloud", "type": "aws", "account": "../x", "fields": {"AWS_REGION": "x"}, "passphrase": null}')" == 400* ]] \
    || fail "an account name with a path was accepted"
_r=$(call credentials-encrypt admin "${_a[@]}" -X POST -d '{"file": "aws-main.yaml", "passphrase": "pass-phrase-1"}')
[[ "${_r}" == '200 {"file": "aws-main.encrypted.yaml", "encrypted": ["AWS_SECRET_ACCESS_KEY"]}' ]] || fail "encrypting a credential failed: ${_r}"
grep -q TOPSECRET1 /etc/lab_creation/credentials/aws-main.encrypted.yaml && fail "the encrypted copy holds the secret in plaintext"
python3.11 -c "
import sys, yaml; sys.path.insert(0, '/usr/local/lib/lab_creation'); import crypto_store
d = yaml.safe_load(open('/etc/lab_creation/credentials/aws-main.encrypted.yaml'))
assert crypto_store.decrypt_cascade('pass-phrase-1', d['AWS_SECRET_ACCESS_KEY']) == b'TOPSECRET1'
" || fail "the encrypted copy does not decrypt with its passphrase"
[[ "$(call credentials-delete admin "${_a[@]}" -X POST -d '{"file": "aws-main.yaml"}')" == '200 {"deleted": "aws-main.yaml"}' ]] \
    || fail "deleting a credential failed"
[[ "$(call credentials-delete admin "${_a[@]}" -X POST -d '{"file": "../../lab_creation.cfg"}')" == 400* ]] \
    || fail "deleting a file outside the credentials directory was accepted"
sudo -u wwwrun sudo -n /usr/bin/id >/dev/null 2>&1 && fail "the web server user can run more than lab-builder-helper as root"

# Create lab: a background job running setup_lab.py --keep on the saved lab.
_r=$(call create admin "${_a[@]}" -X POST -d '{"filename": "demo.json", "keep": true}')
[[ "${_r}" == 200*'"job": "demo-'* ]] || fail "Create lab failed: ${_r}"
_job=$(sed -n 's/.*"job": "\([^"]*\)".*/\1/p' <<<"${_r}")
for _i in $(seq 20); do _r=$(call "job&id=${_job}" admin "${_a[@]}"); [[ "${_r}" == *'"state": "running"'* ]] || break; sleep 0.5; done
[[ "${_r}" == *'"state": "done"'* && "${_r}" == *'setup_lab stub: --keep /srv/www/lab-builder/labs/demo.json'* ]] \
    || fail "the job did not run setup_lab.py --keep on the saved lab: ${_r}"
[[ "$(call create admin "${_a[@]}" -X POST -d '{"filename": "../demo.json"}')" == 400* ]] || fail "Create lab accepted a path"
/usr/sbin/start_apache2 -k stop >/dev/null 2>&1

# run-local.py (service mode): the same login on /admin, and HTTPS required.
export LABBUILDER_USERS=/etc/lab-builder/htpasswd LABBUILDER_OUTPUT_DIR=/tmp/labs
LABBUILDER_TLS_CERT=/etc/lab_creation/tls/cert.pem LABBUILDER_TLS_KEY=/etc/lab_creation/tls/key.pem \
    python3 webui/run-local.py 8901 >/tmp/run-local.log 2>&1 &
python3 webui/run-local.py 8902 >/tmp/run-local-http.log 2>&1 &
for _i in $(seq 20); do curl -sk -o /dev/null https://127.0.0.1:8901/ && curl -s -o /dev/null http://127.0.0.1:8902/ && break; sleep 0.5; done
_url=https://127.0.0.1:8901
[[ "$(curl -sk -D - -o /dev/null "${_url}/admin?action=labs" | tr -d '\r')" == *"WWW-Authenticate: Basic"* ]] \
    || fail "run-local.py does not ask for a login on /admin"
[[ "$(call labs admin -u alice:wrong-pw 2>/dev/null)" == 401* ]] || fail "run-local.py accepts a wrong password"
[[ "$(curl -sk -u alice:secret-pw1 "${_url}/admin?action=labs")" == '{"labs": []}' ]] || fail "run-local.py refuses the login"
[[ "$(curl -s -u alice:secret-pw1 -o /dev/null -w '%{http_code}' "http://127.0.0.1:8902/admin?action=labs")" == 403 ]] \
    || fail "run-local.py over plain HTTP runs a login action"
kill %1 %2 2>/dev/null

[[ ${_fail} -eq 0 ]] && echo "all lab-builder login checks passed"
exit ${_fail}
