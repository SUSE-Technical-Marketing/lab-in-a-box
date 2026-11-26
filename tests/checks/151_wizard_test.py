#!/usr/bin/env python3
# Unit tests and the feasibility sweep for webui/lib/wizard.py.
#
# The sweep is the point: it walks every combination of the choice answers and the smallest, default and largest
# of every count answer in all five workflows, builds the lab each one describes, and runs the real preflight over
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
check("five workflows, the five shapes a lab is started from",
      [w["name"] for w in wizard.workflows()]
      == ["kubernetes", "multi-linux", "virtualization", "vm", "example"])
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
    # The example workflow is the exception by design: the example it starts from already answers both.
    check("{}: the lab's name is asked first and the backend second, which is the one thing never guessed"
          .format(workflow), names[:2] == ["name", "backend"] or workflow == "example")
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

# Where an add-on can go is its own declared layer, not the catalogue filter's vocabulary.
CATALOGUE = [
    {"name": "install_local_path", "kind": "addon", "title": "local_path", "description": "storage",
     "layers": ["kubernetes"], "requires_cluster": ["ingress"]},
    {"name": "install_grafana", "kind": "addon", "title": "grafana", "description": "dashboards",
     "layers": ["standalone-container"]},
    {"name": "install_anthropic", "kind": "addon", "title": "anthropic", "description": "needs a key",
     "layers": ["kubernetes"], "required_fields": ["anthropic_api_key"]},
    {"name": "install_longhorn", "kind": "addon", "title": "longhorn-credential", "description": "needs an account",
     "layers": ["kubernetes"], "credential_fields": ["lh_appcollection_account"]},
    {"name": "pxe", "kind": "infrastructure", "title": "pxe", "layers": []},
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
import api  # noqa: E402

# Simplify asks questions and writes a definition, which is what the public half of the API is for: putting its
# actions behind the login made the browser raise a password dialog on the button itself, and refusing it left
# the panel open with nothing in it. Creating or saving what it wrote still needs the login.
check("the three wizard actions are not behind the login",
      not ({"wizard", "wizard-steps", "wizard-build"} & webui_admin.ACTIONS)
      and {"wizard", "wizard-steps", "wizard-build"} == webui_admin.WIZARD_ACTIONS)
check("creating and saving a lab still are", {"create", "save"} <= webui_admin.ACTIONS)


def public(action, method="GET", params=None, body=None):
    """The action as anyone reaches it: no user, no HTTPS."""
    status, obj = api.dispatch(action, method, params or {}, body)
    check("the public API answers {} with 200".format(action), status == 200)
    return obj


def logged_in(action, method="GET", params=None, body=None):
    """The same action with a login, which is what adds the credential store's accounts."""
    status, obj = api.dispatch(action, method, params or {}, body, user="op", https=True)
    check("the login API answers {} with 200".format(action), status == 200)
    return obj


webui_admin.authorized = lambda user: True

# The credential files are root-only, so the web process cannot list them itself: the accounts have to come
# through lab-builder-helper. Reading the directory instead found nothing and offered no cloud account at all,
# on an installation that had two.
webui_admin.helper = lambda args, stdin=None: {"credentials": [
    {"name": "aws-tmm", "kind": "cloud", "type": "aws"},
    {"name": "gcp-tmm", "kind": "cloud", "type": "gcp"},
    {"name": "scc-tmm", "kind": "service", "type": "scc"},
]}
check("the cloud accounts the helper reports are the ones offered as a backend",
      [o["value"] for o in logged_in("wizard-steps", params={"name": ["vm"]})["steps"][1]["options"]]
      == ["libvirt", "account:aws-tmm", "account:gcp-tmm"])
check("a service credential it reports satisfies the workflow that needs one",
      logged_in("wizard")["workflows"][1]["prerequisites"] == [])
check("...and a build uses them too, so the account chosen in the form is one the lab can name",
      logged_in("wizard-build", "POST", body=json.dumps({"workflow": "vm", "answers": {
          "name": "cloudy", "backend": "account:aws-tmm", "count": 1}}).encode())["config"]["common"]["cloud_account"]
      == "aws-tmm")
# Without a login the same questions are asked, and the store's account names are not among the answers.
check("no login names no account: the form offers this hypervisor alone",
      [o["value"] for o in public("wizard-steps", params={"name": ["vm"]})["steps"][1]["options"]] == ["libvirt"])
check("and says where a cloud account would come from",
      "credential store" in public("wizard-steps", params={"name": ["vm"]})["steps"][1]["help"])
check("every workflow is still offered without a login", len(public("wizard")["workflows"]) == 5)
check("and a lab still builds without one",
      list(public("wizard-build", "POST",
                  body=json.dumps({"workflow": "vm",
                                   "answers": {"name": "anon", "count": 1}}).encode())["config"]["nodes"])
      == ["vm1.anon.lab"])
webui_admin.helper = lambda args, stdin=None: (_ for _ in ()).throw(OSError("no helper here"))
check("a helper that cannot be reached leaves the form working, with this hypervisor alone",
      [o["value"] for o in logged_in("wizard-steps", params={"name": ["vm"]})["steps"][1]["options"]]
      == ["libvirt"])

answered = public("wizard-steps", params={"name": ["virtualization"], "answers": ['{"product": "kvm"}']})
check("the answers so far reach the server, which decides what follows from them",
      [s["name"] for s in answered["steps"]][-2:] == ["apps", "image"])
check("an unparseable answers parameter is treated as none rather than failing the request",
      public("wizard-steps", params={"name": ["vm"], "answers": ["{oops"]})["steps"])

built = logged_in("wizard-build", "POST",
                  body=json.dumps({"workflow": "vm", "answers": {"name": "from-the-api", "count": 1}}).encode())
check("the build action answers with a definition the canvas can load",
      list(built["config"]["nodes"]) == ["vm1.from-the-api.lab"])

page = (_REPO / "webui" / "htdocs" / "index.html").read_text()
script = (_REPO / "webui" / "htdocs" / "app.js").read_text()
style = (_REPO / "webui" / "htdocs" / "style.css").read_text()
check("the canvas has the button that opens the step view", 'id="simplifyBtn"' in page)
# A panel inside <main class="layout"> took a column of the grid and hid the catalogue and the canvas to get it,
# which moved the lab panel. It is a dialog like every other one here, so entering it changes no layout at all.
check("the step view is a dialog over the page", '<dialog id="wizardPanel"' in page)
check("it is not a panel in the layout grid",
      'class="panel wizard-area"' not in page and "wizard-area" not in style)
check("and nothing in the page is hidden to show it",
      "wizard-on" not in style and "wizard-on" not in script)
# What this asserts is only the wiring; that the click really opens the dialog is run, in a sandboxed DOM and
# through these same helpers, by tests/checks/07_webui_js_smoke.js.
check("it opens only once the server has answered, so a failed read leaves the canvas alone",
      'showDialog("#wizardPanel")' in script and "toastError(e.message);\n    return;" in script)
check("and it opens through the dialog helper the rest of the page uses",
      "function showDialog(sel)" in script and "openDialog" not in script)
check("Simplify asks the public API, so the browser never raises a login dialog on the button",
      "state.auth && state.auth.user ? ADMIN_API : API" in script
      and 'await wizardCall("wizard")' in script)
check("the panel offers every ending the brief asked for: save, download, the canvas and Create lab",
      'id="wizardOpen"' in page and 'id="wizardCreate"' in page
      and 'id="wizardDownload"' in page and 'id="wizardSave"' in page)
check("saving and downloading reuse the canvas's own, rather than a second implementation of each",
      "downloadLab(wiz.config, wizardLabName())" in script and "saveLab(wiz.config, wizardLabName())" in script)
check("downloading the file needs no login, and Save to server says why when it cannot be used",
      "$(\"#wizardSave\").disabled = Boolean(serverLock())" in script)
check("the steps are re-read after each answer, since a later one can depend on an earlier one",
      "await loadWizardSteps()" in script)
check("a finished lab goes onto the canvas through the same path an example takes",
      "loadLab(wiz.config)" in script)
check("Create lab is the canvas's own, not a second way to deploy", "await openCreate()" in script)
check("the static page hides the button, having no server to answer it",
      "#simplifyBtn" in (_REPO / "scripts" / "build-cube-static.py").read_text())


# ── starting from an example lab ──────────────────────────────────────────
ex_steps = wizard.steps("example", examples=[
    {"name": "cluster.json.template", "title": "cluster", "description": "", "nodes": 6, "kclusters": 1,
     "addons": ["longhorn", "neuvector"]},
    {"name": "portable-k3s-rancher.json.template", "title": "portable k3s rancher", "description": "One node.",
     "nodes": 1, "kclusters": 1, "addons": []},
])
check("the example workflow asks which example, and nothing the example already answers",
      [s["name"] for s in ex_steps] == ["example", "name"])
check("every example this installation has is offered",
      [o["value"] for o in ex_steps[0]["options"]]
      == ["cluster.json.template", "portable-k3s-rancher.json.template"])
check("each one says what it holds, counting add-ons listed by name",
      ex_steps[0]["options"][0]["help"] == "6 VMs, 1 cluster, 2 add-ons")
check("...and a description is not followed by a stray full stop",
      ex_steps[0]["options"][1]["help"] == "One node. 1 VM, 1 cluster")
check("the file name defaults to the example's own, without its extension",
      wizard.steps("example", examples=[{"name": "solar-system-lab.json", "title": "solar"}],
                   answers={"example": "solar-system-lab.json"})[1]["default"] == "solar-system-lab")
built_example = wizard.build("example", {"example": "cluster.json.template", "name": "mine"},
                             example_definition={"nodes": {"n1.a.lab": {"myip": "10.0.0.1"}}})
check("the lab built is the example itself, unrewritten",
      built_example == {"nodes": {"n1.a.lab": {"myip": "10.0.0.1"}}})
source = {"nodes": {"n1.a.lab": {"myip": "10.0.0.1"}}}
twice = wizard.build("example", {"example": "cluster.json.template"}, example_definition=source)
twice["nodes"]["n1.a.lab"]["myip"] = "changed"
check("...and a copy of it, so editing the lab on the canvas cannot change the example",
      source["nodes"]["n1.a.lab"]["myip"] == "10.0.0.1")
try:
    wizard.build("example", {"example": "nothing-here.json"})
    check("an example that cannot be resolved is refused rather than built empty", False)
except ValueError:
    pass


# ── the feasibility sweep ─────────────────────────────────────────────────
# The example workflow hands back a shipped lab as it is written, so sweeping it runs every example this
# checkout ships through the same preflight: lab_examples resolves a name to its file, as discovery does.
import lab_examples  # noqa: E402

EXAMPLES = lab_examples.catalogue(config={})


def _example_definition(name):
    return primary.load_definition(lab_examples.path_of(name, config={})) if name else None


def _steps(workflow, answers=None):
    return wizard.steps(workflow, catalogue=CATALOGUE_LIVE, images=IMAGES, addable=ADDABLE,
                        accounts=ACCOUNTS, service_accounts=SERVICE_ACCOUNTS, examples=EXAMPLES, answers=answers)


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
        try:
            with redirect_stdout(buf):
                ok = lc.validate_lab_definition(loaded, CONFIG, "/iso", _SETUP, host_checks=False,
                                                read_credentials=False)
        except SystemExit as e:
            # Part of the preflight reports by die() rather than by returning False. Without this the sweep
            # exited on the first such lab, printing nothing and failing the check with no report of why.
            return False, buf.getvalue() + "\nERROR: exited: {}\n".format(e)
        return ok, buf.getvalue()
    finally:
        os.unlink(path)


# The example workflow is left out of the sweep on purpose: it generates nothing, it hands back a lab this
# project ships, so sweeping it would test the examples rather than the builder. What it does contribute, that
# the file arrives unchanged, is checked above and again right below.
swept, broken = 0, []
for workflow in [w["name"] for w in wizard.workflows() if w["name"] != "example"]:
    for answers in paths(workflow):
        definition = wizard.build(workflow, dict(answers, name="sweep"), network=NETWORK,
                                  catalogue=CATALOGUE_LIVE, images=IMAGES, addable=ADDABLE,
                                  accounts=ACCOUNTS, service_accounts=SERVICE_ACCOUNTS, examples=EXAMPLES,
                                  example_definition=_example_definition(answers.get("example")))
        ok, report = preflight(definition)
        swept += 1
        if not ok:
            broken.append((workflow, answers, [l.strip() for l in report.splitlines() if "ERROR" in l]))

check("the sweep actually generated labs, so an empty run is not read as every path passing", swept >= 40)
check("every path through every workflow builds a lab the preflight accepts: {}".format(
    "" if not broken else "\n    " + "\n    ".join(
        "{} {} -> {}".format(w, a, e) for w, a, e in broken[:6])), not broken)
print("swept {} generated labs through the preflight".format(swept))

# Every example this checkout ships is reachable through the workflow and arrives as its file says, which is the
# whole of what the workflow does.
offered = [o["value"] for o in _steps("example")[0]["options"]]
check("every shipped example is offered by the workflow: {}".format(", ".join(offered)),
      sorted(offered) == sorted(e["name"] for e in EXAMPLES) and len(offered) >= 4)
unchanged = []
for name in offered:
    on_disk = _example_definition(name)
    handed = wizard.build("example", {"example": name}, examples=EXAMPLES, example_definition=on_disk)
    if handed != on_disk:
        unchanged.append(name)
check("and each one is handed over exactly as written: {}".format(", ".join(unchanged) or "all unchanged"),
      not unchanged)

if failures:
    print("{} check(s) failed".format(len(failures)))
    sys.exit(1)
print("all wizard checks passed")
