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

# No library is sourced: MYREG from lab_creation.cfg and a container engine are all this needs. It used to test
# ${_lib_path}, a directory, with -f and exited "Library not found" every single time.
if [[ -z "${MYREG:-}" ]]
then
        echo "ERROR: MYREG is not set in lab_creation.cfg — it is the registry to push to, e.g. registry.mydemo.lab"
        exit 1
fi

# podman first: it is what this project installs and uses everywhere else. docker is accepted for a host that has
# only that.
if command -v podman >/dev/null 2>&1
then
        _engine=podman
elif command -v docker >/dev/null 2>&1
then
        _engine=docker
else
        echo "ERROR: neither podman nor docker is installed on this host"
        exit 1
fi


if [[ -z "${inputFile}" ]]
then
        echo "Usage: ${0##*/} <image>"
        echo 'No image name provided'
        exit 1
fi

MYIMAGE="${inputFile}"
"${_engine}" pull "${MYIMAGE}" || exit 1
"${_engine}" tag "${MYIMAGE}" "${MYREG}/${MYIMAGE}" || exit 1
"${_engine}" push "${MYREG}/${MYIMAGE}" || exit 1
echo "Pushed ${MYREG}/${MYIMAGE} with ${_engine}"





