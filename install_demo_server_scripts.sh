#!/bin/bash
# Part of lab-in-a-box, prepares the demo server scripts
# Author/s: Raul Mahiques
# License: GPLv3
#
#  This program is free software: you can redistribute it and/or modify it under the terms of the GNU General Public License as published by the Free Software Foundation, either version 3 of the License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License along with this program. If not, see <https://www.gnu.org/licenses/gpl-3.0.html>.



[[ -d /var/tmp/setup_demo_server ]] || mkdir /var/tmp/setup_demo_server
if git clone  https://raw.githubusercontent.com/SUSE-Technical-Marketing /var/tmp/setup_demo_server/
then
	cd /var/tmp/setup_demo_server/setup_demo_server/
	chmod 0755 setup_kvm_node.sh setup_lab_automation.sh

	cp lab.cfg.template  lab.cfg
	echo "
Please edit the file lab.cfg in this folder to include your settings
Once you have done so run setup_kvm_node.sh:
./setup_kvm_node.sh [<node_ip>]

Where node_ip is the IP of the server you want to use for your lab.
You can also setup your current machine as the lab server by not adding any parameter.

"
else
	echo "ERROR: Cloning the repository failed, this is the command used: \"git clone  https://raw.githubusercontent.com/SUSE-Technical-Marketing /var/tmp/setup_demo_server/\""
	exit 1
fi


