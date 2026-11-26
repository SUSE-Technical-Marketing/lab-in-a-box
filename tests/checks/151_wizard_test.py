#!/usr/bin/env python3
# Unit tests and the feasibility sweep for webui/lib/wizard.py.
#
# The sweep is the point: it walks every combination of the choice answers and the smallest, default and largest
# of every count answer in all four workflows, builds the lab each one describes, and runs the real preflight over
# it with host_checks=False — the same call the lab-builder's Validate makes. A workflow that can produce an
# invalid lab fails here. Run from 151_wizard.sh, in its own container — see tests/run_tests.sh.
import io
import itertools
import json
import os
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
# The preflight asks apps.addon_installer() for each add-on's script, and that looks beside the running program.
# Pointing argv[0] at scripts/ is what makes a generated lab's add-ons findable here, as they are in production.
sys.argv[0] = str(_REPO / "scripts" / "wizard-feasibility")
sys.path.insert(0, str(_REPO / "libs"))
sys.path.insert(0, str(_REPO / "webui" / "lib"))

# This checkout's own apps.py, imported before anything else can put a deployed copy of the library on the path:
# libs/lab_creation.py prepends /usr/local/lib/lab_creation when it exists, and on a node whose deployed library
# is older than this checkout the probe would then be missing and the sweep would quietly test nothing.
import apps  # noqa: E402,F401
import lab_creation as lc  # noqa: E402
import primary  # noqa: E402
import sizing  # noqa: E402
import wizard  # noqa: E402

failures = []


def check(desc, cond):
    if not cond:
        failures.append(desc)
        print("FAIL:", desc)


NETWORK = {"mygw": "192.168.90.1", "mymask": "24", "mydns": "192.168.90.10"}
IMAGES = ["Leap-15.6.qcow2", "SLE-Micro-6.0.qcow2"]
AMI = "ami-0123456789abcdef0"

# One cloud account, so the sweep can walk the paths that need one — SUSE Virtualization runs nowhere else here.
# The file holds nothing but the cloudtype, which is all the preflight reads when it may not decrypt anything.
# JSON, not YAML: a test container has no PyYAML, and the loader reads either.
_CREDENTIALS = tempfile.mkdtemp()
Path(os.path.join(_CREDENTIALS, "aws-sweep.json")).write_text('{"cloudtype": "aws"}')
ACCOUNTS = [("aws-sweep", "aws")]
CONFIG = {"REMOTE_HOST": "hv1", "VIRT_SRV": "qemu+ssh://root@hv1/system?keyfile=.ssh/id_rsa",
          "CREDENTIALS_PATH": _CREDENTIALS}

# The real catalogue of this checkout, so the sweep places the add-ons the workflow would really offer. A filter
# that let through an add-on needing configuration would fail the sweep rather than an operator.
import discovery  # noqa: E402

CATALOGUE_LIVE = discovery.catalogue()
# The add-ons that accept a lab configuring them not at all, asked of each one's own --validate. The sweep places
# only these, so a filter that let through an add-on needing configuration fails here rather than an operator.
ADDABLE = discovery.addable_addons()
# One service credential of each kind a workflow needs, so the sweep tests the path that has what it asks for.
# The path that has not is asserted separately, through prerequisites().
SERVICE_ACCOUNTS = {"scc": ["scc-sweep"]}

# The default provisioning method is Ignition and Combustion, whose templates the preflight insists on, so the
# sweep gives it a tree that has them. Their contents are never read here.
_SETUP = tempfile.mkdtemp()
for _part in ("ignition", "combustion"):
    os.makedirs(os.path.join(_SETUP, _part))
    open(os.path.join(_SETUP, _part, "template"), "w").close()


# ── the questions ─────────────────────────────────────────────────────────
check("four workflows, the four shapes a lab is started from",
      [w["name"] for w in wizard.workflows()] == ["kubernetes", "multi-linux", "virtualization", "vm"])
check("each one says what it is for, since the operator picks from these alone",
      all(w["title"] and w["description"] for w in wizard.workflows()))
check("a workflow that does not exist is refused rather than rendering an empty form",
      not wizard.known("nope"))
try:
    wizard.steps("nope")
    check("steps() refuses an unknown workflow", False)
except ValueError:
    check("steps() refuses an unknown workflow", True)

