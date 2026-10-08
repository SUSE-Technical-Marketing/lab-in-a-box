#!/bin/bash
# Part of lab-in-a-box, install the automation node scripts in their respective paths, etc..
# Author/s: Raul Mahiques
# License: GPLv3
#
#  This program is free software: you can redistribute it and/or modify it under the terms of the GNU General Public License as published by the Free Software Foundation, either version 3 of the License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License along with this program. If not, see <https://www.gnu.org/licenses/gpl-3.0.html>.




if [[ "$_scripts_path" != "" ]]
then
	cd "$_scripts_path" || exit
fi

# Let's do a backup first (_backup=off skips it — for callers such as an
# automated re-install on every deploy, which would otherwise leave one
# tarball per run in ~).
if [[ "${_backup:-on}" != "off" ]]
then
_timestamp="$(date +%s)"
tar --ignore-failed-read -cJf ~/"backups-install_automation_node_scripts-${_timestamp}.tar.xz" \
  /usr/local/lib/lab_creation/ \
  /usr/local/bin/ \
  /usr/share/lab_creation/templates/addons/ \
  /etc/lab_creation* \
  /srv/www/htdocs/lab_creation/ \
  /srv/www/lab-builder/
fi

# scripts/ and libs/ Python files are pinned to python3.11 explicitly (the
# automation VM's default `python3` is 3.6, too old for this codebase) —
# refuse to deploy rather than install scripts that can't run.
# _python_bin overrides the interpreter on hosts that ship a newer Python but
# no python3.11 (e.g. SLES 16: python3.13) — the installed scripts' shebangs
# are rewritten to it below. Default unchanged: python3.11.
_python_bin="${_python_bin:-python3.11}"
if ! command -v "${_python_bin}" >/dev/null 2>&1
then
	echo "ERROR: ${_python_bin} is required (this project's Python code is pinned to python3.11; set _python_bin to use another interpreter) but was not found on PATH." >&2
	exit 1
fi

# Prints the short hash of the last commit that changed path $1 (relative to the repository root): from git, else from
# .lab-versions ("<hash> <path>" per line, written by setup_lab_automation.sh when it copies the tree without .git),
# else "unknown".
lab_version() {
	local _v
	_v=$(git log -1 --format='%h' -- "$1" 2>/dev/null)
	[[ -z "${_v}" && -f .lab-versions ]] && _v=$(awk -v p="$1" '$2 == p {print $1; exit}' .lab-versions)
	echo "${_v:-unknown}"
}

if [[ "$_templ_addons_loc" == "" ]]
then
	_templ_addons_loc=/usr/share/lab_creation/templates/addons/
fi


# create directories
mkdir -p /srv/www/htdocs/lab_creation/{combustion,ignition,cloud-init,salt,install_iso} ${_templ_addons_loc} /usr/local/lib/lab_creation/ &>/dev/null
# Explicit, not left to umask: confirmed live 2026-08-30 that this
# directory ended up 0700 on a real deployment, silently breaking the
# webui's Apache-mode CGI (runs as wwwrun, not root) for every request that
# imports anything from it (primary/api/discovery/apps/layers) —
# "ModuleNotFoundError: No module named 'apps'" even though the file was
# right there and world-readable itself, because wwwrun couldn't even
# traverse the directory to find it. Holds no secrets, just library code —
# safe to be world-readable/traversable regardless of the caller's umask.
chmod 0755 /usr/local/lib/lab_creation/

cp lab_creation.defaults /etc/lab_creation.defaults
chmod 0600 /etc/lab_creation.defaults


cp templates/lab_creation.cfg.example /etc/lab_creation.cfg.example
chmod 0600 /etc/lab_creation.cfg.example

# Shared core libs: libs/ holds the active Python modules (multi-KVM-host
# selection, pluggable VM backends, spacecmd_common.py, etc.) — one
# directory, one loop. Used to also carry the bash helpers install_ds389
# needed before it was ported to Python (2026-09-21); those were removed
# once nothing live sourced them any more (see legacy_bash/README.md).
for i in libs/*
do
    [[ -d "${i}" ]] && continue
    cp "${i}" /usr/local/lib/lab_creation/
done

cp -r  templates/addons/* ${_templ_addons_loc}/


# Add-ons: every install_<name> executable under scripts/, in any language,
# with or without a file extension. The extension is stripped, so each lands
# at /usr/local/bin/install_<name>, the name setup_lab.py's dispatch and the
# webui's discovery look up. When install_<name> exists both with and without
# an extension, the last in sorted order is deployed (libs/apps.addon_files()
# picks the same one). The version placeholder is stamped into text files only.
for i in scripts/install_*
do
        [[ -f "${i}" ]] || continue
        _name="${i##*/}"
        _name="${_name%%.*}"
        _dst="/usr/local/bin/${_name}"
        cp "${i}" "${_dst}"
        if grep -Iq "__LABVERSION__" "${_dst}"
        then
            sed -i "s/__LABVERSION__/$(lab_version "${i}")/" "${_dst}"
        fi
        chmod 0755 "${_dst}"
