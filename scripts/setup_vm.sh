#!/bin/bash
# Part of lab-in-a-box, it will setup a VM
# Author/s: Raul Mahiques
# License: GPLv3
#
#  This program is free software: you can redistribute it and/or modify it under the terms of the GNU General Public License as published by the Free Software Foundation, either version 3 of the License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License along with this program. If not, see <https://www.gnu.org/licenses/gpl-3.0.html>.



inputFile=${1}
_vm_name=${2}

function usage() {
        echo "Usage:
$0 <configuration file> <vm_name>"

}


if [[ ! ${inputFile} ]]
then
        _msg="ERROR: missing configuration file parameter" show_nicer_messages
        usage
        exit 1
fi
if [[ ! -f ${inputFile} ]]
then
        _msg="ERROR: configuration file \"${inputFile}\" not found or name incorrect" show_nicer_messages
        usage
        exit 1
elif ! jq <"${inputFile}" >/dev/null
then
   _msg="Cluster definition not in validated JSON format" show_nicer_messages
   exit 1
fi

if [[ ! ${_vm_name} ]]
then
        _msg="ERROR: Missing VM name parameter" show_nicer_messages
        usage
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
        _msg="ERROR: Configuration file lab_creation.cfg not found in local path or /etc" show_nicer_messages
	exit 1
fi

if [[ ! -f ${_lib_path} ]]
then
        _msg="ERROR: Library \"${_lib_path}\" not found" show_nicer_messages
	exit 1
else
	# load library
	. ${_lib_path}
fi



# load VM settings
load_vm_vars

IGN_FILE="${_vm_name}.ign"
COM_FILE="${_vm_name}"

# set some defaults
if [[ "$mymac" == "" ]]
then
  NETWORK="${NETWORK:-bridge=br0}"
else
  NETWORK="${NETWORK:-bridge=br0,mac.address=${mymac}}"
fi

# Define shortcut for the ssh command
ssh_command="ssh  -o StrictHostKeyChecking=accept-new root@${REMOTE_HOST}"


copy_vm_img

if [[ "$config_method" == "" ]]
then
  prepare_ign_and_cmb
else
  prepare_${config_method}
fi


copy_to_hypervisor

add_to_dns

create_vm

clean_ssh_keys

prepare_local_as_kubeclient


_msg="\t\tVM \"${_vm_name}\" created" show_nicer_messages


