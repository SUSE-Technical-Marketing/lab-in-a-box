#!/bin/bash
# The lab-builder's own files must be revalidated by browsers, not served from cache across a redeploy: the installer
# enables mod_expires and its Apache drop-in makes every htdocs file expire at once. Runs the real installer, starts
# Apache with the installed config and checks the responses. Independent container — see tests/run_tests.sh.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit
_fail=0
fail() { echo "FAIL: $*"; _fail=1; }

echo test-node > /etc/hostname
_backup=off bash install_automation_node_scripts.sh >/tmp/install.log 2>&1 \
    || { tail -20 /tmp/install.log; fail "install_automation_node_scripts.sh failed"; }

grep -q '^APACHE_MODULES=".*\bexpires\b' /etc/sysconfig/apache2 || fail "mod_expires not enabled in /etc/sysconfig/apache2"
[[ "$(grep '^APACHE_MODULES=' /etc/sysconfig/apache2 | grep -o '\bexpires\b' | wc -l)" == "1" ]] \
    || fail "expires listed twice in APACHE_MODULES"
# "expires" also appears in this file's own "## Default:" comment, so disabling it and running the installer again
# proves the module check reads the APACHE_MODULES line itself and is not satisfied by the comment.
grep -q '^## Default:.*\bexpires\b' /etc/sysconfig/apache2 \
    || echo "note: the Default comment does not list expires on this image; the comment-match case is not exercised"
sed -i 's/^\(APACHE_MODULES="[^"]*\)\bexpires\b/\1/' /etc/sysconfig/apache2
grep -q '^APACHE_MODULES=".*\bexpires\b' /etc/sysconfig/apache2 && fail "could not disable expires for the test"
_backup=off bash install_automation_node_scripts.sh >/tmp/install2.log 2>&1 || fail "second run failed"
grep -q '^APACHE_MODULES=".*\bexpires\b' /etc/sysconfig/apache2 \
    || fail "expires was not re-enabled: the module check is satisfied by the Default comment instead of APACHE_MODULES"

/usr/sbin/start_apache2 -t >/tmp/configtest.log 2>&1 || { cat /tmp/configtest.log; fail "Apache config test failed"; }
/usr/sbin/start_apache2 -k start >/tmp/apache.log 2>&1 || { cat /tmp/apache.log; fail "Apache did not start"; }
for _i in $(seq 20); do curl -s -o /dev/null http://127.0.0.1/ && break; sleep 0.5; done

# The installed config redirects plain HTTP to HTTPS, so the real responses are the HTTPS ones (self-signed cert).
for _f in "" style.css app.js index.html
do
    _url="https://127.0.0.1/lab-builder/${_f}"
    _hdr="$(curl -skI "${_url}")"
    grep -q "^HTTP/[0-9.]* 200" <<<"${_hdr}" || fail "${_url} did not answer 200: $(head -1 <<<"${_hdr}")"
    grep -qi "^cache-control:.*max-age=0" <<<"${_hdr}" || fail "${_url} is not served with max-age=0: $(grep -i cache-control <<<"${_hdr}")"
    grep -qi "^expires:" <<<"${_hdr}" || fail "${_url} carries no Expires header"
done

# The page's own asset URLs carry the deployed version, so a redeploy changes them and a browser cannot reuse a
# cached style.css or app.js with newer markup even if it never revalidates.
_page="$(curl -sk https://127.0.0.1/lab-builder/)"
grep -q '__LABVERSION__' <<<"${_page}" && fail "the deployed page still holds the __LABVERSION__ placeholder"
for _asset in style.css app.js theme.js cube-fx.js
do
    _ref="$(grep -o "${_asset}?v=[^\"']*" <<<"${_page}" | head -1)"
    [[ -n "${_ref}" ]] || fail "the page does not reference ${_asset} with a ?v= version"
    [[ "$(curl -sk -o /dev/null -w '%{http_code}' "https://127.0.0.1/lab-builder/${_ref}")" == "200" ]] \
        || fail "the versioned URL ${_ref} is not served"
done

# Revalidation has to be cheap: an unchanged file answers 304 for a conditional request.
_etag="$(curl -skI https://127.0.0.1/lab-builder/style.css | sed -n 's/^[Ee][Tt]ag: *//p' | tr -d '\r')"
[[ -n "${_etag}" ]] || fail "style.css carries no ETag"
_code="$(curl -sk -o /dev/null -w '%{http_code}' -H "If-None-Match: ${_etag}" https://127.0.0.1/lab-builder/style.css)"
[[ "${_code}" == "304" ]] || fail "a conditional request for an unchanged style.css returned ${_code}, not 304"

# A redeployed file must be served again, not answered 304 from the old validator.
cp /srv/www/lab-builder/htdocs/style.css /tmp/style.css.orig
printf '/* redeployed */\n' >> /srv/www/lab-builder/htdocs/style.css
_code="$(curl -sk -o /dev/null -w '%{http_code}' -H "If-None-Match: ${_etag}" https://127.0.0.1/lab-builder/style.css)"
[[ "${_code}" == "200" ]] || fail "a changed style.css still answered ${_code} to the old ETag"
cp /tmp/style.css.orig /srv/www/lab-builder/htdocs/style.css

/usr/sbin/start_apache2 -k stop >/dev/null 2>&1

if [[ ${_fail} -ne 0 ]]; then
    exit 1
fi
echo "all webui cache-header checks passed"