done

# Orchestration: setup_lab.py/setup_vm.py/destroy_vm.py/destroy_lab.py. The
# .py suffix is KEPT here (unlike install_<addon> above): setup_lab.py does
# plain top-level `from destroy_vm import destroy_vm` / `from setup_vm
# import provision_vm`, which only resolves because these exact filenames
# exist alongside it.
for i in setup_lab.py setup_vm.py destroy_vm.py destroy_lab.py
do
    cp "scripts/${i}" "/usr/local/bin/${i}"
    sed -i "s/__LABVERSION__/$(lab_version "scripts/${i}")/" "/usr/local/bin/${i}"
    chmod 0755 "/usr/local/bin/${i}"
done

# Non-addon, non-orchestration tooling.
for i in pushDockerImage.sh lab_schema refresh_hypervisor_status.py setup_harvester_cluster.py build_lab_usb.py setup_credentials.py vm_power.py
do
    cp "scripts/${i}" "/usr/local/bin/${i}"
    sed -i "s/__LABVERSION__/$(lab_version "scripts/${i}")/" "/usr/local/bin/${i}"
    chmod 0755 "/usr/local/bin/${i}"
done

# The lab-builder's root-side tools: lab-builder-helper (run by the web UI
# through sudo, see the webui section below) and lab-builder-passwd (logins).
cp scripts/lab_builder_helper.py /usr/local/sbin/lab-builder-helper
sed -i "s/__LABVERSION__/$(lab_version scripts/lab_builder_helper.py)/" /usr/local/sbin/lab-builder-helper
cp scripts/lab-builder-passwd /usr/local/sbin/lab-builder-passwd
chown root:root /usr/local/sbin/lab-builder-helper /usr/local/sbin/lab-builder-passwd
chmod 0755 /usr/local/sbin/lab-builder-helper /usr/local/sbin/lab-builder-passwd

# Point the installed scripts at _python_bin when it isn't the default.
if [[ "${_python_bin}" != "python3.11" ]]
then
    for i in /usr/local/bin/install_* /usr/local/bin/setup_lab.py /usr/local/bin/setup_vm.py \
             /usr/local/bin/destroy_vm.py /usr/local/bin/destroy_lab.py /usr/local/bin/lab_schema \
             /usr/local/bin/refresh_hypervisor_status.py /usr/local/bin/setup_harvester_cluster.py \
             /usr/local/bin/build_lab_usb.py /usr/local/bin/setup_credentials.py /usr/local/bin/vm_power.py \
             /usr/local/sbin/lab-builder-helper
    do
        [[ -f "${i}" ]] && grep -Iq . "${i}" && sed -i "1s|^#!/usr/bin/env python3.11\$|#!/usr/bin/env ${_python_bin}|" "${i}"
    done
fi

# Old bash orchestration binaries are fully superseded by the ones installed
# above — remove them so nothing can ever dispatch to a stale copy.
rm -f /usr/local/bin/setup_lab.sh /usr/local/bin/setup_vm.sh /usr/local/bin/destroy_vm.sh /usr/local/bin/destroy_lab.sh

# The libs/* loop above only copies what's currently in source — it never
# prunes a file that used to be there. lab_creation.bash/k8s_functions.bash/
# primary_functions.bash were removed from libs/ once install_ds389 (their
# last consumer) was ported to Python (2026-09-21); explicitly remove any
# already-deployed copies too, so a VM last deployed before this change
# doesn't keep carrying dead weight forever.
rm -f /usr/local/lib/lab_creation/lab_creation.bash /usr/local/lib/lab_creation/k8s_functions.bash /usr/local/lib/lab_creation/primary_functions.bash


