#!/bin/bash
# HTTPS for the provisioning web server: runs the real install_automation_node_scripts.sh, starts Apache with the
# installed config and fetches a provisioning file over HTTP and HTTPS. Then runs the libs/provisioning.py unit tests.
# Independent container — see tests/run_tests.sh.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit
_fail=0
fail() { echo "FAIL: $*"; _fail=1; }

echo test-node > /etc/hostname
_webui_mode=off _backup=off _tls_ips="192.0.2.10" bash install_automation_node_scripts.sh >/tmp/install.log 2>&1 \
    || { cat /tmp/install.log; fail "install_automation_node_scripts.sh failed"; }

_cert=/etc/lab_creation/tls/cert.pem
[[ -f "${_cert}" && -f /etc/lab_creation/tls/key.pem ]] || fail "certificate or key missing"
[[ "$(stat -c %a /etc/lab_creation/tls/key.pem)" == "600" ]] || fail "key is not mode 0600"
_san="$(openssl x509 -in "${_cert}" -noout -ext subjectAltName 2>/dev/null)"
for _name in "DNS:test-node" "IP Address:192.0.2.10"
do
    grep -q "${_name}" <<<"${_san}" || fail "certificate SAN lacks ${_name}: ${_san}"
done
openssl x509 -in "${_cert}" -noout -ext basicConstraints 2>/dev/null | grep -q "CA:FALSE" || fail "certificate is not marked CA:FALSE"
[[ -f /etc/apache2/vhosts.d/lab_creation-ssl.conf ]] || fail "HTTPS vhost not installed"
grep -q '^APACHE_MODULES=".*\bssl\b' /etc/sysconfig/apache2 || fail "mod_ssl not enabled in /etc/sysconfig/apache2"

_fp="$(openssl x509 -in "${_cert}" -noout -fingerprint)"
_webui_mode=off _backup=off bash install_automation_node_scripts.sh >/tmp/install2.log 2>&1 || fail "second run failed"
[[ "$(openssl x509 -in "${_cert}" -noout -fingerprint)" == "${_fp}" ]] || fail "second run regenerated the certificate"
[[ "$(grep '^APACHE_MODULES=' /etc/sysconfig/apache2 | grep -o '\bssl\b' | wc -l)" == "1" ]] || fail "second run added ssl to APACHE_MODULES again"

echo provisioning-file > /srv/www/htdocs/lab_creation/install_iso/probe.ks
/usr/sbin/start_apache2 -t >/tmp/configtest.log 2>&1 || { cat /tmp/configtest.log; fail "Apache config test failed"; }
/usr/sbin/start_apache2 -k start >/tmp/apache.log 2>&1 || { cat /tmp/apache.log; fail "Apache did not start"; }
for _i in $(seq 20); do curl -s -o /dev/null http://127.0.0.1/ && break; sleep 0.5; done
[[ "$(curl -s http://127.0.0.1/lab_creation/install_iso/probe.ks)" == "provisioning-file" ]] || fail "HTTP fetch failed"
[[ "$(curl -sk https://127.0.0.1/lab_creation/install_iso/probe.ks)" == "provisioning-file" ]] || fail "HTTPS fetch failed"
curl -s -o /dev/null https://127.0.0.1/lab_creation/install_iso/probe.ks && fail "HTTPS fetch without -k accepted the self-signed certificate"
[[ "$(curl -s --cacert "${_cert}" --resolve test-node:443:127.0.0.1 https://test-node/lab_creation/install_iso/probe.ks)" == "provisioning-file" ]] \
    || fail "HTTPS fetch verifying against the certificate itself failed"
/usr/sbin/start_apache2 -k stop >/dev/null 2>&1

python3.11 tests/checks/70_provisioning_test.py || _fail=1
[[ ${_fail} -eq 0 ]] && echo "all provisioning HTTPS checks passed"
exit ${_fail}