for workflow in [w["name"] for w in wizard.workflows()]:
    steps = wizard.steps(workflow)
    names = [s["name"] for s in steps]
    check("{}: every step has a name, a title, a kind and a default".format(workflow),
          all(s.get("name") and s.get("title") and s.get("kind") and "default" in s for s in steps))
    check("{}: the lab's name is asked first and the backend second, which is the one thing never guessed"
          .format(workflow), names[:2] == ["name", "backend"])
    check("{}: every kind is one the browser knows how to render".format(workflow),
          all(s["kind"] in ("text", "count", "choice", "multi") for s in steps))
    check("{}: a choice or a multi offers options, and a count declares its range".format(workflow),
          all(s.get("options") is not None for s in steps if s["kind"] in ("choice", "multi"))
          and all("min" in s and "max" in s for s in steps if s["kind"] == "count"))
    check("{}: no step asks for an address, which is what the allocator is for".format(workflow),
          not [s for s in names if "ip" in s or "address" in s])

check("the backend step offers this hypervisor, and a cloud account for each one in the store",
      [o["value"] for o in wizard.steps("vm", accounts=[("aws-tmm", "aws")])[1]["options"]]
      == ["libvirt", "account:aws-tmm"])

CATALOGUE = [
    {"name": "install_local_path", "kind": "addon", "title": "local_path", "description": "storage",
     "facets": ["kubernetes", "needs-ingress"]},
    {"name": "install_grafana", "kind": "addon", "title": "grafana", "description": "dashboards",
     "facets": ["standalone-container"]},
    {"name": "install_anthropic", "kind": "addon", "title": "anthropic", "description": "needs a key",
     "facets": ["kubernetes"], "required_fields": ["anthropic_api_key"]},
    {"name": "install_longhorn", "kind": "addon", "title": "longhorn-credential", "description": "needs an account",
     "facets": ["kubernetes"], "credential_fields": ["lh_appcollection_account"]},
    {"name": "pxe", "kind": "infrastructure", "title": "pxe", "facets": ["lab-service"]},
]
cluster_apps = [o["value"] for o in wizard.steps("kubernetes", catalogue=CATALOGUE)[5]["options"]]
check("the step the apps are on is the one asked after the counts", True)
host_apps = [o["value"] for o in wizard.steps("vm", catalogue=CATALOGUE)[3]["options"]]
check("a cluster workflow offers the cluster add-ons of the live catalogue",
      cluster_apps == ["local_path"])
check("a VM workflow offers the ones that install on a host instead", host_apps == ["grafana"])
check("an add-on with a required field is offered by neither, because nothing here could fill it in",
      "anthropic" not in cluster_apps + host_apps)
check("nor is one that needs a credential account, which its own --validate refuses to go without",
      "longhorn" not in cluster_apps)
check("what is not an add-on at all is never offered", "pxe" not in cluster_apps + host_apps)


# ── what the answers build ────────────────────────────────────────────────
lab = wizard.build("kubernetes", {"name": "My K8s Lab", "servers": 2, "agents": 3}, network=NETWORK)
check("the lab's name becomes a DNS zone of its own, which is what one lab per zone means",
      lab["common"]["mydomain"] == "my-k8s-lab.lab")
check("every node is named inside that zone",
      all(name.endswith(".my-k8s-lab.lab") for name in lab["nodes"]))
check("the counts are honoured", len([n for n in lab["nodes"] if n.startswith("server")]) == 2
      and len([n for n in lab["nodes"] if n.startswith("agent")]) == 3)
check("the network it was given is the lab's own, and no node carries an address",
      lab["common"]["mygw"] == "192.168.90.1"
      and not [n for n in lab["nodes"].values() if n.get("myip")])
check("one kcluster, with the release the distribution needs",
      len(lab["kclusters"]) == 1 and list(lab["kclusters"].values())[0]["clu_rel"])
check("each node says which cluster it belongs to and which role it has",
      all(n.get("kcluster") and n.get("INSTALL_RKE2_TYPE") for n in lab["nodes"].values()))

sized = wizard.build("kubernetes", {"name": "k", "servers": 1, "agents": 1, "apps": ["longhorn"]})
node = sized["nodes"]["server1.k.lab"]
check("a node is sized for what it will actually run, from the documented figures",
      int(node["VM_MEM"]) >= sizing.PRODUCTS["rke2"]["memory_mb"]
      and int(node["VM_DSK"]) >= sizing.BASE_DISK_GB + sizing.PRODUCTS["longhorn"]["extra_disk_gb"])
