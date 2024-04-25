#!/bin/bash
# Part of lab-in-a-box, it will install LongHorn
# Author/s: Raul Mahiques
# License: GPLv3
#
#  This program is free software: you can redistribute it and/or modify it under the terms of the GNU General Public License as published by the Free Software Foundation, either version 3 of the License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License along with this program. If not, see <https://www.gnu.org/licenses/gpl-3.0.html>.


inputFile=${1}

function usage() {
        echo "Usage:
$0 <configuration file>"

}

if [[ ! ${inputFile} ]]
then
        echo "missing configuration file parameter"
        usage
        exit 1
fi

if [[ ! -f ${inputFile} ]]
then
        echo "configuration file \"${inputFile}\" not found or name incorrect"
        usage
	exit 1
elif ! jq <"${inputFile}" >/dev/null
then
   echo "Cluster definition not in validated JSON format"
   exit 1
fi


# load lab_creation config
if [[ -f /etc/lab_creation.cfg ]]
then
        . /etc/lab_creation.cfg
elif [[ -f lab_creation.cfg ]]
then
        . lab_creation.cfg
else
        echo "ERROR: Configuration file lab_creation.cfg not found in local path or /etc"
        exit 1
fi

if [[ ! -f ${_lib_path} ]]
then
        echo "ERROR: Library \"${_lib_path}\" not found"
        exit 1
else
        # load library
        . ${_lib_path}
fi

# load cluster vars
load_cluster_vars

# load rancher variables
load_rancher_vars

# load longhorn variables
load_lh_vars




# find a server node or API url
for _vm_name in $(jq -r '.nodes | to_entries[].key' < ${inputFile} |xargs)
do
	load_vm_vars
	ssh_command="ssh  -o StrictHostKeyChecking=accept-new -q  root@${_vm_name}"
	if [[ "${INSTALL_RKE2_TYPE}" == "server" || "${INSTALL_RKE2_TYPE}" == "" ]]
	then
		echo "# Using node: $_vm_name"
		setup_helm
		setup_lh_repo
		setup_lh
		sleep 60
		exit 1
	fi
done


