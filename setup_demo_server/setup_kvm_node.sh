#!/bin/bash
# Prepares the hypervisor to work as lab_automation node
# Author/s: Raul Mahiques
# License: GPLv3
#
#  This program is free software: you can redistribute it and/or modify it under the terms of the GNU General Public License as published by the Free Software Foundation, either version 3 of the License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License along with this program. If not, see <https://www.gnu.org/licenses/gpl-3.0.html>.






function do_it_all() {
        if [[ ! -f setup_lab_automation.sh ]]
        then
                echo "Please download setup_lab_automation.sh script from the GIT repository"
		exit 1
        fi
        echo "## Configure package repositories ##"
        SUSEConnect --product PackageHub/15.5/x86_64
        SUSEConnect --product sle-module-containers/15.5/x86_64
        SUSEConnect --product sle-module-basesystem/15.5/x86_64
#        SUSEConnect --product sle-module-development-tools/15.5/x86_64
        SUSEConnect --product sle-module-legacy/15.5/x86_64
        echo "## Update all packages and install necessary ones ##"
        zypper refresh
        zypper update -y
        zypper install -y libvirt podman docker cri-tools minikube-bash-completion kubectl-who-can kubevirt-virtctl kubernetes1.28-client gpgme-devel device-mapper-devel libbtrfs-devel git-core mc bridge-utils tcpdump sensors ftsteutates-sensors


        [[ -d /var/lib/libvirt/images/sources/ ]] || mkdir -p /var/lib/libvirt/images/sources/

        echo "## Download openSUSE Leap image to be used for the VM ##"
        cd /var/lib/libvirt/images/sources/ && wget -nc https://download.opensuse.org/distribution/leap/15.5/appliances/openSUSE-Leap-15.5-Minimal-VM.x86_64-kvm-and-xen.qcow2

        echo '<!--
WARNING: THIS IS AN AUTO-GENERATED FILE. CHANGES TO IT ARE LIKELY TO BE
OVERWRITTEN AND LOST. Changes to this xml configuration should be made using:
  virsh pool-edit pool
or other application using the libvirt API.
-->

<pool type='dir'>
  <name>pool</name>
  <uuid>8bd63226-f3e4-4a14-965f-a75673a1a291</uuid>
  <capacity unit='bytes'>0</capacity>
  <allocation unit='bytes'>0</allocation>
  <available unit='bytes'>0</available>
  <source>
  </source>
  <target>
    <path>/var/lib/libvirt/images/sources</path>
  </target>
</pool>
' >/etc/libvirt/storage/pool.xml

	ln -s /etc/libvirt/storage/pool.xml /etc/libvirt/storage/autostart/pool.xml &>/dev/null
        systemctl enable --now libvirtd
        systemctl disable --now firewalld

        echo "## Start setup_lab_automation.sh script to create the automation VM ##"
	cd /var/tmp/$0_${_currenttime}/
        tmp_folder=/var/tmp/$0_${_currenttime}/ bash setup_lab_automation.sh

}

[[ "${_currenttime}" == "" ]] && _currenttime="`date +%s`"
_input="$1"

if [[ -f lab.cfg ]]
then
	echo "Loading configuration file lab.cfg"
	. lab.cfg
else
	echo "Missing configuratoin file lab.cfg"
	exit 1
fi

if [[ "${_input}" != "" ]]
then
	if  ping -c 1 -q "${_input}" &>/dev/null
	then
	        echo -e "###############\n## Setting up ${_input} remotely ##\n###############"
	        ssh-copy-id root@${_input}
		if [[ "$?" != "0" ]]
		then
			echo "ERROR, we need an SSH key to continue, to generate one please run ssh-keygen -b 16384 -t rsa -a 100 -f ~/id_rsa_TESTTDELETEME -N ''"
			exit 1
		fi
	        ssh root@${_input} "mkdir /var/tmp/$0_${_currenttime}"
#		if [[ ! -f setup_lab_automation.sh ]]
#        	then
#                   echo "Please download setup_lab_automation.sh script from the GIT repository"
#                   exit 1
#                fi
	        scp $0 lab.cfg setup_lab_automation.sh root@${_input}:/var/tmp/$0_${_currenttime}/
	        ssh root@${_input} "cd /var/tmp/$0_${_currenttime}/ ; _currenttime=${_currenttime} bash $0 -y"
	elif [[ "${_input}" == "-y" ]]
	then
		do_it_all
	else
		echo "ERROR: incorrect parameter \"${_input}\""
	fi
else
        read -p 'Are you sure? (yes/n): ' _response
        if [[ "${_response}" == "yes" ]]
        then
                do_it_all
        else
		echo "

Usage: $0 [-y] [<IP/hostname>]
-y Automatically accept
<IP/hostname> of the host you want to setup

"
                exit 0
        fi

fi