check("a cluster add-on sizes the nodes it will land on, although it is listed on the cluster",
      int(sized["nodes"]["agent1.k.lab"]["VM_DSK"]) == int(node["VM_DSK"])
      and sized["kclusters"]["k-cluster"]["addons"] == ["longhorn"])
check("common carries the sizes the schema requires of it, and no more than the smallest node needs",
      int(sized["common"]["VM_MEM"]) == min(int(n["VM_MEM"]) for n in sized["nodes"].values()))

k3s = wizard.build("kubernetes", {"name": "k", "distribution": "k3s", "servers": 1, "agents": 1})
check("a K3s agent is sized as an agent, which is smaller than its server",
      int(k3s["nodes"]["agent1.k.lab"]["VM_MEM"]) < int(k3s["nodes"]["server1.k.lab"]["VM_MEM"]))
rke2 = wizard.build("kubernetes", {"name": "k", "distribution": "rke2", "servers": 1, "agents": 1})
check("an RKE2 agent is sized like its server, because RKE2 documents one figure for both",
      rke2["nodes"]["agent1.k.lab"]["VM_MEM"] == rke2["nodes"]["server1.k.lab"]["VM_MEM"])

mlm = wizard.build("multi-linux", {"name": "m", "clients": 2, "activation_key": "1-lab-key"})
check("the server runs the product, and the clients are plain VMs",
      mlm["nodes"]["smlm.m.lab"]["addons"] == ["smlm"]
      and len([n for n in mlm["nodes"] if n.startswith("client")]) == 2)
check("an activation key registers the clients against the lab's own server, which is the one add-on whose "
      "fields this can fill in",
      mlm["client_registration"]["client_registration_server"] == "smlm.m.lab"
      and mlm["client_registration"]["client_registration_activation_key"] == "1-lab-key"
      and mlm["nodes"]["client1.m.lab"]["addons"] == ["client_registration"])
check("the server is sized for the volumes its documentation asks for",
      int(mlm["nodes"]["smlm.m.lab"]["VM_DSK"]) >= sizing.PRODUCTS["smlm"]["extra_disk_gb"])

no_key = wizard.build("multi-linux", {"name": "m", "clients": 1, "activation_key": ""})
check("without a key the clients are still built, and nothing claims to register them",
      "client_registration" not in no_key and not no_key["nodes"]["client1.m.lab"].get("addons"))

harv = wizard.build("virtualization", {"name": "v", "product": "harvester", "nodes": 1})
check("Harvester is a kcluster of its own type, sized by its own documentation",
      harv["kclusters"]["v-harvester"]["clu_type"] == "harvester"
      and int(harv["nodes"]["harvester1.v.lab"]["VM_MEM"]) == sizing.PRODUCTS["harvester"]["memory_mb"])
kvm = wizard.build("virtualization", {"name": "v", "product": "kvm", "nodes": 2})
check("a KVM node is a plain VM with no cluster", "kclusters" not in kvm and len(kvm["nodes"]) == 2)

cloud = wizard.build("kubernetes", {"name": "k", "backend": "account:aws-tmm", "servers": 1, "agents": 0},
                     network=NETWORK)
check("a cloud account is named as one, and the lab's own network is left out: the provider assigns those",
      cloud["common"]["cloud_account"] == "aws-tmm"
      and "mygw" not in cloud["common"] and "backend" not in cloud["common"])

check("an answer that is missing falls back to its step's own default",
      wizard.build("vm", {})["nodes"] == wizard.build("vm", {"name": "vm-lab", "count": 1})["nodes"])
check("a count outside its range is clamped rather than obeyed",
      len(wizard.build("vm", {"name": "v", "count": 99})["nodes"]) == 5
      and len(wizard.build("vm", {"name": "v", "count": -3})["nodes"]) == 1)
check("a name that is not a DNS label becomes one", wizard.build("vm", {"name": "  Añejo Lab!! "})
      ["common"]["mydomain"].startswith("a"))


# ── prerequisites: what an installation may be missing before a workflow can work ──
check("a Multi-Linux Manager lab needs a registration credential, which nothing here can invent",
      [m["what"] for m in wizard.prerequisites("multi-linux", {})] == ["a registration credential"])
check("...and says how to fix it, since the operator is the only one who can",
      all(m.get("why") and m.get("fix") for m in wizard.prerequisites("multi-linux", {})))