for i in templates/salt/*
do
  cp $i /srv/www/htdocs/lab_creation/salt/
done

for i in combustion.template ignition.template cloud-init.template_meta-data cloud-init.template_network-config cloud-init.template_network-config-dhcp cloud-init.template_network-config-dhcp-nomac cloud-init.template_user-data \
          install_iso.template_autoyast install_iso.template_kickstart install_iso.template_preseed install_iso.template_autoinstall
do
  cp templates/${i} /srv/www/htdocs/lab_creation/${i//./\/}
done



# ── HTTPS for the provisioning web server ────────────────────────────────────
# Apache serves /srv/www/htdocs (provisioning files, helm, lab-builder) on port
# 80 and, through templates/apache/lab_creation-ssl.conf, on port 443 with a
# self-signed certificate. The certificate is generated once at
# /etc/lab_creation/tls/{cert,key}.pem and never regenerated while present; an
# existing /etc/lab-builder/tls certificate is reused. Its names are the
# hostname from /etc/hostname plus the addresses in _tls_ips (default: the
# output of `hostname -I`). Lab VMs use HTTPS without verifying it by default,
# see libs/provisioning.py.
_tls_dir=/etc/lab_creation/tls
_tls_cert="${_tls_dir}/cert.pem"
_tls_key="${_tls_dir}/key.pem"
mkdir -p "${_tls_dir}"
if [[ ! -f "${_tls_cert}" || ! -f "${_tls_key}" ]]
then
    if [[ -f /etc/lab-builder/tls/cert.pem && -f /etc/lab-builder/tls/key.pem ]]
    then
        cp /etc/lab-builder/tls/cert.pem "${_tls_cert}"
        cp /etc/lab-builder/tls/key.pem "${_tls_key}"
    elif command -v openssl &>/dev/null
    then
        _tls_name="$(cat /etc/hostname 2>/dev/null)"
        _tls_name="${_tls_name:-$(hostname -f 2>/dev/null || hostname)}"
        _tls_san="DNS:${_tls_name},DNS:${_tls_name%%.*}"
        for _ip in ${_tls_ips:-$(hostname -I 2>/dev/null)}
        do
            _tls_san="${_tls_san},IP:${_ip}"
        done
        openssl req -x509 -newkey rsa:2048 -nodes -days 3650 \
            -subj "/CN=${_tls_name}" -addext "subjectAltName=${_tls_san}" \
            -addext "basicConstraints=critical,CA:FALSE" -addext "extendedKeyUsage=serverAuth" \
            -keyout "${_tls_key}" -out "${_tls_cert}" &>/dev/null
    else
        echo "ERROR: openssl is required to generate the HTTPS certificate in ${_tls_dir}" >&2
        exit 1
    fi
fi
chmod 0600 "${_tls_key}"
chmod 0644 "${_tls_cert}"

if [[ -d /etc/apache2/vhosts.d ]]          # SLES / openSUSE
then
    cp templates/apache/lab_creation-ssl.conf /etc/apache2/vhosts.d/lab_creation-ssl.conf
    if ! grep -q '^APACHE_MODULES=".*\bssl\b' /etc/sysconfig/apache2 2>/dev/null
    then
        sed -i 's/^APACHE_MODULES="\(.*\)"/APACHE_MODULES="\1 ssl"/' /etc/sysconfig/apache2
    fi
    systemctl try-restart apache2 2>/dev/null || true
elif [[ -d /etc/httpd/conf.d ]]            # RHEL family (needs the mod_ssl package)
then
    if [[ -f /etc/httpd/conf.d/ssl.conf ]]
    then
        sed '/^<IfDefine !SSL>$/,/^<\/IfDefine>$/d' templates/apache/lab_creation-ssl.conf > /etc/httpd/conf.d/lab_creation-ssl.conf
    else
        cp templates/apache/lab_creation-ssl.conf /etc/httpd/conf.d/lab_creation-ssl.conf
    fi
    systemctl try-restart httpd 2>/dev/null || true
fi


# ── lab-builder web UI ────────────────────────────────────────────────────────
# Static single-page app + one CGI endpoint, normally served by Apache at
# /lab-builder/. Auto-detects the scripts (/usr/local/bin) and libs
# (/usr/local/lib/lab_creation) installed above; saved labs go under
# /srv/www/lab-builder/labs.
#
# _webui_mode selects how (or whether) it's deployed:
#   apache   (default, unchanged from before) — Apache + mod_cgi, as above.
#   service  — runs webui/run-local.py (the zero-dependency stdlib server,
#              no Apache/CGI needed at all) as a persistent background
#              process. Managed via systemd when the target has it
#              (checked by the presence of /run/systemd/system — the
#              standard, documented way to detect a systemd-booted system —
#              not just by whether a `systemctl` binary happens to exist);
#              otherwise via a small init-system-independent control script
#              (/usr/local/bin/lab-builder-ctl, start/stop/restart/status
#              over a plain PID file). Either way the actual serving process
#              is the same run-local.py; only how it's started/stopped
#              differs.
#   off      — skip webui deployment entirely.
# _webui_port — port for "service" mode only (default 8677).
# _webui_tls  — HTTPS by default ("1", the default) or plain HTTP only ("0").
#               When on, the lab-builder uses the certificate generated above
#               (/etc/lab_creation/tls), and Apache mode redirects
#               http://<automation-vm>/lab-builder/ to HTTPS. Self-signed means
#               browsers warn on the first visit.
if [[ "${_webui_mode:-apache}" != "off" && -d webui ]]
then
    _lb_root=/srv/www/lab-builder
    mkdir -p "${_lb_root}/labs" &>/dev/null

    _lb_tls_cert=""
    _lb_tls_key=""
    if [[ "${_webui_tls:-1}" != "0" ]]
    then
        _lb_tls_cert="${_tls_cert}"
        _lb_tls_key="${_tls_key}"
    fi
    _lb_scheme="http"
    [[ -n "${_lb_tls_cert}" ]] && _lb_scheme="https"

    # refresh the app dirs (idempotent — keeps existing labs/ intact)
    rm -rf "${_lb_root:?}/htdocs" "${_lb_root:?}/cgi-bin" "${_lb_root:?}/lib"
    cp -r webui/htdocs webui/cgi-bin webui/lib "${_lb_root}/"
    cp webui/run-local.py "${_lb_root}/"
    cp webui/README.md "${_lb_root}/" 2>/dev/null || true
    chmod 0755 "${_lb_root}/cgi-bin/labbuilder.py" "${_lb_root}/run-local.py"

    # version-stamp the deployed files (same as the scripts above)
    _lb_ver=$(lab_version webui)
    grep -rl '__LABVERSION__' "${_lb_root}" 2>/dev/null | while read -r _f
    do
        sed -i "s/__LABVERSION__/${_lb_ver}/g" "${_f}"
    done

    # Logins for the server actions (Save to server, saved labs, credentials,
    # Create lab): /etc/lab-builder/htpasswd, managed with lab-builder-passwd.
    # It starts empty, so those actions stay locked until a user is added.
    # The web server's group reads it (Apache Basic auth and the CGI).
    _lb_web_user=""
    for _u in wwwrun apache www-data
    do
        id "${_u}" &>/dev/null && { _lb_web_user="${_u}"; break; }
    done
    _lb_web_group=root
    if [[ "${_webui_mode:-apache}" != "service" && -n "${_lb_web_user}" ]]
    then
        # The group Apache runs as (SLES: www in uid.conf), else the user's own group.
        _lb_web_group=$(awk '$1 == "Group" {print $2; exit}' /etc/apache2/uid.conf /etc/httpd/conf/httpd.conf 2>/dev/null)
        [[ -n "${_lb_web_group}" ]] || _lb_web_group=$(id -gn "${_lb_web_user}")
    fi
    install -d -m 0750 -o root -g "${_lb_web_group}" /etc/lab-builder
    [[ -f /etc/lab-builder/htpasswd ]] || install -m 0640 -o root -g "${_lb_web_group}" /dev/null /etc/lab-builder/htpasswd
    chgrp "${_lb_web_group}" /etc/lab-builder/htpasswd
    install -d -m 0700 -o root -g root /var/lib/lab-builder /var/lib/lab-builder/jobs

    if [[ "${_webui_mode:-apache}" == "service" ]]
    then
        _lb_port="${_webui_port:-8677}"

        # Control script: does the actual start/stop/restart/status work,
        # independent of any init system. This is what always runs the
        # server; systemd (when present) just becomes another caller of it.
        cat > /usr/local/bin/lab-builder-ctl << CTLEOF
#!/bin/bash
# Start/stop/restart/status for the lab-builder web UI's standalone server.
# Used directly on systems with no systemd; on systemd systems the
# lab-builder.service unit runs run-local.py itself (see below) and this
# script is kept only as a manual fallback/diagnostic tool.
_lb_root="${_lb_root}"
_lb_port="${_lb_port}"
_lb_scheme="${_lb_scheme}"
export LABBUILDER_TLS_CERT="${_lb_tls_cert}"
export LABBUILDER_TLS_KEY="${_lb_tls_key}"
export LABBUILDER_OUTPUT_DIR="\${_lb_root}/labs"
_lb_pidfile=/run/lab-builder.pid
_lb_logfile=/var/log/lab-builder.log

case "\$1" in
  start)
    if [[ -f "\$_lb_pidfile" ]] && kill -0 "\$(cat "\$_lb_pidfile")" 2>/dev/null
    then
        echo "lab-builder already running (pid \$(cat "\$_lb_pidfile"))"
        exit 0
    fi
    nohup python3 "\${_lb_root}/run-local.py" "\${_lb_port}" >>"\$_lb_logfile" 2>&1 &
    echo \$! > "\$_lb_pidfile"
    echo "lab-builder started (pid \$!) -> \${_lb_scheme}://<automation-vm>:\${_lb_port}/"
    ;;
  stop)
    if [[ -f "\$_lb_pidfile" ]]
    then
        kill "\$(cat "\$_lb_pidfile")" 2>/dev/null
        rm -f "\$_lb_pidfile"
        echo "lab-builder stopped"
    else
        echo "lab-builder is not running (no pidfile)"
    fi
    ;;
  restart)
    "\$0" stop
    sleep 1
    "\$0" start
    ;;
  status)
    if [[ -f "\$_lb_pidfile" ]] && kill -0 "\$(cat "\$_lb_pidfile")" 2>/dev/null
    then
        echo "lab-builder running (pid \$(cat "\$_lb_pidfile"))"
    else
        echo "lab-builder not running"
        exit 1
    fi
    ;;
  *)
    echo "Usage: \$0 {start|stop|restart|status}"
    exit 1
    ;;
esac
CTLEOF
        chmod 0755 /usr/local/bin/lab-builder-ctl

        if [[ -d /run/systemd/system ]]
        then
            # systemd present: run run-local.py directly under systemd
            # (native, gets proper logging/Restart=/boot persistence) rather
            # than wrapping the control script.
            cat > /etc/systemd/system/lab-builder.service << UNITEOF
[Unit]
Description=lab-builder web UI (standalone, no Apache)
After=network.target

[Service]
Type=simple
Environment=LABBUILDER_TLS_CERT=${_lb_tls_cert}
Environment=LABBUILDER_TLS_KEY=${_lb_tls_key}
Environment=LABBUILDER_OUTPUT_DIR=${_lb_root}/labs
ExecStart=/usr/bin/env python3 ${_lb_root}/run-local.py ${_lb_port}
Restart=always
RestartSec=2

[Install]
WantedBy=multi-user.target
UNITEOF
            systemctl daemon-reload
            systemctl enable --now lab-builder.service
            echo "lab-builder web UI installed as a systemd service -> ${_lb_scheme}://<automation-vm>:${_lb_port}/"
            echo "  manage it with: systemctl {start|stop|restart|status} lab-builder"
        else
            # No systemd on this target — start it now via the plain
            # control script. There is no single reliable way to hook every
            # possible non-systemd init system for boot-persistence, so this
            # only starts it for the current session; re-run
            # 'lab-builder-ctl start' (e.g. from whatever startup mechanism
            # this system does use) to have it survive a reboot.
            /usr/local/bin/lab-builder-ctl start
            echo "  no systemd detected — managed via: lab-builder-ctl {start|stop|restart|status}"
            echo "  (add that to your system's own boot mechanism for it to survive a reboot)"
        fi
    else
        # Apache (wwwrun) must be able to write generated labs
        chown -R wwwrun "${_lb_root}/labs" 2>/dev/null || true

        # The CGI runs lab-builder-helper as root through sudo, and nothing else.
        if [[ -n "${_lb_web_user}" ]] && command -v visudo &>/dev/null
        then
            printf '%s ALL=(root) NOPASSWD: /usr/local/sbin/lab-builder-helper\n' "${_lb_web_user}" > /etc/sudoers.d/lab-builder.new
            chmod 0440 /etc/sudoers.d/lab-builder.new
            if visudo -cf /etc/sudoers.d/lab-builder.new &>/dev/null
            then
                mv -f /etc/sudoers.d/lab-builder.new /etc/sudoers.d/lab-builder
            else
                rm -f /etc/sudoers.d/lab-builder.new
                echo "WARNING: sudoers rule for lab-builder-helper rejected by visudo; credentials and Create lab will not work" >&2
            fi
        else
            echo "WARNING: no web server user or no sudo; the lab-builder's credentials and Create lab will not work" >&2
        fi

        if [[ -d /etc/apache2/vhosts.d ]]          # SLES / openSUSE
        then
            cp webui/apache/lab-builder.conf /etc/apache2/vhosts.d/lab-builder.conf
            # enable the modules this needs if they aren't already: cgid
            # always, rewrite for the HTTPS redirect unless _webui_tls=0.
            _needed_modules="cgid auth_basic authn_file authz_user"
            if [[ -n "${_lb_tls_cert}" ]]
            then
                cp webui/apache/lab-builder-ssl.conf /etc/apache2/vhosts.d/lab-builder-ssl.conf
                _needed_modules="${_needed_modules} rewrite"
            else
                rm -f /etc/apache2/vhosts.d/lab-builder-ssl.conf
            fi
            for _mod in ${_needed_modules}
            do
                if ! grep -q "${_mod}" /etc/sysconfig/apache2 2>/dev/null
                then
                    sed -i "s/^APACHE_MODULES=\"\(.*\)\"/APACHE_MODULES=\"\1 ${_mod}\"/" /etc/sysconfig/apache2
                fi
            done
            # a restart (not just reload) is needed when new modules/Listen
            # directives are added, which is only sometimes the case here —
            # always restarting is simplest and safe (this is the automation
            # VM's own webui, not a production service with connections to
            # drain).
            systemctl restart apache2 2>/dev/null || true
            echo "lab-builder web UI installed -> ${_lb_scheme}://<automation-vm>/lab-builder/"
            echo "  add a login for its server actions with: lab-builder-passwd <user>"
            [[ -n "${_lb_tls_cert}" ]] && echo "  (also reachable, and redirected to, from http://<automation-vm>/lab-builder/ — self-signed cert, browsers will warn once)"
        elif [[ -d /etc/httpd/conf.d ]]            # RHEL family
        then
            cp webui/apache/lab-builder.conf /etc/httpd/conf.d/lab-builder.conf
            if [[ -n "${_lb_tls_cert}" ]]
            then
                cp webui/apache/lab-builder-ssl.conf /etc/httpd/conf.d/lab-builder-ssl.conf
            else
                rm -f /etc/httpd/conf.d/lab-builder-ssl.conf
            fi
            systemctl restart httpd 2>/dev/null || true
            echo "lab-builder web UI installed -> ${_lb_scheme}://<automation-vm>/lab-builder/"
        else
            echo "webui copied to ${_lb_root}, but no Apache config dir found; configure manually (see README.webui.md), or set _webui_mode=service to skip Apache entirely"
        fi
    fi

    # ── hypervisor status snapshot (feeds the status panel + live ISO_IMAGE
    # dropdown) ──────────────────────────────────────────────────────────────
    # refresh_hypervisor_status.py runs as root on a schedule — unlike the
    # CGI (which never runs anything privileged, see lab-builder.conf's
    # header), it SSHes to the hypervisor using root's own key to build a
    # non-secret JSON snapshot at ${_lb_root}/status.json; the webui only
    # ever reads that file. One synchronous run now so the panel isn't empty
    # immediately after install; systemd timer when available, else cron.d.
    if [[ -x /usr/local/bin/refresh_hypervisor_status.py ]]
    then
        LABBUILDER_STATUS_FILE="${_lb_root}/status.json" /usr/local/bin/refresh_hypervisor_status.py \
            || echo "warning: initial hypervisor-status refresh failed (webui status panel will be empty until it succeeds — check /etc/lab_creation.cfg and SSH access to the hypervisor)"

        if [[ -d /run/systemd/system ]]
        then
            cat > /etc/systemd/system/lab-builder-status.service << STATUSEOF
[Unit]
Description=Refresh lab-builder's hypervisor status snapshot

[Service]
Type=oneshot
Environment=LABBUILDER_STATUS_FILE=${_lb_root}/status.json
ExecStart=/usr/bin/env python3 /usr/local/bin/refresh_hypervisor_status.py
STATUSEOF
            cat > /etc/systemd/system/lab-builder-status.timer << STATUSEOF
[Unit]
Description=Periodic refresh of lab-builder's hypervisor status snapshot

[Timer]
OnBootSec=1min
OnUnitActiveSec=5min

[Install]
WantedBy=timers.target
STATUSEOF
            systemctl daemon-reload
            systemctl enable --now lab-builder-status.timer
        elif [[ -d /etc/cron.d ]]
        then
            echo "*/5 * * * * root LABBUILDER_STATUS_FILE=${_lb_root}/status.json /usr/bin/env python3 /usr/local/bin/refresh_hypervisor_status.py" \
                > /etc/cron.d/lab-builder-status
            echo "  hypervisor status refresh scheduled via cron.d (every 5 min)"
        else
            echo "  no systemd or cron.d found — run 'refresh_hypervisor_status.py' manually/periodically to keep the webui status panel current"
        fi
    fi
