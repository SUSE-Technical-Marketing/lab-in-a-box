#!/bin/bash



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





if [[ "$1" ]]
then
	MYIMAGE="${1}"
        docker pull "$MYIMAGE"
        docker tag "$MYIMAGE" ${MYREG}/${MYIMAGE}
	docker push ${MYREG}/${MYIMAGE}

else
        echo 'No image name provided'
fi

