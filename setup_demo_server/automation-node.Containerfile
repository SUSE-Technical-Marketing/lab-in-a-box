# The lab-in-a-box automation node as a container: the latest openSUSE Leap with systemd as init, running
# the same services as the automation VM (sshd, named, apache2). setup_lab_automation.sh builds it with
# _automation_node=container (attached to the lab bridge with its own IP) and runs it on Kubernetes with
# _automation_node=kubernetes (published as ghcr.io/rmahique/lab-automation-node by CI).
# Build argument KUBECTL_INSTALL: the script that installs the pinned kubectl (libs/kvm_host_profiles.py's
# kubectl_binary_script()).
FROM registry.opensuse.org/opensuse/leap:16.0
LABEL org.opencontainers.image.source=https://github.com/rmahique/lab-in-a-box

ARG KUBECTL_INSTALL
RUN test -n "${KUBECTL_INSTALL}"

RUN zypper --non-interactive --gpg-auto-import-keys install -y --no-recommends \
        systemd openssh-server openssh-clients apache2 bind bind-utils \
        vim-small git-core rsync jq curl tar gzip openssl python313 python313-PyYAML \
        podman libvirt-client virt-install salt-ssh fuse3 sshfs netcat-openbsd \
        xorriso mkisofs iproute2 iptables iputils hostname which lsof timezone \
    && zypper clean --all

RUN sh -c "${KUBECTL_INSTALL}"

# lab-node-setup.service configures the node at every start from /etc/lab_creation/node (on the persistent volume,
# put there by setup_lab_automation.sh), so a recreated container or pod is complete without the KVM host.
RUN printf '%s\n' '[Unit]' 'Description=Configure the lab-in-a-box automation node' \
        'ConditionPathExists=/etc/lab_creation/node/automation-node-lib.sh' 'After=local-fs.target' '' \
        '[Service]' 'Type=oneshot' 'RemainAfterExit=yes' \
        'ExecStart=/bin/bash /etc/lab_creation/node/automation-node-lib.sh configure --boot' '' \
        '[Install]' 'WantedBy=multi-user.target' > /etc/systemd/system/lab-node-setup.service

# /etc/lab_creation.cfg lives in /etc/lab_creation, which setup_lab_automation.sh keeps on a persistent volume.
RUN systemctl enable sshd named apache2 lab-node-setup \
    && mkdir -p /srv/www/htdocs/helm /srv/www/htdocs/lab_creation /srv/www/sources /root/.ssh \
        /etc/lab_creation /etc/lab-builder /etc/lab-mcp \
    && chmod 0700 /root/.ssh \
    && ln -s lab_creation/lab_creation.cfg /etc/lab_creation.cfg

STOPSIGNAL SIGRTMIN+3
CMD ["/usr/lib/systemd/systemd"]