fi


# ── MCP (Model Context Protocol) endpoint ──────────────────────────────────────
# Lets an MCP client (an LLM agent) drive lab-in-a-box. Off by default
# (_mcp_mode:-off) — this is real new capability that can trigger real
# deploy/destroy once MCP_ALLOW_MUTATIONS is also set in lab_creation.cfg,
# so it isn't turned on for every automation VM just because this script
# ran. Runs as its own root-owned process directly on this host — NOT
# containerized (that was the original design, reverted after live-testing
# on 2026-08-29: its mutating tools need full, unsandboxed host access —
# virsh/virt-install, and DNS record management, which reads/writes BIND's
# local zone files and restarts the local named service directly, with no
# remote/SSH equivalent since BIND already runs on this same host — see
# mcp_server.py's own module docstring). Separate from the webui's
# Apache-user CGI regardless (see mcp_server.py's docstring for why that
# separation matters). Its own third-party Python deps (mcp/uvicorn — the
# only third-party deps anywhere in this otherwise stdlib-only project)
# live in a dedicated venv so they stay isolated from the rest of the
# system's Python environment even without a container.
# _mcp_mode  — "off" (default) or "on".
if [[ "${_mcp_mode:-off}" != "off" && -d mcp ]]
then
    if ! command -v python3.11 &>/dev/null
    then
        echo "python3.11 not found — skipping the MCP endpoint (install python3.11 and re-run with _mcp_mode=on to enable it)"
    else
        # mTLS CA + server cert, generated once (idempotent — never
        # regenerated if already present), mirroring the webui's own
        # self-signed-cert pattern above. Client certs are issued on demand
        # via lab-mcp-issue-client-cert, signed by this same CA.
        mkdir -p /etc/lab-mcp/tls
        if [[ ! -f /etc/lab-mcp/tls/ca.crt || ! -f /etc/lab-mcp/tls/ca.key ]]
        then
            if command -v openssl &>/dev/null
            then
                openssl req -x509 -newkey rsa:2048 -nodes -days 3650 \
                    -subj "/CN=lab-mcp-ca" \
                    -keyout /etc/lab-mcp/tls/ca.key \
                    -out /etc/lab-mcp/tls/ca.crt &>/dev/null
                chmod 0600 /etc/lab-mcp/tls/ca.key
                chmod 0644 /etc/lab-mcp/tls/ca.crt
            else
                echo "openssl not found — cannot generate the MCP CA; skipping the MCP endpoint"
            fi
        fi
        if [[ -f /etc/lab-mcp/tls/ca.crt && -f /etc/lab-mcp/tls/ca.key \
              && ( ! -f /etc/lab-mcp/tls/server.crt || ! -f /etc/lab-mcp/tls/server.key ) ]]
        then
            openssl req -newkey rsa:2048 -nodes \
                -subj "/CN=$(hostname -f 2>/dev/null || hostname)" \
                -keyout /etc/lab-mcp/tls/server.key \
                -out /tmp/lab-mcp-server.csr &>/dev/null
            openssl x509 -req -in /tmp/lab-mcp-server.csr \
                -CA /etc/lab-mcp/tls/ca.crt -CAkey /etc/lab-mcp/tls/ca.key -CAcreateserial \
                -days 3650 -out /etc/lab-mcp/tls/server.crt &>/dev/null
            rm -f /tmp/lab-mcp-server.csr
            chmod 0600 /etc/lab-mcp/tls/server.key
            chmod 0644 /etc/lab-mcp/tls/server.crt
        fi

        if [[ -f /etc/lab-mcp/tls/ca.crt && -f /etc/lab-mcp/tls/server.crt ]]
        then
            cp mcp/lab-mcp-issue-client-cert /usr/local/bin/
            chmod 0755 /usr/local/bin/lab-mcp-issue-client-cert

            mkdir -p /var/log/lab-mcp /root/.lab-builder/labs

            # Dedicated venv for mcp/uvicorn — created once, reused after
            # (idempotent, mirrors the webui/DNS cert-generation pattern
            # above: skip work already done rather than redo it every run).
            if [[ ! -d /etc/lab-mcp/venv ]]
            then
                python3.11 -m venv /etc/lab-mcp/venv
            fi
            /etc/lab-mcp/venv/bin/pip install --quiet "mcp==2.1.1" uvicorn \
                || echo "pip install failed in /etc/lab-mcp/venv — MCP endpoint not deployed"

            if [[ -x /etc/lab-mcp/venv/bin/python3.11 ]]
            then
                mkdir -p /usr/local/lib/lab_creation
                cp mcp/mcp_server.py /usr/local/lib/lab_creation/mcp_server.py
                chmod 0755 /usr/local/lib/lab_creation/mcp_server.py
                sed -i "s/__LABVERSION__/$(lab_version mcp/mcp_server.py)/" \
                    /usr/local/lib/lab_creation/mcp_server.py

                if [[ -d /run/systemd/system ]]
                then
                    cat > /etc/systemd/system/lab-mcp.service << UNITEOF