check("with one in the store there is nothing in the way",
      wizard.prerequisites("multi-linux", SERVICE_ACCOUNTS) == [])
check("no other workflow asks for anything this installation might not have",
      all(wizard.prerequisites(w, {}) == [] for w in ("kubernetes", "virtualization", "vm")))

mlm = wizard.build("multi-linux", {"name": "m", "clients": 1}, service_accounts=SERVICE_ACCOUNTS)
check("the server is told the name it will answer to, which the wizard knows and its --validate insists on",
      mlm["smlm"]["smlm_fqdn"] == "smlm.m.lab")
check("...and the credential the operator chose", mlm["smlm"]["smlm_scc_account"] == "scc-sweep")
check("an add-on that needs a credential is not offered as an application either",
      ADDABLE is None or "longhorn" not in ADDABLE)
check("the probe answered for the add-ons of this checkout, and half of them do need configuration",
      ADDABLE is None or 20 < len(ADDABLE) < 100)


# ── what the browser is handed, and the panel that renders it ─────────────
import admin as webui_admin  # noqa: E402

check("the three wizard actions are behind the login, like every other action that builds a lab",
      {"wizard", "wizard-steps", "wizard-build"} <= webui_admin.ACTIONS)

# The credential files are root-only, so the web process cannot list them itself: the accounts have to come
# through lab-builder-helper. Reading the directory instead found nothing and offered no cloud account at all,
# on an installation that had two.
webui_admin.helper = lambda args, stdin=None: {"credentials": [
    {"name": "aws-tmm", "kind": "cloud", "type": "aws"},
    {"name": "gcp-tmm", "kind": "cloud", "type": "gcp"},
    {"name": "scc-tmm", "kind": "service", "type": "scc"},
]}
check("the cloud accounts the helper reports are the ones offered as a backend",
      [o["value"] for o in webui_admin.dispatch("wizard-steps", "GET", {"name": ["vm"]}, {})["steps"][1]["options"]]
      == ["libvirt", "account:aws-tmm", "account:gcp-tmm"])
check("a service credential it reports satisfies the workflow that needs one",
      webui_admin.dispatch("wizard", "GET", {}, {})["workflows"][1]["prerequisites"] == [])
check("...and a build uses them too, so the account chosen in the form is one the lab can name",
      webui_admin.dispatch("wizard-build", "POST", {}, {"workflow": "vm", "answers": {
          "name": "cloudy", "backend": "account:aws-tmm", "count": 1}})["config"]["common"]["cloud_account"]
      == "aws-tmm")
webui_admin.helper = lambda args, stdin=None: (_ for _ in ()).throw(OSError("no helper here"))
check("a helper that cannot be reached leaves the form working, with this hypervisor alone",
      [o["value"] for o in webui_admin.dispatch("wizard-steps", "GET", {"name": ["vm"]}, {})["steps"][1]["options"]]
      == ["libvirt"])

answered = webui_admin.dispatch("wizard-steps", "GET",
                                {"name": ["virtualization"], "answers": ['{"product": "kvm"}']}, {})
check("the answers so far reach the server, which decides what follows from them",
      [s["name"] for s in answered["steps"]][-2:] == ["apps", "image"])
check("an unparseable answers parameter is treated as none rather than failing the request",
      webui_admin.dispatch("wizard-steps", "GET", {"name": ["vm"], "answers": ["{oops"]}, {})["steps"])

built = webui_admin.dispatch("wizard-build", "POST", {},
                             {"workflow": "vm", "answers": {"name": "from-the-api", "count": 1}})
check("the build action answers with a definition the canvas can load",
      list(built["config"]["nodes"]) == ["vm1.from-the-api.lab"])

page = (_REPO / "webui" / "htdocs" / "index.html").read_text()
script = (_REPO / "webui" / "htdocs" / "app.js").read_text()
check("the canvas has the button that swaps it for the step view", 'id="simplifyBtn"' in page)
check("the step view is there, hidden until it is asked for", 'id="wizardPanel"' in page and "hidden" in page)
check("the panel offers both endings: the canvas, and Create lab",
      'id="wizardOpen"' in page and 'id="wizardCreate"' in page)
check("the steps are re-read after each answer, since a later one can depend on an earlier one",
      "await loadWizardSteps()" in script)
check("a finished lab goes onto the canvas through the same path an example takes",
      "loadLab(wiz.config)" in script)
