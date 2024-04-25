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
function create_ign_and_cmb() {
	echo "# - Create ignition and combustion files"
	cp ${LAB_SETUP_PATH}/combustion/{template,$_vm_name}
	cp ${LAB_SETUP_PATH}/ignition/{template,$_vm_name.ign}
        
	sed "s/TEMPLATE_HN/$_vm_name/g;s#ROOT_PWD_HASH#${ROOT_PWD_HASH}#g;s#ROOT_SSH_KEY#$(cat /root/.ssh/id_rsa.pub)#g" -i ${LAB_SETUP_PATH}/ignition/${_vm_name}.ign

	sed "/#local vars/a mysource=${mysource}\nsourcepath=${sourcepath}\nmydns=${mydns}\nmyip=${myip}\nmymask=${mymask}\nmygw=${mygw}\nSUSE_email=${SUSE_email}\nSUSE_regcode=${SUSE_regcode}\nSUSE_url=${SUSE_url}" -i ${LAB_SETUP_PATH}/combustion/${_vm_name}
        sed "s#ROOT_SSH_KEY#$ROOT_SSH_KEY#g" -i ${LAB_SETUP_PATH}/combustion/${_vm_name}
}


# Copy the lab materials needed for the install to the hypervisor
function copy_to_hypervisor() {
	echo "## - Copy accross the lab setup materials"
	ssh root@${REMOTE_HOST} "mkdir -p ${LAB_SETUP_PATH}/"
        ssh -q root@${REMOTE_HOST} "mkdir -p ${LAB_SETUP_PATH}/{combustion,ignition}"
        rsync -aqv ${LAB_SETUP_PATH}/combustion/${_vm_name} root@${REMOTE_HOST}:${LAB_SETUP_PATH}/combustion/
	rsync -aqv ${LAB_SETUP_PATH}/ignition/${_vm_name}.ign root@${REMOTE_HOST}:${LAB_SETUP_PATH}/ignition/
        ssh  -q root@${REMOTE_HOST} "chmod 0644 ${LAB_SETUP_PATH}/ignition/* ${LAB_SETUP_PATH}/combustion/*"
}

# Add hostname entry to the DNS server as well as the API DNS entry, TBI
function add_to_dns() {
	echo "## Add hostname DNS entry"
	grep -qi "'${_vm_name}." /var/lib/named/${mynet_reverse}.db || echo "${myip//*.}      IN  PTR     ${_vm_name}." >>/var/lib/named/${mynet_reverse}.db
	grep -qi "^${_vm_name//.*} " /var/lib/named/${mydomain}.lan || echo "${_vm_name//.*}         IN  A       ${myip}" >>/var/lib/named/${mydomain}.lan

	echo "## Add API DNS"
	_dns_entry="api.${clu_name}"
	for _dns in $(jq -r '.nodes | to_entries[].key' < ${inputFile} |xargs)
        do
		if [[ $(jq -r ".nodes[\"${_dns}\"][\"INSTALL_RKE2_TYPE\"]" < ${inputFile} ) == "server" ]]
                then
        		add_dns_to_named_rr
		fi
	done
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
	virt-install --connect ${VIRT_SRV} \
	       --name  ${_vm_name} \
	       --vcpus ${VM_CPU}  \
	       --memory ${VM_MEM} \
	       --os-variant=slem5.4 \
	       --import \
	       --disk size=${VM_DSK},path=${VM_IMG_LOC}/${_vm_name}.qcow2,sparse=no,boot.order=1 \
	       --graphics=spice  \
	       --network "${NETWORK}" \
	       --noautoconsole \
	       --qemu-commandline="-fw_cfg name=opt/com.coreos/config,file=${LAB_SETUP_PATH}/ignition/${IGN_FILE} -fw_cfg name=opt/org.opensuse.combustion/script,file=${LAB_SETUP_PATH}/combustion/${COM_FILE}"
}

# Deletes a VM from a KVM hypervisor
function delete_vm() {
	echo "## Delete VM"
	virsh -c ${VIRT_SRV} destroy  "${_vm_name}" 2>/dev/null
	virsh -c ${VIRT_SRV} undefine "${_vm_name}" --remove-all-storage
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


# Setup RK2
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
            export ${_key}=$(jq -r ".nodes[\"${_vm_name}\"][\"${_key}\"]" < ${inputFile} )
        done
        for _key in $(jq -r '.common | to_entries[].key ' < ${inputFile} )
        do
            export ${_key}=$(jq -r ".common[\"${_key}\"]" < ${inputFile} )
        done
}


