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
        echo "ERROR: missing configuration file parameter"
        usage
        exit 1
fi
if [[ ! -f ${inputFile} ]]
then
        echo "ERROR: configuration file \"${inputFile}\" not found or name incorrect"
        usage
        exit 1
fi

if [[ ! ${_vm_name} ]]
then
        echo "ERROR: Missing VM name parameter"
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



# load VM settings
load_vm_vars

IGN_FILE=${_vm_name}.ign
COM_FILE=${_vm_name}
NETWORK="bridge=br0,mac.address=${mymac}"



copy_vm_img

create_ign_and_cmb

copy_to_hypervisor

add_to_dns

create_vm

clean_ssh_keys

prepare_local_as_kubeclient


echo -e "#\t\tVM \"${_vm_name}\" created\n"


