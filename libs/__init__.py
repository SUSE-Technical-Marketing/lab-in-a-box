# Part of lab-in-a-box — Python library package
# Author/s: Raul Mahiques
# License: GPLv3
#
# Python equivalents of the bash libraries in this directory.
# Install path: /usr/local/lib/lab_creation/
#
# Usage from a script:
#   import sys; sys.path.insert(0, "/usr/local/lib/lab_creation")
#   from primary import load_definition, load_config
#   from lab_creation import ssh_run, load_vm_vars, add_to_dns
#   from k8s import get_distro, first_server_node
