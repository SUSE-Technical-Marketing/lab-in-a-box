#!/bin/bash
# Part of lab-in-a-box, this is a simple library that defines functions used by other shell scripts.
# Author/s: Raul Mahiques
# License: GPLv3
#
#  This program is free software: you can redistribute it and/or modify it under the terms of the GNU General Public License as published by the Free Software Foundation, either version 3 of the License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License along with this program. If not, see <https://www.gnu.org/licenses/gpl-3.0.html>.


# set -x -v


# Creates a VM image from a separate image and resizes it to the desired size.
function copy_vm_img() {
	echo "# - Copy the image for the new VM"
	ssh root@${REMOTE_HOST} "cp ${ISO_LOC}/${ISO_IMAGE} ${VM_IMG_LOC}/${_vm_name}.qcow2 ; qemu-img resize ${VM_IMG_LOC}/${_vm_name}.qcow2 ${VM_DSK}G"
}


# Creates ignition and combustion files used to setup the VM
function prepare_ign_and_cmb() {
	echo "# - Create ignition and combustion files"
	cp ${LAB_SETUP_PATH}/combustion/{template,$_vm_name}
	cp ${LAB_SETUP_PATH}/ignition/{template,$_vm_name.ign}
        
	sed "s/TEMPLATE_HN/$_vm_name/g;s#ROOT_PWD_HASH#${ROOT_PWD_HASH}#g;s#ROOT_SSH_KEY#$(cat /root/.ssh/id_rsa.pub)#g" -i ${LAB_SETUP_PATH}/ignition/${_vm_name}.ign

	sed "/#local vars/a mysource=${mysource}\nsourcepath=${sourcepath}\nmydns=${mydns}\nmyip=${myip}\nmymask=${mymask}\nmygw=${mygw}\nSUSE_email=${SUSE_email}\nSUSE_regcode=${SUSE_regcode}\nSUSE_url=${SUSE_url}" -i ${LAB_SETUP_PATH}/combustion/${_vm_name}
        sed "s#ROOT_SSH_KEY#$ROOT_SSH_KEY#g" -i ${LAB_SETUP_PATH}/combustion/${_vm_name}
}


# Creates cloud-init files used to setup the VM
function prepare_cloud-init() {
        echo "# - Create cloud-init files"
        ROOT_SSH_KEY=$(cat /root/.ssh/id_rsa.pub)
        for _type in user-data network-config meta-data
	do
          template_file=${LAB_SETUP_PATH}/cloud-init/template_${_type}
          process_templates >${LAB_SETUP_PATH}/cloud-init/${_vm_name}_${_type}
        done
}



# Copy the lab materials needed for the install to the hypervisor
function copy_to_hypervisor() {
	echo "## - Copy accross the lab setup materials"
	ssh root@${REMOTE_HOST} "mkdir -p ${LAB_SETUP_PATH}/"

        if [[ "$config_method" == "" ]]
        then
          ssh -q root@${REMOTE_HOST} "mkdir -p ${LAB_SETUP_PATH}/{combustion,ignition}"
          rsync -aqv ${LAB_SETUP_PATH}/combustion/${_vm_name} root@${REMOTE_HOST}:${LAB_SETUP_PATH}/combustion/
          rsync -aqv ${LAB_SETUP_PATH}/ignition/${_vm_name}.ign root@${REMOTE_HOST}:${LAB_SETUP_PATH}/ignition/
          ssh  -q root@${REMOTE_HOST} "chmod 0644 ${LAB_SETUP_PATH}/ignition/* ${LAB_SETUP_PATH}/combustion/*"
        else
          ssh -q root@${REMOTE_HOST} "mkdir -p ${LAB_SETUP_PATH}/${config_method}"
          rsync -aqv ${LAB_SETUP_PATH}/${config_method}/${_vm_name}* root@${REMOTE_HOST}:${LAB_SETUP_PATH}/${config_method}/
          ssh  -o StrictHostKeyChecking=accept-new root@${REMOTE_HOST} "cd ${LAB_SETUP_PATH}/${config_method}/; for i in ${_vm_name}*; do echo cp \${i} /tmp/\${i/${_vm_name}_/}; cp \${i} /tmp/\${i/${_vm_name}_/}; done ; rm -f ${VM_IMG_LOC}/${_vm_name}_ci.iso  ;mkisofs -J -l -R -V "cidata" -iso-level 4 -o ${VM_IMG_LOC}/${_vm_name}_ci.iso /tmp/user-data /tmp/meta-data /tmp/network-config"
          
        fi

}