# Load cluster variables
function load_cluster_vars() {
        for _key in $(jq -r '.cluster | to_entries[].key ' < ${inputFile} )
        do
            export ${_key}=$(jq -r ".cluster[\"${_key}\"]" < ${inputFile} )
        done
}


# Setup Helm
function setup_helm() {
	# add helm
	echo "# Setup Helm"
        $ssh_command "curl -#L https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 | bash"
}


function helm_repo_add() {
        echo "Adding helm repository \"${_repo_name}\""
	$ssh_command "helm repo add ${_repo_name} ${_repo_url}"
        $ssh_command "helm repo update"
}

# Setup SUSE Rancher helm respoitory
function setup_rancher_repo() {
        _repo_name="${rancher_rel:-rancher-stable/rancher}"
        _repo_url="${rancher_repo_url:-https://releases.rancher.com/server-charts/stable}"
	helm_repo_add
	_repo_name="${rancher_cert_repo_name:-jetstack}"
	_repo_url="${rancher_cert_repo_url:-https://charts.jetstack.io}"
	helm_repo_add
}

# Setup SUSE NeuVector helm repository
function setup_nv_repo() {
	_repo_name="${nv_rel:-neuvector}"
        _repo_url="${nv_repo_url:-https://neuvector.github.io/neuvector-helm}"
	helm_repo_add
}

# Setup SUSE Longhorn helm repository
function setup_lh_repo() {
        _repo_name="${lh_rel:-longhorn}"
        _repo_url="${lh_repo_url:-https://charts.longhorn.io}"
	helm_repo_add
}


# Setup SUSE Longhorn
function setup_lh() {
                $ssh_command "kubectl create namespace longhorn-system"
                $ssh_command "helm upgrade -i longhorn longhorn/longhorn --namespace longhorn-system --set ingress.enabled=true --set ingress.host=${lh_shorthn:-longhorn}.${clu_name}.${mydomain}"
		echo "Longhorn should be available in a few minutes in: ${lh_shorthn:-longhorn}.${clu_name}.${mydomain}"
}

# Setup SUSE NeuVector
function setup_nv() {
	        $ssh_command "kubectl create namespace cattle-neuvector-system"
	        $ssh_command "helm upgrade -i neuvector neuvector/core --namespace cattle-neuvector-system --set k3s.enabled=true --set k3s.runtimePath=/run/k3s/containerd/containerd.sock --set manager.ingress.enabled=true --set manager.svc.type=ClusterIP --set controller.pvc.enabled=true --set manager.ingress.host=${nv_shorthn:-neuvector}.${clu_name}.${mydomain} --set global.cattle.url=https://${rancher_shorthn}.${clu_name}.${mydomain} --set controller.ranchersso.enabled=true --set rbac=true"
		echo "NeuVector should be available in a few minutes in: ${nv_shorthn:-neuvector}.${clu_name}.${mydomain}"
}


# Setup cert-manager
function setup_cert-manager() {
        # https://cert-manager.io/docs/installation/helm/
	echo "# Setup Cert-manager"
        $ssh_command "helm upgrade -i cert-manager jetstack/cert-manager ${cert_manager_ver} --namespace cert-manager --create-namespace --set installCRDs=true"
}

# Setup SUSE Rancher
function setup_rancher() {
	echo "# Setup Rancher ${rancher_repo:-jjjj}"
        $ssh_command "helm upgrade -i ${rancher_helm_rel:-rancher} ${rancher_helm_chart} --create-namespace --namespace cattle-system --set hostname="${rancher_shorthn}.${clu_name}.${mydomain}" --set bootstrapPassword=\"${rancher_initial_pwd}\""
	echo "# Get initial password: "
	$ssh_command "kubectl get secret --namespace cattle-system bootstrap-secret -o go-template='{{.data.bootstrapPassword|base64decode}}{{ \"\n\" }}'"
	
	# verify it's up and running
	# kubectl -n cattle-system rollout status deploy/rancher
	# kubectl -n cattle-system get deploy rancher
	
	echo "## Add Rancher DNS"
        _dns_entry="${rancher_shorthn:-ERROR_ranchershort}.${clu_name}"
        for _dns in $(jq -r '.nodes | to_entries[].key' < ${inputFile} |xargs)
        do
                        add_dns_to_named_rr
        done
        systemctl restart named
	echo "Wait 5 minutes for the installation to finish"
	sleep 300

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