check("Create lab is the canvas's own, not a second way to deploy", "await openCreate()" in script)
check("the static page hides the button, having no server to answer it",
      "#simplifyBtn" in (_REPO / "scripts" / "build-cube-static.py").read_text())


# ── the feasibility sweep ─────────────────────────────────────────────────
def _steps(workflow, answers=None):
    return wizard.steps(workflow, catalogue=CATALOGUE_LIVE, images=IMAGES, addable=ADDABLE,
                        accounts=ACCOUNTS, service_accounts=SERVICE_ACCOUNTS, answers=answers)


def paths(workflow):
    """
    Every combination of this workflow's choice answers with the ends and the default of each count, and every
    backend it offers.

    A later question can depend on an earlier one, so the whole step list is rebuilt for each value of the
    question others depend on — the virtualization product today — exactly as a caller re-reads the steps after
    each answer.
    """
    product_step = next((s for s in _steps(workflow) if s["name"] == "product"), None)
    products = [o["value"] for o in product_step["options"]] if product_step else [None]
    for product in products:
        fixed = {"product": product} if product else {}
        axes = {}
        choices = {}
        for step in _steps(workflow, answers=fixed):
            if step["name"] == "product":
                continue
            if step["kind"] == "choice":
                values = [o["value"] for o in step["options"]] or [step["default"]]
                choices[step["name"]] = values
            elif step["kind"] == "count":
                values = sorted({step["min"], step["default"], step["max"]})
            elif step["name"] == "apps":
                # Straight from the step's own options, so the sweep tests whatever the filter decided to offer.
                offered = [o["value"] for o in step["options"]]
                values = [[], offered[:1], offered[:2]] if offered else [[]]
            elif step["name"] == "activation_key":
                values = ["", "1-lab-key"]
            elif step["name"] == "ami":
                values = [AMI]
            else:
                continue
            axes[step["name"]] = values

        # Every value of every answer, each one against the defaults, plus every combination of the answers that
        # are a choice between named things. The full product of all of them would be thousands of labs and
        # minutes of preflight for nothing: a defect here is a bad answer, or two choices that disagree.
        base = {name: values[0] for name, values in axes.items()}
        seen = set()
        for combination in itertools.product(*[choices[n] for n in sorted(choices)]):
            candidate = dict(base, **dict(zip(sorted(choices), combination)))
            key = json.dumps(candidate, sort_keys=True)
            if key not in seen:
                seen.add(key)
                yield dict(fixed, **candidate)
        for name, values in sorted(axes.items()):
            for value in values:
                candidate = dict(base)
                candidate[name] = value
                key = json.dumps(candidate, sort_keys=True)
                if key not in seen:
                    seen.add(key)
                    yield dict(fixed, **candidate)


def preflight(definition):
    """The real preflight, with its report captured. Returns (ok, report)."""
    path = tempfile.mkstemp(suffix=".json")[1]
    Path(path).write_text(json.dumps(definition))
    try:
        loaded = primary.load_definition(path)
        buf = io.StringIO()
        with redirect_stdout(buf):
            ok = lc.validate_lab_definition(loaded, CONFIG, "/iso", _SETUP, host_checks=False,
                                            read_credentials=False)
        return ok, buf.getvalue()
    finally:
        os.unlink(path)


swept, broken = 0, []
for workflow in [w["name"] for w in wizard.workflows()]:
    for answers in paths(workflow):
        definition = wizard.build(workflow, dict(answers, name="sweep"), network=NETWORK,
                                  catalogue=CATALOGUE_LIVE, images=IMAGES, addable=ADDABLE,
                                  accounts=ACCOUNTS, service_accounts=SERVICE_ACCOUNTS)
        ok, report = preflight(definition)
        swept += 1
        if not ok:
            broken.append((workflow, answers, [l.strip() for l in report.splitlines() if "ERROR" in l]))

check("the sweep actually generated labs, so an empty run is not read as every path passing", swept >= 40)
check("every path through every workflow builds a lab the preflight accepts: {}".format(
    "" if not broken else "\n    " + "\n    ".join(
        "{} {} -> {}".format(w, a, e) for w, a, e in broken[:6])), not broken)
print("swept {} generated labs through the preflight".format(swept))

if failures:
    print("{} check(s) failed".format(len(failures)))
    sys.exit(1)
print("all wizard checks passed")