[Unit]
Description=lab-in-a-box MCP (Model Context Protocol) endpoint
After=network.target named.service

[Service]
Type=simple
ExecStart=/etc/lab-mcp/venv/bin/python3.11 /usr/local/lib/lab_creation/mcp_server.py
Restart=always
RestartSec=2

[Install]
WantedBy=multi-user.target
UNITEOF
                    systemctl daemon-reload
                    systemctl enable lab-mcp.service &>/dev/null
                    systemctl restart lab-mcp.service \
                        || echo "failed to start lab-mcp.service — check 'journalctl -u lab-mcp.service'"
                else
                    # No systemd: same plain-control-script fallback pattern
                    # as lab-builder-ctl above — starts it for this session
                    # only; re-run 'lab-mcp-ctl start' from whatever this
                    # system's own boot mechanism is for it to survive a
                    # reboot.
                    cat > /usr/local/bin/lab-mcp-ctl << CTLEOF
#!/bin/bash
# Start/stop/restart/status for the MCP endpoint's standalone server.
_mcp_pidfile=/run/lab-mcp.pid
_mcp_logfile=/var/log/lab-mcp/server.log

case "\$1" in
  start)
    if [[ -f "\$_mcp_pidfile" ]] && kill -0 "\$(cat "\$_mcp_pidfile")" 2>/dev/null
    then
        echo "lab-mcp already running (pid \$(cat "\$_mcp_pidfile"))"
        exit 0
    fi
    nohup /etc/lab-mcp/venv/bin/python3.11 /usr/local/lib/lab_creation/mcp_server.py >>"\$_mcp_logfile" 2>&1 &
    echo \$! > "\$_mcp_pidfile"
    echo "lab-mcp started (pid \$!)"
    ;;
  stop)
    if [[ -f "\$_mcp_pidfile" ]]
    then
        kill "\$(cat "\$_mcp_pidfile")" 2>/dev/null
        rm -f "\$_mcp_pidfile"
        echo "lab-mcp stopped"
    else
        echo "lab-mcp is not running (no pidfile)"
    fi
    ;;
  restart)
    "\$0" stop
    sleep 1
    "\$0" start
    ;;
  status)
    if [[ -f "\$_mcp_pidfile" ]] && kill -0 "\$(cat "\$_mcp_pidfile")" 2>/dev/null
    then
        echo "lab-mcp running (pid \$(cat "\$_mcp_pidfile"))"
    else
        echo "lab-mcp not running"
        exit 1
    fi
    ;;
  *)
    echo "Usage: \$0 {start|stop|restart|status}"
    exit 1
    ;;
esac
CTLEOF
                    chmod 0755 /usr/local/bin/lab-mcp-ctl
                    /usr/local/bin/lab-mcp-ctl restart
                    echo "  no systemd detected — managed via: lab-mcp-ctl {start|stop|restart|status}"
                    echo "  (add that to your system's own boot mechanism for it to survive a reboot)"
                fi
                echo "  MCP endpoint listening (mTLS) — issue a client cert with: lab-mcp-issue-client-cert <name>"
                echo "  Mutating tools (deploy/rebuild/destroy) stay disabled until MCP_ALLOW_MUTATIONS=true is set in /etc/lab_creation.cfg"
            fi
        fi
    fi
fi
