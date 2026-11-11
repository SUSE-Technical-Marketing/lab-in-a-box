#!/bin/bash
# Upgrading an automation node deployed before lab_creation-ssl.conf existed. Back then lab-builder-ssl.conf opened
# port 443 itself, and Apache refuses a second Listen on the same port, so a redeploy must replace that file before it
# applies the configuration. Also checks that an invalid Apache configuration is never applied: the deploy stops with
# an error and the running server keeps serving.
# Independent container — see tests/run_tests.sh.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit
_fail=0
fail() { echo "FAIL: $*"; _fail=1; }

echo test-node > /etc/hostname

# The node as an older deploy left it: lab-builder-ssl.conf with its own Listen 443, its certificate, and the
# modules that deploy enabled.
mkdir -p /etc/lab-builder/tls /etc/apache2/vhosts.d
openssl req -x509 -newkey rsa:2048 -nodes -days 1 -subj "/CN=test-node" \
    -keyout /etc/lab-builder/tls/key.pem -out /etc/lab-builder/tls/cert.pem &>/dev/null
cat > /etc/apache2/vhosts.d/lab-builder-ssl.conf <<'EOF'
Listen 443

<VirtualHost *:443>
    SSLEngine on
    SSLCertificateFile    /etc/lab-builder/tls/cert.pem
    SSLCertificateKeyFile /etc/lab-builder/tls/key.pem
</VirtualHost>

<Location /lab-builder>
    RewriteEngine on
    RewriteCond %{HTTPS} off
    RewriteRule ^ https://%{HTTP_HOST}%{REQUEST_URI} [R=301,L]
</Location>
EOF
for _mod in ssl rewrite
do
    sed -i "s/^APACHE_MODULES=\"\(.*\)\"/APACHE_MODULES=\"\1 ${_mod}\"/" /etc/sysconfig/apache2
done

_webui_mode=off _backup=off bash install_automation_node_scripts.sh >/tmp/install.log 2>&1 \
    || { cat /tmp/install.log; fail "redeploy over a node with the old lab-builder-ssl.conf failed"; }
grep -q "^[[:space:]]*Listen" /etc/apache2/vhosts.d/lab-builder-ssl.conf \
    && fail "the old lab-builder-ssl.conf still opens a port of its own"
cmp -s webui/apache/lab-builder-ssl.conf /etc/apache2/vhosts.d/lab-builder-ssl.conf \
    || fail "lab-builder-ssl.conf was not replaced with the current one"
/usr/sbin/start_apache2 -t >/tmp/configtest.log 2>&1 || { cat /tmp/configtest.log; fail "Apache config test failed after the upgrade"; }
/usr/sbin/start_apache2 -k start >/tmp/apache.log 2>&1 || { cat /tmp/apache.log; fail "Apache did not start after the upgrade"; }
for _i in $(seq 20); do curl -s -o /dev/null http://127.0.0.1/ && break; sleep 0.5; done
echo provisioning-file > /srv/www/htdocs/lab_creation/install_iso/probe.ks
[[ "$(curl -sk https://127.0.0.1/lab_creation/install_iso/probe.ks)" == "provisioning-file" ]] \
    || fail "HTTPS fetch failed after the upgrade"

# An invalid configuration: the deploy must refuse to apply it, say why, and leave the running server serving.
echo "ThisIsNotAnApacheDirective on" > /etc/apache2/vhosts.d/zz-broken.conf
echo "# changed" >> /etc/apache2/vhosts.d/lab_creation-ssl.conf     # so the redeploy has a change to apply
if _webui_mode=off _backup=off bash install_automation_node_scripts.sh >/tmp/install2.log 2>&1
then
    fail "the deploy succeeded although the Apache configuration is invalid"
fi
grep -q "Apache configuration is invalid" /tmp/install2.log || { cat /tmp/install2.log; fail "the deploy did not say the configuration is invalid"; }
grep -q "ThisIsNotAnApacheDirective" /tmp/install2.log || fail "the deploy did not show Apache's own error"
[[ "$(curl -sk https://127.0.0.1/lab_creation/install_iso/probe.ks)" == "provisioning-file" ]] \
    || fail "the running Apache stopped serving after a refused deploy"
/usr/sbin/start_apache2 -k stop >/dev/null 2>&1

[[ ${_fail} -eq 0 ]] && echo "all apache legacy-listen checks passed"
exit ${_fail}