# Add hostname entry to the DNS server as well as the API DNS entry, TBI
function add_to_dns() {
	echo "## Add hostname DNS entry"
	grep -qi "'${_vm_name}." /var/lib/named/${mynet_reverse}.db || echo "${myip//*.}      IN  PTR     ${_vm_name}." >>/var/lib/named/${mynet_reverse}.db
	grep -qi "^${_vm_name//.*} " /var/lib/named/${mydomain}.lan || echo "${_vm_name//.*}         IN  A       ${myip}" >>/var/lib/named/${mydomain}.lan


        if [[ "${clu_name}" != "" ]]
        then
          echo "## Add API DNS"
          _dns_entry="api.${clu_name}"
          for _dns in $(jq -r '.nodes | to_entries[].key' < ${inputFile} |xargs)
          do
                if [[ $(jq -r ".nodes[\"${_dns}\"][\"INSTALL_${clu_type}_TYPE\"]" < ${inputFile} ) == "server" ]]
                then
                        add_dns_to_named_rr
                fi
          done
          add_service_dns
        fi
        systemctl restart named
}


# function to add a service DNS giving preference to agent nodes.
function add_service_dns() {
        _count=0
        for _dns in $(jq -r '.nodes | to_entries[].key' < ${inputFile} |xargs)
        do
                if [[ $(jq -r ".nodes[\"${_dns}\"][\"INSTALL_${clu_type}_TYPE\"]" < ${inputFile} ) == "agent" ]]
                then
                        add_dns_to_named_rr
                        _count=1
                fi
        done
        if [[ "${_count}" == "0" ]]
        then
        	for _dns in $(jq -r '.nodes | to_entries[].key' < ${inputFile} |xargs)
	        do
			add_dns_to_named_rr
		done
		echo "DNS ${_dns_entry} added to point to all nodes"
	else
		echo "DNS ${_dns_entry} added to point to agent nodes"
        fi
        systemctl restart named
}


# Adds a DNS to Bind for round-robing balancing.
function add_dns_to_named_rr() {
	echo "## add DNS entry ${_dns_entry}.${mydomain}"
	_myip=$(jq -r ".nodes[\"${_dns}\"][\"myip\"]" < ${inputFile} )

	sed "/${_dns_entry}\tIN A  ${_myip}/d" -i /var/lib/named/${mydomain}.lan
	echo -e "${_dns_entry}\tIN A  ${_myip}" >> /var/lib/named/${mydomain}.lan
}

# Deletes a DNS entry from Bind
function del_from_dns() {
	echo "## Delete DNS entries for ${_vm_name}"
	sed "/${myip//*.}      IN  PTR     ${_vm_name}./d" -i /var/lib/named/8.168.192.db
	sed "/${_vm_name//.*}         IN  A       ${myip}/d" -i /var/lib/named/${mydomain}.lan
	systemctl restart named
}


# Creates a VM on a KVM hypervisor
function create_vm() {
	echo "## Create virtual machine"

        if [[ "$config_method" == "" ]]
        then
          virt-install --connect ${VIRT_SRV} \
	       --name  ${_vm_name} \
               --autostart \
               --boot uefi \
	       --vcpus ${VM_CPU}  \
	       --memory ${VM_MEM} \
	       --os-variant=${VM_OSVARIANT:-slem5.4} \
	       --import \
	       --disk size=${VM_DSK},path=${VM_IMG_LOC}/${_vm_name}.qcow2,sparse=no,boot.order=1 \
	       --graphics=spice  \
	       --network "${NETWORK}" \
	       --noautoconsole \
	       --qemu-commandline="-fw_cfg name=opt/com.coreos/config,file=${LAB_SETUP_PATH}/ignition/${IGN_FILE} -fw_cfg name=opt/org.opensuse.combustion/script,file=${LAB_SETUP_PATH}/combustion/${COM_FILE}"
        elif [[ "$config_method" == "cloud-init" ]]
        then
          virt-install  --connect ${VIRT_SRV} \
               --name  ${_vm_name} \
               --import \
               --autostart \
               --boot uefi \
               --vcpus ${VM_CPU}  \
               --memory ${VM_MEM} \
               --os-variant=${VM_OSVARIANT:-slem5.4} \
               --disk size=${VM_DSK},path=${VM_IMG_LOC}/${_vm_name}.qcow2,sparse=no,boot.order=1 \
               --graphics=spice  \
               --network "${NETWORK}" \
               --noautoconsole \
               --disk ${VM_IMG_LOC}/${_vm_name}_ci.iso,device=cdrom
          echo "### Waiting 3 minutes"
          sleep 180
          echo "### eject media"
          virsh --connect ${VIRT_SRV} change-media ${_vm_name} --eject ${VM_IMG_LOC}${_vm_name}_ci.iso
          if [[ "$salt_states" != "" ]]
          then
            setup_salt
            echo "#### applying salt states"
            for _salt_state in ${salt_states}
            do
              salt-ssh -i -v --update-roster  ${_vm_name} state.apply ${_salt_state}
            done
          fi
         echo "### reboot node"
          virsh --connect ${VIRT_SRV} reboot ${_vm_name}
        fi

}

# Deletes a VM from a KVM hypervisor
function delete_vm() {
	echo "## Delete VM"
        virsh -c ${VIRT_SRV} undefine --nvram "${_vm_name}"
	virsh -c ${VIRT_SRV} destroy  "${_vm_name}" 2>/dev/null
	virsh -c ${VIRT_SRV} undefine "${_vm_name}" --nvram --remove-all-storage
}

