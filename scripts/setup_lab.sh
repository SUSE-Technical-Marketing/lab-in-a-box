#!/bin/bash
# Part of lab-in-a-box, it will setup a Lab
# Author/s: Raul Mahiques
# License: GPLv3
#
#  This program is free software: you can redistribute it and/or modify it under the terms of the GNU General Public License as published by the Free Software Foundation, either version 3 of the License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License along with this program. If not, see <https://www.gnu.org/licenses/gpl-3.0.html>.

inputFile=${1}


if [[ ! -f "${inputFile}" ]] 
then
   echo "Lab definition file (${inputFile}) doesn't exists or not specified"
   exit 1
elif ! jq <"${inputFile}" >/dev/null
then
   echo "Lab definition not in validated JSON format"
   exit 1
fi

clu_name="$(jq -r '.cluster.name ' < ${inputFile} &>/dev/null )"
lab_name="$(jq -r '.common.lab_name ' < ${inputFile} &>/dev/null )"

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


if [[ "${clu_name}" != "" ]]
then
  # load cluster vars
  load_cluster_vars
  echo "# Create all the VMs for the cluster \"${lab_name:-$clu_name}\""
else
  echo "# Create all the VMs for the lab \"$lab_name\""
fi

function load_def(){
	load_vm_vars
	ssh_command="ssh  -o StrictHostKeyChecking=accept-new -q  root@${_vm_name}"
}

for _vm_name in $(jq -r '.nodes | to_entries[].key' < ${inputFile} |xargs)
do
	echo "# Node: $_vm_name"
	ssh-keygen -f ~/.ssh/known_hosts -R "${_vm_name}"
	destroy_vm.sh "${inputFile}" "${_vm_name}"
	setup_vm.sh "${inputFile}" "${_vm_name}"
done

echo "# Wait ${delay_min} min (${delay_sec} sec)  and restart"
sleep ${delay_sec}



if [[ "${clu_name}" != "" ]]
then
  for _vm_name in $(jq -r '.nodes | to_entries[].key' < ${inputFile} |xargs)
  do
	load_def
	echo "# Restart node ${_vm_name}"
	$ssh_command 'reboot'
  done

  echo "# Wait ${delay_min} min and continue setting up the cluster"
  sleep $((60 * $delay_min))

  for _vm_name in $(jq -r '.nodes | to_entries[].key' < ${inputFile} |xargs)
  do
	echo "# Installing ${clu_type} on node ${_vm_name}"
	load_def
	
	setup_${clu_type}
  done

  echo "# Wait $(( 2 + $delay_min )) min and continue setting up the cluster $((60 * ( 2 + $delay_min) ))"
  sleep $((60 * ( 2 + $delay_min ) ))

  for _addon in $(jq -r '.addons[]' < ${inputFile})
  do
	if command -v install_${_addon} &>/dev/null
	then
                echo -e "\n## Running addon \"${_addon}\" ##\n"
		install_${_addon} "${inputFile}"
	else
		echo "## FAILED! Addon script \"install_${_addon}\" not found"
	fi
  done
fi


echo "## Install individual addons for each VM"
for _vm_name in $(jq -r '.nodes | to_entries[].key' < ${inputFile} |xargs)
do
    echo ${_vm_name}
    for _addon in $(jq -r ".nodes.\"${_vm_name}\".addons[]" < ${inputFile})
    do
      if command -v install_${_addon} &>/dev/null
      then
        echo -e "\n## Running addon \"${_addon}\" ##\n"
        _vm_name=$_vm_name install_${_addon} "${inputFile}"
      else
        echo "## FAILED! Addon script \"install_${_addon}\" not found"
      fi
    done
done



