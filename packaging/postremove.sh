#!/bin/bash
# Runs after this package is removed. Deliberately does NOT touch anything
# install_automation_node_scripts.sh deployed (/usr/local/bin,
# /usr/local/lib/lab_creation, /etc/lab_creation*, /srv/www/htdocs/lab_creation,
# /srv/www/lab-builder) — a live automation node can have real labs, DNS
# zones, and TLS material sitting under those paths, and this project
# already has an explicit, deliberate, opt-in way to tear that down
# (destroy_lab.py / rodeo-style clean commands); a package removal is not
# it. Only /usr/share/lab-in-a-box (this package's own copy of the
# source tree) is removed by the package manager itself.
cat <<'EOF'
lab-in-a-box package removed. Anything install_automation_node_scripts.sh
already deployed (/usr/local/bin, /usr/local/lib/lab_creation,
/etc/lab_creation*, /srv/www/htdocs/lab_creation, /srv/www/lab-builder) was
left in place on purpose — remove it by hand if you really want to, only
after checking for labs/DNS zones/certs you still need.
EOF
