#!/bin/bash
# An add-on written in bash (install_hello.sh) next to the Python ones: install_automation_node_scripts.sh deploys it
# as /usr/local/bin/install_hello, and libs/apps.py, the web UI discovery and lab validation use it only through its
# own executable contract (--schema json, --validate). Independent container — see tests/run_tests.sh.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit
_fail=0
fail() { echo "FAIL: $*"; _fail=1; }

_src=$(mktemp -d)
git -c safe.directory="$PWD" ls-files -z | while IFS= read -r -d '' f; do [[ -e "$f" ]] && printf '%s\0' "$f"; done \
    | tar --null -T - -cf - | tar -xf - -C "${_src}"

cat > "${_src}/scripts/install_hello.sh" <<'ADDON'
#!/bin/bash
# An add-on in bash: prints its schema, validates its section, installs nothing.
case "${1:-}" in
    --schema)
        cat <<'JSON'
{"section": "hello", "description": "Says hello", "fields": [{"name": "greeting", "type": "string", "required": true}],
 "capabilities": {"targets": ["vm"], "layers": ["os-native"], "requires_kubernetes": null, "aux_services": []}}
JSON
        ;;
    --capabilities) echo '{"targets": ["vm"], "layers": ["os-native"], "requires_kubernetes": null, "aux_services": []}' ;;
    --validate) grep -q '"greeting"' "$2" || { echo "[ERROR] hello.greeting is required"; exit 1; } ;;
    --version) echo "install_hello 1.0" ;;
    *) echo "hello on ${_vm_name:-?}" ;;
esac
ADDON
chmod 0755 "${_src}/scripts/install_hello.sh"

(cd "${_src}" && _webui_mode=off _backup=off bash install_automation_node_scripts.sh >/tmp/install.log 2>&1) \
    || { tail -20 /tmp/install.log; fail "install_automation_node_scripts.sh failed"; }
[[ -x /usr/local/bin/install_hello ]] || fail "install_hello.sh was not deployed as /usr/local/bin/install_hello"
[[ ! -e /usr/local/bin/install_hello.sh ]] || fail "the deployed add-on kept its .sh extension"
[[ -x /usr/local/bin/install_mariadb && ! -e /usr/local/bin/install_mariadb.py ]] || fail "install_mariadb.py not deployed as install_mariadb"
[[ "$(_vm_name=vm1 install_hello lab.json)" == "hello on vm1" ]] || fail "install_hello does not run from PATH"

export LAB_ADDON_CACHE=/tmp/addon-cache.json
python3.11 - "${_src}" <<'PY' || _fail=1
import json, os, sys, tempfile
src = sys.argv[1]
sys.path.insert(0, os.path.join(src, "libs"))
sys.path.insert(0, os.path.join(src, "webui", "lib"))
os.environ["LABBUILDER_SCRIPTS_DIR"] = os.path.join(src, "scripts")
os.environ["LABBUILDER_LIBS_DIR"] = os.path.join(src, "libs")
os.environ["LABBUILDER_STATUS_FILE"] = os.devnull
import subprocess
import apps
import discovery

bad = []
def check(desc, cond):
    if not cond:
        bad.append(desc)
        print("FAIL:", desc)

plugin = apps.load_plugin("hello")
check("load_plugin('hello') finds install_hello on PATH and reads its capabilities",
      plugin["targets"] == ["vm"] and plugin["layers"] == ["os-native"] and plugin["requires_kubernetes"] is None)
items = {it["name"]: it for it in discovery.discover()}
check("discover() lists the bash add-on by its name without extension", "install_hello" in items)
check("discover() reports its fields and capabilities",
      items.get("install_hello", {}).get("field_count") == 1 and items["install_hello"]["targets"] == ["vm"])
check("discover() still lists the Python add-ons", "install_mariadb" in items)
sc = discovery.schema("install_hello")
check("schema('install_hello') is the add-on's own output", sc.get("section") == "hello" and sc["fields"][0]["name"] == "greeting")

lab = {"common": {}, "nodes": {"vm1.lab": {"myip": "10.0.0.1", "addons": ["hello"]}}, "kclusters": {}}
with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
    json.dump(lab, f)
r = subprocess.run(["install_hello", "--validate", f.name], stdout=subprocess.PIPE, universal_newlines=True)
check("install_hello --validate rejects a lab without hello.greeting", r.returncode == 1 and "[ERROR]" in r.stdout)
sys.exit(1 if bad else 0)
PY

[[ ${_fail} -eq 0 ]] && echo "all language-independent add-on checks passed"
exit ${_fail}
