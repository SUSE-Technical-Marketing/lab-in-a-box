#!/bin/bash
# Runs after the lab-in-a-box package's files land under
# /usr/share/lab-in-a-box (see packaging/nfpm.yaml's own "contents").
# Deliberately does NOT reimplement install_automation_node_scripts.sh's
# own deployment logic (directory creation, __LABVERSION__ stamping, the
# webui/MCP endpoint setup, etc.) — it just runs that script, from the
# package's own copy, the exact same way a manual `git clone` + run
# already does. Safe to re-run on a package upgrade: that script backs up
# the previous deployment first and is explicitly designed to be re-run
# (see README.md's "Update an existing install").
#
# Deliberately does NOT `set -e` / exit non-zero when
# install_automation_node_scripts.sh itself fails (e.g. no python3.11 on
# PATH yet — the most common case, confirmed live installing this RPM on a
# bare fedora:latest container, which doesn't have python3.11 by default).
# rpm/dnf treats a non-zero %post as a FAILED TRANSACTION and rolls back
# the whole install — not just this package, every dependency (bind, jq,
# rsync, openssh-clients...) it just pulled in too — confirmed live. The
# package's own files under /usr/share/lab-in-a-box are already on disk
# regardless of what install_automation_node_scripts.sh does, so there's
# nothing to roll back there; the friendlier outcome is "package installed,
# here's what to do next", not "install everything, then undo it all".
cd /usr/share/lab-in-a-box || exit 0

if bash install_automation_node_scripts.sh; then
	cat <<'EOF'

lab-in-a-box's automation-node toolkit is installed. A few tools it also
needs are NOT pulled in by this package (no consistent native package
across every distro this package supports) — install separately if you
don't already have them:
  - helm      (helm.sh/docs/intro/install)
  - kubectl   (kubernetes.io/docs/tasks/tools)
  - virt-install / virsh (on the HYPERVISOR host, not necessarily here)

See README.md for the rest of the setup (lab_creation.cfg, DNS, etc.).
EOF
else
	cat <<'EOF' >&2

lab-in-a-box's package files are installed under /usr/share/lab-in-a-box,
but install_automation_node_scripts.sh did not complete (see the error
above — commonly a missing python3.11, this project's pinned Python
version). Fix that, then finish the deployment yourself with:
  bash /usr/share/lab-in-a-box/install_automation_node_scripts.sh
EOF
fi

exit 0
