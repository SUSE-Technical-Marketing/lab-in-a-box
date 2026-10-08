#!/usr/bin/env python3
"""URLs and TLS options for the files lab VMs fetch from the automation node.

The automation node serves /srv/www/htdocs over HTTP (port 80) and HTTPS (port 443, self-signed certificate) at the same
time. Clients use HTTPS unless PROVISIONING_BASE_URL says otherwise.

lab_creation.cfg keys:
  PROVISIONING_BASE_URL   : scheme://host[:port] the VMs use for the automation node, e.g. "http://192.168.88.250".
                            Empty (default): https://<host the caller passes>.
  PROVISIONING_TLS_VERIFY : "1" verifies the server certificate. "0" (default) accepts any certificate, so the
                            self-signed one works.
"""

_TRUE = ("1", "true", "yes", "on")

# Kernel arguments that make each installer accept an unverified HTTPS certificate.
_INSTALLER_NO_VERIFY = {
    "autoyast": "ssl.certs=0",
    "kickstart": "inst.noverifyssl",
    "preseed": "debian-installer/allow_unauthenticated_ssl=true",
}


def base_url(host: str, override: str = "") -> str:
    """Return the scheme://host prefix for provisioning URLs: override when set, else https://host."""
    return override.rstrip("/") if override else "https://{}".format(host)


def tls_verify(value: str) -> bool:
    """Return True when a PROVISIONING_TLS_VERIFY value asks for certificate verification."""
    return str(value or "").strip().lower() in _TRUE


def installer_args(install_type: str, url: str, verify: bool) -> str:
    """Return the kernel arguments that point an installer (autoyast/kickstart/preseed) at its answer file url."""
    args = {
        "autoyast": "autoyast={}".format(url),
        # inst.text is a kernel argument, separate from the kickstart's own 'text' directive, which only selects the UI
        # style. Without it Anaconda on RHEL 8 and later starts its graphical/WebUI path, which never completes under
        # --noautoconsole with no display. Anaconda's text UI also queries the terminal's capabilities at startup and
        # blocks until a reply arrives, which never comes under --noautoconsole; TERM=vt100 skips the query. The Debian
        # installer has no vt100 terminfo entry and loops on it, so only kickstart gets TERM=vt100.
        "kickstart": "inst.ks={} inst.sshd inst.text TERM=vt100".format(url),
        "preseed": "auto=true priority=critical url={}".format(url),
    }[install_type]
    if url.startswith("https://") and not verify:
        args += " " + _INSTALLER_NO_VERIFY[install_type]
    return args


def curl_tls_option(url: str, verify: bool) -> str:
    """Return the curl option for url: "-k" for HTTPS without verification, else ""."""
    return "-k" if url.startswith("https://") and not verify else ""
