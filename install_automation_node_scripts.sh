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





cp templates/lab_creation.cfg.example /etc/lab_creation.cfg.example
chmod 0600 /etc/lab_creation.cfg.example

cp scripts/setup_vm.sh /usr/local/bin/setup_vm.sh
chmod 0755 /usr/local/bin/setup_vm.sh
mkdir /usr/local/lib/lab_creation/
cp libs/lab_creation.bash /usr/local/lib/lab_creation/lab_creation.bash

cp scripts/destroy_vm.sh /usr/local/bin/destroy_vm.sh
chmod 0755  /usr/local/bin/destroy_vm.sh





for i in scripts/install_*.sh
do     
	cp $i  /usr/local/bin/
        chmod 0755  /usr/local/bin/${i//*\/}
done


cp scripts/setup_cluster.sh /usr/local/bin/setup_cluster.sh
chmod 0755  /usr/local/bin/setup_cluster.sh
mkdir -p /srv/www/htdocs/lab_creation/{combustion,ignition}
cp templates/combustion.template /srv/www/htdocs/lab_creation/combustion/template
cp templates/ignition.template /srv/www/htdocs/lab_creation/ignition/template
