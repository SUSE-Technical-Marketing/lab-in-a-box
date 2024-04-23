#!/bin/bash
# Part of lab-in-a-box, it will create the automation VM that orchestrates the creation of the labs
# Author/s: Raul Mahiques
# License: GPLv3
#
#  This program is free software: you can redistribute it and/or modify it under the terms of the GNU General Public License as published by the Free Software Foundation, either version 3 of the License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License along with this program. If not, see <https://www.gnu.org/licenses/gpl-3.0.html>.



# Defaults
_disk_size=40

if [[ -f lab.cfg ]]
then
        echo "Loading configuration file lab.cfg"
        . lab.cfg
else
        echo "Missing configuratoin file lab.cfg"
        exit 1
fi



echo "## Delete VM"
virsh -c qemu:///system destroy  "${AUTOMATION_HOSTNAME}" 2>/dev/null
virsh -c qemu:///system undefine "${AUTOMATION_HOSTNAME}" --remove-all-storage

cp ${_QCOW_IMAGE} /var/lib/libvirt/images/${AUTOMATION_HOSTNAME}.qcow2 ; qemu-img resize /var/lib/libvirt/images/${AUTOMATION_HOSTNAME}.qcow2 ${_disk_size}G


guestmount -i --rw -a  /var/lib/libvirt/images/${AUTOMATION_HOSTNAME}.qcow2 /mnt/

rm /mnt/var/lib/YaST2/reconfig_system
cp /etc/resolv.conf /mnt/etc/

echo "$AUTOMATION_HOSTNAME">/mnt/etc/hostname

umask 077 # Required for NM config
mkdir -p /mnt/etc/NetworkManager/system-connections/
cat >/mnt/etc/NetworkManager/system-connections/static.nmconnection <<-EOF
[connection]
id=static
type=ethernet
autoconnect=true

[ipv4]
method=manual
dns-search=${_mydomain}
dns=${_myip};${_mydns}
address1=${_myip}/${_mymask}
gateway=${_mygw}
EOF

# set the keyboard
echo "KEYMAP=us" >> /mnt/etc/vconsole.conf

# set the time zone
ln -sf /usr/share/zoneinfo/Europe/Zurich /mnt/etc/localtime

chroot /mnt/ zypper install -y  vim-small git rsync apache2  bind-utils bind docker podman libvirt-client jq NetworkManager virt-install
chroot /mnt/ systemctl disable firewalld.service
chroot /mnt/ systemctl disable wicked.service
chroot /mnt/ systemctl enable sshd.service
chroot /mnt/ systemctl enable NetworkManager.service
chroot /mnt/ systemctl enable named
chroot /mnt/ ssh-keygen -b 16384 -N '' -t rsa -f /root/.ssh/id_rsa
cat /mnt/root/.ssh/id_rsa.pub >>/root/.ssh/authorized_keys
echo "# This is the automation host public key: 

`cat /mnt/root/.ssh/id_rsa.pub `

"
echo 'root:${root_pwd}' | chroot /mnt/ chpasswd -c SHA512
echo "$ROOT_SSH_PUB_KEY" >> /mnt/root/.ssh/authorized_keys


cat >/mnt/etc/named.conf  <<-EOF
options {
        directory "/var/lib/named";
        managed-keys-directory "/var/lib/named/dyn/";
        dump-file "/var/log/named_dump.db";
        statistics-file "/var/log/named.stats";
        # if listen on IPv4 port
        listen-on port 53 { any; };
        # if listen on IPv6 port
        listen-on-v6 { any; };
        # allow query range ( set internal server and so on )
        allow-query { 127.0.0.1; 0.0.0.0/0; };
        recursion yes;
};
zone "." in {
        type hint;
        file "root.hint";
};
zone "localhost" in {
        type master;
        file "localhost.zone";
};
zone "0.0.127.in-addr.arpa" in {
        type master;
        file "127.0.0.zone";
};
zone "0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.ip6.arpa" IN {
        type master;
        file "127.0.0.zone";
};
zone "${_mydomain}" in {
        type master;
        file "${_mydomain}.lan";
        allow-update { none; };
};
zone "${_mynetrev}.in-addr.arpa" in {
        type master;
        file "${_mynetrev}.db";
        allow-update { none; };
};

EOF


cat >/mnt/var/lib/named/${_mynetrev}.db  <<-EOF
\$TTL 86400
@   IN  SOA     ${AUTOMATION_HOSTNAME}. root.${_mydomain}. (
        2019011601  ;Serial
        3600        ;Refresh
        1800        ;Retry
        604800      ;Expire
        86400       ;Minimum TTL
)
        IN  NS      ${AUTOMATION_HOSTNAME}.

        IN  PTR     ${_mydomain}.
        IN  A       ${_mymask}

${_myip//*.}      IN  PTR     ${AUTOMATION_HOSTNAME}.
$(ip -4 --brief a show br0 primary|awk -F'.' '{print $NF}'| cut -d/ -f1  )      IN  PTR     $(hostname -f).


EOF

cat >/mnt/var/lib/named/${_mydomain}.lan  <<-EOF
\$TTL 86400
@   IN  SOA     ${AUTOMATION_HOSTNAME}. root.${_mydomain}. (
        2019011603  ;Serial
        1m        ;Refresh
        15m        ;Retry
        3w      ;Expire
        2h       ;Minimum TTL
)
        IN  NS      ${AUTOMATION_HOSTNAME}.
        IN  A       ${_myip}
        IN  MX 10   ${AUTOMATION_HOSTNAME}.

${AUTOMATION_HOSTNAME//.$_mydomain}         IN  A       ${_myip}
bastion          IN  CNAME   ${AUTOMATION_HOSTNAME}.
$(hostname)         IN  A       $(gethostip -d $HOSTNAME)

EOF

chmod 0644 /mnt/var/lib/named/${_mydomain}.lan /mnt/var/lib/named/${_mynetrev}.db 


sleep 5
guestunmount /mnt


echo "## Create virtual machine"
        virt-install --connect qemu:///system \
               --name  $AUTOMATION_HOSTNAME \
               --vcpus 1  \
               --memory 2048 \
               --osinfo=opensuse15.5 \
               --import \
               --disk size=${_disk_size},path=/var/lib/libvirt/images/${AUTOMATION_HOSTNAME}.qcow2,sparse=no,boot.order=1 \
               --graphics=spice  \
               --network "bridge=br0" \
               --noautoconsole

echo "Wait until the VM starts"
sleep 60 

# There seems to be a bug, the vnet of the vm doesn't get added automatically to the bridge, so we need to stop it and start it.
virsh shutdown $AUTOMATION_HOSTNAME
virsh start $AUTOMATION_HOSTNAME

echo "Reconfigure host to use new VM as DNS server"
sed "s/NETCONFIG_DNS_STATIC_SERVERS=.*/NETCONFIG_DNS_STATIC_SERVERS=\"${_myip} ${_mydns}\"/;s/NETCONFIG_DNS_STATIC_SEARCHLIST=.*/NETCONFIG_DNS_STATIC_SEARCHLIST=\"${_mydomain}\"/" -i /etc/sysconfig/network/config
systemctl restart network




