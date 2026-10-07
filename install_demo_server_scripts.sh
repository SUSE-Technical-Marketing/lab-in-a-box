#!/bin/bash
# Part of lab-in-a-box: one-command setup of a KVM hypervisor and its automation VM.
# Author/s: Raul Mahiques
# License: GPLv3
#
#  This program is free software: you can redistribute it and/or modify it under the terms of the GNU General Public License as published by the Free Software Foundation, either version 3 of the License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License along with this program. If not, see <https://www.gnu.org/licenses/gpl-3.0.html>.
#
# Usage (as root):
#   curl -fsSL https://raw.githubusercontent.com/SUSE-Technical-Marketing/lab-in-a-box/main/install_demo_server_scripts.sh | bash -s -- [options]
#
# Installs git and Python 3 (python311 on openSUSE Leap / SLES 15, the distribution's python3
# elsewhere), fetches lab-in-a-box into /var/tmp/setup_demo_server and runs
# setup_demo_server/setup_kvm_node.py with every other option (see its --help). Prompts are read
# from the terminal; without one (cloud-init, CI) pass --non-interactive.
#
# Options handled here:
#   --source DIR    use the lab-in-a-box checkout in DIR instead of cloning
#   --fetch-only    only fetch the code; edit setup_demo_server/lab.cfg and run setup_kvm_node.py yourself
# Environment: LAB_REPO_URL, LAB_REPO_BRANCH (default: the SUSE-Technical-Marketing repo, main).

set -euo pipefail

_red='\033[1;31m'; _reset='\033[0m'
die() { echo -e "${_red}ERROR${_reset}: $*" >&2; exit 1; }

_repo_url=${LAB_REPO_URL:-https://github.com/SUSE-Technical-Marketing/lab-in-a-box.git}
_branch=${LAB_REPO_BRANCH:-main}
_dest=/var/tmp/setup_demo_server
_source=""
_fetch_only=0
_args=()
while (( $# )); do
    case $1 in
        --source) _source=${2:?--source needs a directory}; shift 2 ;;
        --source=*) _source=${1#*=}; shift ;;
        --fetch-only) _fetch_only=1; shift ;;
        *) _args+=("$1"); shift ;;
    esac
done

[[ $EUID -eq 0 ]] || die "run this as root (e.g. curl ... | sudo bash -s -- [options])"
[[ -f /etc/os-release ]] || die "/etc/os-release not found: cannot tell which distribution this is"
. /etc/os-release
_major=${VERSION_ID%%.*}

case " ${ID} ${ID_LIKE:-} " in
    *" opensuse-leap "*|*" sles "*|*" suse "*)
        _py=python3; _pkgs=(git-core python3)
        [[ $_major == 15 ]] && { _py=python3.11; _pkgs=(git-core python311); }
        _install() { zypper --non-interactive install -y "$@"; } ;;
    *" debian "*|*" ubuntu "*)
        _py=python3; _pkgs=(git python3)
        _install() { apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y "$@"; } ;;
    *" rhel "*|*" fedora "*|*" centos "*)
        _py=python3; _pkgs=(git-core python3)
        _install() { dnf install -y "$@"; } ;;
    *) die "unsupported distribution: ${PRETTY_NAME:-$ID}" ;;
esac

echo "- ${PRETTY_NAME:-$ID}: installing ${_pkgs[*]}"
_install "${_pkgs[@]}" || die "could not install ${_pkgs[*]}"
"$_py" -c 'import sys; sys.exit(sys.version_info < (3, 9))' || die "$_py is older than 3.9"

if [[ -n $_source ]]; then
    _repo=$_source
elif [[ -d $_dest/.git ]]; then
    git -C "$_dest" pull --ff-only || die "could not update $_dest"
    _repo=$_dest
else
    git clone --branch "$_branch" "$_repo_url" "$_dest" || die "could not clone $_repo_url"
    _repo=$_dest
fi
_setup="$_repo/setup_demo_server/setup_kvm_node.py"
[[ -f $_setup ]] || die "$_setup not found"

if (( _fetch_only )); then
    echo "Fetched into $_repo. Next: edit $_repo/setup_demo_server/lab.cfg (or let the setup write it) and run:"
    echo "  $_py $_setup"
    exit 0
fi

if [[ -r /dev/tty ]] && { : </dev/tty; } 2>/dev/null; then
    exec "$_py" "$_setup" "${_args[@]}" </dev/tty
fi
exec "$_py" "$_setup" "${_args[@]}"
