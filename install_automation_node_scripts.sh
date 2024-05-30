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
	cd $_scripts_path
fi

if [[ "$_templ_addons_loc" == "" ]]
then
	_templ_addons_loc=/usr/share/lab_creation/templates/addons/
fi


# create directories
mkdir -p /srv/www/htdocs/lab_creation/{combustion,ignition,cloud-init,salt} ${_templ_addons_loc} /usr/local/lib/lab_creation/ &>/dev/null

cp templates/lab_creation.cfg.example /etc/lab_creation.cfg.example
chmod 0600 /etc/lab_creation.cfg.example

cp libs/lab_creation.bash /usr/local/lib/lab_creation/lab_creation.bash

cp -r  templates/addons/* ${_templ_addons_loc}/


for i in scripts/install_*
do     
	cp $i  /usr/local/bin/
        chmod 0755  /usr/local/bin/${i//*\/}
done

for i in setup_cluster.sh destroy_vm.sh setup_vm.sh pushDockerImage.sh
do
    cp scripts/$i /usr/local/bin/$i
    chmod 0755  /usr/local/bin/$i

done


for i in templates/salt/*
do
  cp $i /srv/www/htdocs/lab_creation/salt/
done

for i in combustion.template ignition.template cloud-init.template_meta-data cloud-init.template_network-config cloud-init.template_user-data
do
  cp templates/${i} /srv/www/htdocs/lab_creation/${i//./\/}
done



