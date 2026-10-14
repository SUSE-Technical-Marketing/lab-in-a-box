#!/bin/bash


inputFile="$1"


_VERSION="__LABVERSION__"
[[ "${1}" == "--version" || "${1}" == "-v" ]] && echo "${0##*/} ${_VERSION}" && exit 0
for _arg in "$@"
do
        if [[ "${_arg}" == "-h" || "${_arg}" == "--help" ]]
        then
                echo "Usage: ${0##*/} <image>"
                echo "Pulls <image>, tags it as \${MYREG}/<image> (MYREG from lab_creation.cfg) and pushes it."
                echo "Options:"
                echo "  --version, -v  Print the version and exit."
                echo "  --help, -h     Print this help and exit."
                exit 0
        fi
done

# load lab_creation defaults
if [[ -f /etc/lab_creation.defaults ]]
then
        . /etc/lab_creation.defaults
elif [[ -f lab_creation.defaults ]]
then
        . lab_creation.defaults
else
        echo "ERROR: Configuration file lab_creation.defaults not found in local path or /etc"
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





if [[ "${inputFile}" ]]
then
	MYIMAGE="${inputFile}"
        docker pull "$MYIMAGE"
        docker tag "$MYIMAGE" ${MYREG}/${MYIMAGE}
	docker push ${MYREG}/${MYIMAGE}

else
        echo 'No image name provided'
fi





