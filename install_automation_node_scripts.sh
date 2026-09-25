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

# Let's do a backup first
_timestamp="$(date +%s)"
tar --ignore-failed-read -cJf ~/"backups-install_automation_node_scripts-${_timestamp}.tar.xz" \
  /usr/local/lib/lab_creation/ \
  /usr/local/bin/ \
  /usr/share/lab_creation/templates/addons/ \
  /etc/lab_creation* \
  /srv/www/htdocs/lab_creation/ \
  /srv/www/lab-builder/

# scripts/ and libs/ Python files are pinned to python3.11 explicitly (the
# automation VM's default `python3` is 3.6, too old for this codebase) —
# refuse to deploy rather than install scripts that can't run.
if ! command -v python3.11 >/dev/null 2>&1
then
	echo "ERROR: python3.11 is required (this project's Python code is pinned to it) but was not found on PATH." >&2
	exit 1
fi

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


# Addons: every install_<name>[.py] under scripts/ — including
# install_smlm/install_uyuni (their shared spacecmd_common.py is fully
# mocked-SSH tested — tests/checks/09_spacecmd_common_test.py — but still
# has no live SMLM/Uyuni server validation; see MIGRATION_TODO.md "Open
# Risk #1" before trusting this in production). install_ds389 is now a
# real Python addon too (2026-09-21) — the bash original that used to live
# here, with no .py suffix to strip, is archived under legacy_bash/.
# .py suffix stripped when present so each lands under the exact name
# setup_lab.py's addon dispatch and the webui's discovery already look up
# (both are name/exec-based, not shebang- or extension-aware).
for i in scripts/install_*
do
        _name="${i##*/}"
        _name="${_name%.py}"
        _dst="/usr/local/bin/${_name}"
        cp "${i}" "${_dst}"
        sed -i "s/__LABVERSION__/$(git log -1 --format='%h' -- ${i} 2>/dev/null || echo 'unknown')/" "${_dst}"
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
    sed -i "s/__LABVERSION__/$(git log -1 --format='%h' -- scripts/${i} 2>/dev/null || echo 'unknown')/" "/usr/local/bin/${i}"
    chmod 0755 "/usr/local/bin/${i}"
done

# Non-addon, non-orchestration tooling.
for i in pushDockerImage.sh lab_schema refresh_hypervisor_status.py setup_harvester_cluster.py build_lab_usb.py setup_credentials.py
do
    cp "scripts/${i}" "/usr/local/bin/${i}"
    sed -i "s/__LABVERSION__/$(git log -1 --format='%h' -- scripts/${i} 2>/dev/null || echo 'unknown')/" "/usr/local/bin/${i}"
    chmod 0755 "/usr/local/bin/${i}"
done

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
#               When on, a self-signed cert/key is generated once (idempotent
#               — never regenerated if already present) at
#               /etc/lab-builder/tls/{cert,key}.pem and wired into whichever
#               _webui_mode is active. Self-signed means browsers will warn
#               on first visit — there is no good alternative for a lab
#               automation VM (Let's Encrypt needs a real reachable domain;
#               there's no internal CA in this project to reuse instead).
if [[ "${_webui_mode:-apache}" != "off" && -d webui ]]
then
    _lb_root=/srv/www/lab-builder
    mkdir -p "${_lb_root}/labs" &>/dev/null

    _lb_tls_cert=""
    _lb_tls_key=""
    if [[ "${_webui_tls:-1}" != "0" ]]
    then
        mkdir -p /etc/lab-builder/tls
        if [[ ! -f /etc/lab-builder/tls/cert.pem || ! -f /etc/lab-builder/tls/key.pem ]]
        then
            if command -v openssl &>/dev/null
            then
                openssl req -x509 -newkey rsa:2048 -nodes -days 3650 \
                    -subj "/CN=$(hostname -f 2>/dev/null || hostname)" \
                    -keyout /etc/lab-builder/tls/key.pem \
                    -out /etc/lab-builder/tls/cert.pem &>/dev/null
                chmod 0600 /etc/lab-builder/tls/key.pem
                chmod 0644 /etc/lab-builder/tls/cert.pem
            else
                echo "openssl not found — cannot generate a TLS cert; lab-builder will stay on plain HTTP (set _webui_tls=0 to silence this)"
            fi
        fi
        if [[ -f /etc/lab-builder/tls/cert.pem && -f /etc/lab-builder/tls/key.pem ]]
        then
            _lb_tls_cert=/etc/lab-builder/tls/cert.pem
            _lb_tls_key=/etc/lab-builder/tls/key.pem
        fi
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
    _lb_ver=$(git log -1 --format='%h' -- webui 2>/dev/null || echo 'unknown')
    grep -rl '__LABVERSION__' "${_lb_root}" 2>/dev/null | while read -r _f
    do
        sed -i "s/__LABVERSION__/${_lb_ver}/g" "${_f}"
    done

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

        if [[ -d /etc/apache2/vhosts.d ]]          # SLES / openSUSE
        then
            cp webui/apache/lab-builder.conf /etc/apache2/vhosts.d/lab-builder.conf
            # enable the modules this needs if they aren't already: cgid
            # always, ssl+rewrite only when a cert was actually generated
            # (a missing cert already logged its own warning above, and
            # leaving the SSL vhost uninstalled in that case avoids Apache
            # refusing to start over a cert file that doesn't exist).
            _needed_modules="cgid"
            if [[ -n "${_lb_tls_cert}" ]]
            then
                cp webui/apache/lab-builder-ssl.conf /etc/apache2/vhosts.d/lab-builder-ssl.conf
                _needed_modules="${_needed_modules} ssl rewrite"
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
            [[ -n "${_lb_tls_cert}" ]] && echo "  (also reachable, and redirected to, from http://<automation-vm>/lab-builder/ — self-signed cert, browsers will warn once)"
        elif [[ -d /etc/httpd/conf.d ]]            # RHEL family
        then
            cp webui/apache/lab-builder.conf /etc/httpd/conf.d/lab-builder.conf
            if [[ -n "${_lb_tls_cert}" ]]
            then
                cp webui/apache/lab-builder-ssl.conf /etc/httpd/conf.d/lab-builder-ssl.conf
                echo "  NOTE: RHEL family needs the 'mod_ssl' package installed separately for the HTTPS vhost above to load (not automated by this script)"
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
                sed -i "s/__LABVERSION__/$(git log -1 --format='%h' -- mcp/mcp_server.py 2>/dev/null || echo 'unknown')/" \
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