# Removes the VM ssh key from the known hosts to avoid warnings.
function clean_ssh_keys() {
	# Cleaup SSH keys
	ssh-keygen -f "${HOME}/.ssh/known_hosts" -R "${myip}"
}


# creates a config directory for operating kubernetes, TBI
function prepare_local_as_kubeclient() {
	# setup as client
	[ -d ~/.kube ] || mkdir -p ~/.kube
}


# Setup RKE2
function setup_rke2() {


        $ssh_command "mkdir -p /var/lib/rancher/${clu_type} /etc/rancher/${clu_type}"
        $ssh_command "curl -sfL https://get.${clu_type}.io | INSTALL_RKE2_TYPE=${INSTALL_RKE2_TYPE:-server}  INSTALL_RKE2_METHOD=${INSTALL_RKE2_METHOD} INSTALL_RKE2_CHANNEL=${clu_rel:-stable} sh -"
        rsync -a ${clu_name}/config-${INSTALL_RKE2_TYPE:-server}.yaml root@${_vm_name}:/etc/rancher/${clu_type}/config.yaml
        if [[ "${token}" == "" ]]
        then
                echo "# This is the 1st node of the cluster"
                echo "# Enable and start ${clu_type}-server"
                $ssh_command "systemctl enable --now ${clu_type}-${INSTALL_RKE2_TYPE:-server}.service"
                echo "# Retrieve node-token"
                token=`$ssh_command "cat /var/lib/rancher/${clu_type}/server/node-token"`
                RANCHER1_IP=${_vm_name}
        else
                $ssh_command "echo 'server: https://${RANCHER1_IP}:9345' >>/etc/rancher/${clu_type}/config.yaml"
                $ssh_command "echo 'token: ${token}' >>/etc/rancher/${clu_type}/config.yaml"
                $ssh_command "systemctl enable --now ${clu_type}-${INSTALL_RKE2_TYPE:-server}.service"
        fi

}


# Load VM variables
function load_vm_vars() {
        for _key in $(jq -r ".nodes[\"${_vm_name}\"] | to_entries[].key" < ${inputFile} )
        do
              export ${_key}="$(jq -r .nodes[\"${_vm_name}\"][\"${_key}\"] < ${inputFile} )"
        done
        for _key in $(jq -r '.common | to_entries[].key ' < ${inputFile} )
        do
              export ${_key}="$(jq -r .common[\"${_key}\"] < ${inputFile} )"
        done
}


# Load cluster variables
function load_cluster_vars() {
        for _key in $(jq -r '.cluster | to_entries[].key ' < ${inputFile} )
        do
            export ${_key}=$(jq -r ".cluster[\"${_key}\"]" < ${inputFile} )
        done
}

# Setup SALT
function setup_salt() {
   [ -d ${HOME}/salt-ssh/states ] || mkdir -p ${HOME}/salt-ssh/states
   cat >${HOME}/salt-ssh/roster <<-EOF
managed:
  host: ${_vm_name}
  user: root
  sudo: False
  priv: ${HOME}/.ssh/id_rsa
EOF

  for _state in ${salt_states}
  do
    template_file=${LAB_SETUP_PATH}/salt-ssh/${_state}
    process_templates >${HOME}/salt-ssh/states/${_state}
  done

}


# Setup Helm
function setup_helm() {
	# add helm
	echo "# Setup Helm"
        if [[ "$online" == "1" ]]
	then
	        $ssh_command "curl -#L https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 | bash"
	else
		$ssh_command 'curl http://automation/helm/install_helm.sh | bash -'
	fi
}


function helm_repo_add() {
        echo "Adding helm repository \"${_repo_name}\""
	$ssh_command "helm repo add ${_repo_name} ${_repo_url}"
        $ssh_command "helm repo update"
}



# Generic load Vars function

function _load_vars() {
	if jq -r ".${_section} | to_entries[].key " < ${inputFile} &>/dev/null
	then
		for _key in $(jq -r ".${_section} | to_entries[].key " < ${inputFile} )
	        do  
	            value=$(jq -r ".${_section}[\"${_key}\"]" < ${inputFile} )
	            export ${_key}="${value}"
	        done
	else
		echo "No variables defined for ${_section}"
	fi
}


# Load rancher related variables.
function load_rancher_vars() {
        _section="rancher"
        _load_vars
}


# Load Jenkins related variables.
function load_jenkins_vars() {
       _section="jenkins"
       _load_vars
}

# Load ArgoCD related variables.
function load_argocd_vars() {
       _section="argocd"
       _load_vars
}

 
  # Load Longhorn related variables.
function load_lh_vars() {
       _section="longhorn"
       _load_vars
}

# Load NeuVector related variables.
function load_nv_vars() {
       _section="neuvector"
       _load_vars
}


# Inspired from https://stackoverflow.com/questions/2914220/bash-templating-how-to-build-configuration-files-from-templates-with-bash#11050943
function process_templates() {
       eval "cat <<EOF                                   
$(cat ${template_file} )
EOF
"

}



