"""
webui/lib/wizard.py: the step-by-step lab builder — its questions, and the lab definition they build.

The three-panel canvas asks for everything a lab definition can hold. This asks four or five questions and works
the rest out, for the operator who wants a working lab of a familiar shape rather than control over every field.

It lives on the server, not in the browser, for two reasons: a generated definition has to be checked by the same
preflight a hand-written one goes through, which is Python (tests/checks/151_wizard.sh walks every path through
every workflow and validates what comes out), and the same question-and-answer shape is what a chat box driving
the MCP tools would need.

    workflows()                     the four shapes a lab can be started from
    steps(workflow, ...)            that workflow's questions, with their options filled in from what this
                                    installation actually has: the add-ons of the live catalogue, the cloud
                                    accounts of the credential store, the images the hypervisors report
    build(workflow, answers, ...)   the lab definition those answers describe

What it decides for itself, and why:

    the backend        is never decided: it is asked, because it is the one answer nothing else implies and
                       getting it wrong means a lab built in the wrong place.
    the addresses      are left out, so the run allocates them from the lab's own subnet (libs/ipalloc.py) and
                       nothing has to be kept in step with the gateway. `network` supplies that subnet, which the
                       web layer reads off the automation node itself.
    the sizes          come from each product's own documentation (libs/sizing.py), per node, for what that node
                       will actually run.
    the add-ons        offered are the ones that need no configuration of their own: an add-on with a required
                       field cannot be added blind, and the canvas is where those belong.
"""

import copy
import re
import secrets

import sizing

# A node's own fields, in the order a reader of the generated file wants them.
_CLUSTER_RELEASE = "stable"
_DEFAULT_DOMAIN_SUFFIX = "lab"

# Which sizing products each role runs, so a node is sized for its own job rather than the lab's largest one.
_ROLE_PRODUCTS = {
    "k8s-server": ("{distribution}",),
    "k8s-agent": ("{distribution}-agent",),
    "smlm": ("{product}",),
    "client": (),
    "harvester": ("harvester",),
    "kvm": (),
    "vm": (),
}

WORKFLOWS = (
    {
        "name": "kubernetes",
        "title": "A Kubernetes cluster",
        "description": "One or more RKE2 or K3s nodes, with the applications you pick running on them.",
    },
    {
        "name": "multi-linux",
        "title": "Multi-Linux management",
        "description": "A SUSE Multi-Linux Manager or Uyuni server, with example clients to register against it.",
    },
    {
        "name": "virtualization",
        "title": "A virtualization node",
        "description": "SUSE Virtualization (Harvester), or a node prepared as a KVM hypervisor of its own.",
    },
    {
        "name": "vm",
        "title": "Plain virtual machines",
        "description": "One or more VMs with no cluster, and whatever you want installed on them.",
    },
    {
        "name": "example",
        "title": "An example lab",
        "description": "Start from one of the labs this installation ships, unchanged, and adjust it on the "
                       "canvas if you want to.",
    },
)


def workflows():
    """The four shapes, as the UI lists them."""
    return [dict(w) for w in WORKFLOWS]


def known(workflow):
    """Whether `workflow` is one of them."""
    return any(w["name"] == workflow for w in WORKFLOWS)


def slug(text, fallback="lab"):
    """`text` as a DNS label: lowercase, letters, digits and dashes, never empty and never starting with a dash."""
    out = re.sub(r"[^a-z0-9-]+", "-", str(text or "").strip().lower()).strip("-")
    return out or fallback


def domain_for(name):
    """The lab's own DNS zone, from its name: one zone per lab, which is what the DNS design expects."""
    return "{}.{}".format(slug(name), _DEFAULT_DOMAIN_SUFFIX)


def _token():
    """A cluster join token: a secret the cluster needs and that nobody has to choose."""
    return secrets.token_hex(16)


# What to say about each distribution the lab definition accepts. A value missing from here is still offered,
# labelled with its own name: the list of distributions is the schema's, never this table's (see
# _distribution_options).
_DISTRIBUTION_HELP = {
    "rke2": ("RKE2", "SUSE's own, and the default here. Brings an ingress controller and a storage class."),
    "k3s": ("K3s", "Lighter, and the smallest cluster this can build. Brings both as well."),
    "vanilla": ("Upstream Kubernetes", "Built with kubeadm from the community packages, with flannel. It brings "
                                       "neither an ingress controller nor a storage class, so an add-on that "
                                       "needs one pulls it in itself."),
}

# clu_type harvester is an appliance the virtualization workflow builds from an AMI, not a cluster this workflow
# can put a node or an add-on on, so it is not a distribution to choose here.
_NOT_A_DISTRIBUTION = ("harvester",)


def _cluster_release(distribution):
    """
    The clu_rel this distribution needs: rke2 and k3s take a channel their own installers resolve, upstream
    Kubernetes installs a named minor (VanillaDistro.minor() refuses a channel), and Harvester takes a release
    tag of its own project, where a channel name is not accepted at all.
    """
    if distribution == "vanilla":
        import k8s
        return k8s.VanillaDistro.DEFAULT_REL
    if distribution == "harvester":
        import releases
        return releases.newest("harvester")
    return _CLUSTER_RELEASE


def _distribution_options():
    """
    The Kubernetes distributions on offer, read from the lab definition's own clu_type enum (libs/
    lab_schema_base.py) so the form can never accept less than the lab format does: a distribution added to the
    schema is offered here without this file being touched.
    """
    import lab_schema_base
    fields = lab_schema_base.base_lab_schema()["sections"]["kclusters"]["fields"]
    enum = next((f.get("enum") or [] for f in fields if f.get("name") == "clu_type"), [])
    options = []
    for value in enum:
        if value in _NOT_A_DISTRIBUTION:
            continue
        label, help_text = _DISTRIBUTION_HELP.get(value, (value, ""))
        options.append({"value": value, "label": label, "help": help_text})
    return options


def _example_options(examples):
    """
    The example labs on offer, from libs/lab_examples.py's own catalogue (shipped and operator-owned alike), so
    the form lists whatever this installation has rather than a copy of the repository's examples/ directory.
    """
    def how_many(value):
        """The catalogue counts VMs as a number and lists add-ons by name; either shape is a count here."""
        if isinstance(value, (list, tuple, dict, set)):
            return len(value)
        try:
            return int(value or 0)
        except (TypeError, ValueError):
            return 0

    options = []
    for item in examples or []:
        counts = []
        for key, word in (("nodes", "VM"), ("kclusters", "cluster"), ("addons", "add-on")):
            total = how_many(item.get(key))
            if total:
                counts.append("{} {}{}".format(total, word, "" if total == 1 else "s"))
        parts = [p for p in [(item.get("description") or "").strip().rstrip("."), ", ".join(counts)] if p]
        options.append({"value": item["name"], "label": item.get("title") or item["name"],
                        "help": ". ".join(parts)})
    return options


def _addon_options(catalogue, placement, addable=None):
    """
    The add-ons a workflow may offer: those that can go where this workflow puts them, and that need no
    configuration of their own. Left out are an add-on with a field its schema requires, and one with a field
    naming a credential account — whose --validate refuses to run without that credential even though the schema
    does not mark the field required. Nothing here could fill either in, and a lab that named it would not
    validate; the canvas is where those are added.

    `placement` is "kubernetes" for a cluster add-on and "host" for one that installs on a VM, matched against
    each add-on's own declared layers, so this never holds a list of add-on names.

    `addable`, when given, is the set of add-ons whose own --validate accepts a lab that configures them not at
    all (apps.accepts_blank_config()). That is the complete answer and the one to trust: an add-on can refuse for
    a reason no metadata expresses, such as "exactly one of these three fields". The two metadata tests stay as
    the fallback for a caller that cannot run the probe.
    """
    wanted = "kubernetes" if placement == "kubernetes" else "standalone-container"
    options = []
    for item in catalogue or []:
        if item.get("kind") != "addon":
            continue
        if item.get("required_fields") or item.get("credential_fields"):
            continue
        short = item["name"][len("install_"):] if item["name"].startswith("install_") else item["name"]
        if addable is not None and short not in addable:
            continue
        layers = item.get("layers") or []
        if wanted in layers or (placement == "host" and "os-native" in layers):
            options.append({"value": item["name"][len("install_"):] if item["name"].startswith("install_")
                            else item["name"], "label": item.get("title") or item["name"],
                            "help": item.get("description") or ""})
    return sorted(options, key=lambda o: o["value"])


def _backend_options(accounts, only_cloudtype=""):
    """
    Where the lab is built: this hypervisor, or one of the cloud accounts the credential store holds. Asked, never
    guessed: `accounts` is [(name, cloudtype)], which primary.list_cloud_accounts() reads without decrypting
    anything.

    `only_cloudtype` narrows it to the accounts of one provider and drops this hypervisor, for a workflow that
    runs nowhere else — SUSE Virtualization, which this project only builds on AWS.
    """
    options = []
    if not only_cloudtype:
        options.append({"value": "libvirt", "label": "This hypervisor (libvirt/KVM)",
                        "help": "The KVM hosts this automation node already knows about."})
    for name, cloudtype in accounts or []:
        if only_cloudtype and cloudtype != only_cloudtype:
            continue
        options.append({"value": "account:{}".format(name), "label": "{} ({})".format(name, cloudtype),
                        "help": "The {} account '{}' from the credential store.".format(cloudtype, name)})
    return options


def _image_options(images):
    """The source images the hypervisors report, as a choice; empty when no snapshot has been taken yet."""
    return [{"value": image, "label": image, "help": ""} for image in sorted(set(images or []))]


def prerequisites(workflow, service_accounts=None):
    """
    What this installation is missing before `workflow` can produce a lab that validates, as
    [{"what", "why", "fix"}]. Empty when there is nothing in the way.

    Only one workflow has such a prerequisite today: a SUSE Multi-Linux Manager server needs a registration
    credential, which nothing here can invent. Reporting it up front is the difference between a form that
    cannot work and a generated lab that fails its own preflight.
    """
    missing = []
    if workflow == "multi-linux" and not (service_accounts or {}).get("scc"):
        missing.append({
            "what": "a registration credential",
            "why": "SUSE Multi-Linux Manager registers against the SUSE Customer Center, and its own --validate "
                   "refuses a lab with no credential to do it with.",
            "fix": "Add one under Credentials, of kind 'scc'. Uyuni, its upstream, needs none.",
        })
    return missing


def steps(workflow, catalogue=(), accounts=(), images=(), addable=None, service_accounts=None,
          examples=(), answers=None):
    """
    `workflow`'s questions, in order, each one {"name", "title", "kind", "default", ...}. Kinds: "text" a string,
    "count" a whole number between min and max, "choice" one of options, "multi" any number of them.

    `answers` is what has been answered so far, because some questions depend on an earlier one: SUSE
    Virtualization only runs on AWS in a lab, with nested virtualization and its own AMI, and takes no add-ons at
    all, so answering "harvester" changes which backends are on offer and removes a question. A caller that
    re-reads the steps after each answer therefore cannot build a lab this project would refuse.

    Raises ValueError for a workflow that does not exist, so a caller never renders an empty form.
    """
    answers = answers or {}
    if not known(workflow):
        raise ValueError("unknown workflow: {}".format(workflow))

    # An example lab already answers every question this form would ask, including where it is built and how big
    # its nodes are, so the only questions left are which one and what to call the file.
    if workflow == "example":
        offered_examples = _example_options(examples)
        chosen = str(answers.get("example") or (offered_examples[0]["value"] if offered_examples else ""))
        return [
            {"name": "example", "title": "Which example lab?", "kind": "choice",
             "default": offered_examples[0]["value"] if offered_examples else "", "options": offered_examples,
             "help": "Taken as it is written. Nothing here rewrites it; the canvas is where it is changed."},
            {"name": "name", "title": "What should the file be called?", "kind": "text",
             "default": slug(re.sub(r"\.(json|yaml|yml)(\.template)?$", "", chosen)) or "example-lab",
             "help": "The file name only: the lab's own domain and node names stay as the example writes them."},
        ]

    name_defaults = {"kubernetes": "k8s-lab", "multi-linux": "mlm-lab",
                     "virtualization": "virt-lab", "vm": "vm-lab"}
    harvester = workflow == "virtualization" and str(answers.get("product") or "harvester") == "harvester"
    backends = _backend_options(accounts, only_cloudtype="aws" if harvester else "")
    common = [
        {"name": "name", "title": "What should this lab be called?", "kind": "text",
         "default": name_defaults[workflow],
         "help": "It names the file, and the lab's own DNS zone: '{}'.".format(domain_for(name_defaults[workflow]))},
        {"name": "backend", "title": "Where should it be built?", "kind": "choice",
         "default": backends[0]["value"] if backends else "libvirt", "options": backends,
         "help": "SUSE Virtualization installs on bare metal and on KVM, but this project builds it only on an "
                 "AWS account, from a Harvester AMI with nested virtualization: libs/harvester.py refuses a node "
                 "on any other backend." if harvester
                 else "The one answer nothing else implies." if accounts
                 else "The one answer nothing else implies. Cloud accounts come from the credential store, which "
                      "is read with a login."},
    ]
    # The first image the hypervisors report is the default, so a lab builds without the operator choosing one.
    # With no snapshot taken yet there is nothing to offer and nothing to default to: the preflight then says
    # SOURCE_IMAGE is missing, which is the truth and is fixed by refreshing the hypervisor status.
    offered = _image_options(images)
    image = [{"name": "image", "title": "Which image should the nodes boot?", "kind": "choice",
              "default": offered[0]["value"] if offered else "", "options": offered,
              "help": "Every image the hypervisors of this installation report."}]

    if workflow == "kubernetes":
        return common + [
            {"name": "distribution", "title": "Which Kubernetes distribution?", "kind": "choice",
             "default": "rke2", "options": _distribution_options()},
            {"name": "servers", "title": "How many server nodes?", "kind": "count",
             "default": 1, "min": 1, "max": 3,
             "help": "One is enough for a lab. Three is what a cluster needs to survive losing one."},
            {"name": "agents", "title": "How many agent nodes?", "kind": "count",
             "default": 2, "min": 0, "max": 6, "help": "Where the workloads run."},
            {"name": "apps", "title": "What should run on it?", "kind": "multi", "default": [],
             "options": _addon_options(catalogue, "kubernetes", addable=addable),
             "help": "Everything here installs with no configuration of its own. The rest needs the canvas."},
        ] + image

    if workflow == "multi-linux":
        return common + [
            {"name": "product", "title": "Which server?", "kind": "choice", "default": "smlm", "options": [
                {"value": "smlm", "label": "SUSE Multi-Linux Manager", "help": "The supported product."},
                {"value": "uyuni", "label": "Uyuni", "help": "Its upstream."}]},
            {"name": "clients", "title": "How many example clients?", "kind": "count",
             "default": 2, "min": 0, "max": 5,
             "help": "Plain VMs on the same network, for registering against the server."},
            {"name": "activation_key", "title": "An activation key to register them with?", "kind": "text",
             "default": "",
             "help": "Leave it empty and the clients are built but not registered, which you can do by hand "
                     "once the server is up and has a key."},
            {"name": "scc_account", "title": "Which registration credential?", "kind": "choice",
             "default": ((service_accounts or {}).get("scc") or [""])[0],
             "options": [{"value": name, "label": name, "help": "A credential of kind 'scc' from the store."}
                         for name in (service_accounts or {}).get("scc") or []],
             "help": "What the server registers with. Only SUSE Multi-Linux Manager needs it; Uyuni does not."},
            {"name": "apps", "title": "Anything else on the server's node?", "kind": "multi", "default": [],
             "options": _addon_options(catalogue, "host", addable=addable), "help": ""},
        ] + image

    if workflow == "virtualization":
        products = []
        if _backend_options(accounts, only_cloudtype="aws"):
            products.append({"value": "harvester", "label": "SUSE Virtualization (Harvester)",
                             "help": "Harvester installs on bare metal and on KVM; the path built here is an AWS "
                                     "account with nested virtualization, from a Harvester AMI."})
        products.append({"value": "kvm", "label": "A KVM hypervisor node",
                         "help": "A node prepared to run virtual machines of its own."})
        chosen = [{"name": "product", "title": "Which virtualization?", "kind": "choice",
                   "default": products[0]["value"], "options": products,
                   "help": "SUSE Virtualization needs an AWS account in the credential store, which is read "
                           "with a login, to be offered."}]
        counted = [{"name": "nodes", "title": "How many nodes?", "kind": "count", "default": 1, "min": 1,
                    "max": 3,
                    "help": "Harvester needs three for high availability, replicas and live migration."}]
        if harvester:
            # No add-on question: this project does not install add-ons on a Harvester cluster, and its image is
            # an AMI rather than one of the hypervisor's own files.
            return common + chosen + counted + [
                {"name": "ami", "title": "Which Harvester AMI?", "kind": "text", "default": "",
                 "help": "The AMI ID of the Harvester image in that account's region, 'ami-' and its hex."}]
        return common + chosen + counted + [
            {"name": "apps", "title": "What should run on it?", "kind": "multi", "default": [],
             "options": _addon_options(catalogue, "host", addable=addable), "help": ""},
        ] + image

    return common + [
        {"name": "count", "title": "How many virtual machines?", "kind": "count",
         "default": 1, "min": 1, "max": 5, "help": ""},
        {"name": "apps", "title": "What should be installed on them?", "kind": "multi", "default": [],
         "options": _addon_options(catalogue, "host", addable=addable),
         "help": "Everything here installs with no configuration of its own."},
    ] + image


def _answer(answers, step, steps_by_name):
    """One answer, falling back to its step's own default so a half-answered form still builds something."""
    value = (answers or {}).get(step)
    if value in (None, "", []):
        return steps_by_name.get(step, {}).get("default")
    return value


def _count(answers, step, steps_by_name):
    """A count answer as an integer, clamped to the range its step declares."""
    spec = steps_by_name.get(step, {})
    try:
        value = int(_answer(answers, step, steps_by_name))
    except (TypeError, ValueError):
        value = int(spec.get("default") or 0)
    return max(int(spec.get("min", 0)), min(int(spec.get("max", value)), value))


def _products_for(role, answers):
    """
    The sizing products a node in `role` runs, with the workflow's own answers filled into the role's template.

    A role template may name something no product documents separately — "rke2-agent", when RKE2 publishes one
    set of figures for servers and agents alike — so the base name answers for it.
    """
    out = []
    for template in _ROLE_PRODUCTS.get(role, ()):
        name = template.format(**{k: str(v) for k, v in (answers or {}).items()}) if "{" in template else template
        if not sizing.known(name) and name.endswith("-agent"):
            name = name[: -len("-agent")]
        if sizing.known(name):
            out.append(name)
    return out


def _node(role, answers, addons=(), sized_with=(), extra=None):
    """
    One node's fields: its own addons[], and the size of what it will actually run.

    `sized_with` is what runs on this node without being listed on it — a cluster add-on belongs to the kcluster
    but its pods land here, so its documented requirement is this node's requirement.
    """
    products = []
    for name in _products_for(role, answers) + [a for a in list(addons) + list(sized_with) if sizing.known(a)]:
        # A product named twice — the server's own add-on is also its role — would add its storage twice.
        if name not in products:
            products.append(name)
    node = {}
    if products:
        sized = sizing.combined(products)
        node["VM_MEM"] = str(sized["memory_mb"])
        node["VM_CPU"] = str(sized["cpu"])
        node["VM_DSK"] = str(sized["disk_gb"])
    if addons:
        node["addons"] = list(addons)
    node.update(extra or {})
    return node


def build(workflow, answers, network=None, catalogue=(), accounts=(), images=(), addable=None,
          service_accounts=None, examples=(), example_definition=None):  # noqa: C901 — one branch per workflow, each one short
    """
    The lab definition `answers` describe. Addresses are deliberately absent: the run allocates them from the
    lab's own subnet, which `network` supplies as {"mygw", "mymask", "mydns"} and which the web layer reads off
    the automation node itself. A cloud account needs none of that, and gets `cloud_account` instead.

    Every node is sized for what it will run, from libs/sizing.py. common carries the smallest of those sizes,
    because the schema requires it to carry them at all, and each node states its own.
    """
    if not known(workflow):
        raise ValueError("unknown workflow: {}".format(workflow))

    # The example is the definition: it is handed back as it is written, so what runs is what the example says,
    # not this form's reading of it. `example_definition` comes from the caller, which resolves the chosen name
    # through libs/lab_examples.py.
    if workflow == "example":
        if not example_definition:
            raise ValueError("no example lab named {!r}".format(answers.get("example") if answers else ""))
        return copy.deepcopy(example_definition)

    spec = {step["name"]: step for step in steps(workflow, catalogue=catalogue, accounts=accounts,
                                                 images=images, addable=addable,
                                                 service_accounts=service_accounts, examples=examples,
                                                 answers=answers)}
    name = slug(_answer(answers, "name", spec))
    domain = domain_for(name)
    backend = str(_answer(answers, "backend", spec) or "libvirt")
    apps = [str(a) for a in (_answer(answers, "apps", spec) or [])]
    image = str(_answer(answers, "image", spec) or "")

    common = {"mydomain": domain}
    if backend.startswith("account:"):
        account = backend.split(":", 1)[1]
        common["cloud_account"] = account
    else:
        common["backend"] = backend
        for field in ("mygw", "mymask", "mydns"):
            if (network or {}).get(field):
                common[field] = str(network[field])
    if image:
        common["SOURCE_IMAGE"] = image

    nodes, kclusters = {}, {}
    def fqdn(short):
        return "{}.{}".format(short, domain)

    if workflow == "kubernetes":
        distribution = str(_answer(answers, "distribution", spec))
        cluster = "{}-cluster".format(slug(name))
        kclusters[cluster] = {"clu_type": distribution, "clu_rel": _cluster_release(distribution),
                              "mydomain": domain,
                              "addons": apps}
        for index in range(1, _count(answers, "servers", spec) + 1):
            nodes[fqdn("server{}".format(index))] = _node(
                "k8s-server", dict(answers or {}, distribution=distribution), sized_with=apps,
                extra={"kcluster": cluster, "INSTALL_RKE2_TYPE": "server"})
        for index in range(1, _count(answers, "agents", spec) + 1):
            nodes[fqdn("agent{}".format(index))] = _node(
                "k8s-agent", dict(answers or {}, distribution=distribution), sized_with=apps,
                extra={"kcluster": cluster, "INSTALL_RKE2_TYPE": "agent"})

    elif workflow == "multi-linux":
        product = str(_answer(answers, "product", spec))
        key = str(_answer(answers, "activation_key", spec) or "")
        server_apps = [product] + apps
        nodes[fqdn(product)] = _node("smlm", dict(answers or {}, product=product), addons=server_apps)
        client_apps = ["client_registration"] if key else []
        for index in range(1, _count(answers, "clients", spec) + 1):
            nodes[fqdn("client{}".format(index))] = _node("client", answers, addons=client_apps)
        # The server's own section: the name it will answer to, which this knows, and the registration
        # credential the operator chose. Both are fields its --validate insists on.
        sections = {product: {"{}_fqdn".format(product): fqdn(product)}}
        account = str(_answer(answers, "scc_account", spec) or "")
        if account:
            sections[product]["{}_scc_account".format(product)] = account
        if key:
            sections["client_registration"] = {"client_registration_server": fqdn(product),
                                               "client_registration_activation_key": key}
        return _finish(common, nodes, kclusters, sections)

    elif workflow == "virtualization":
        product = str(_answer(answers, "product", spec))
        count = _count(answers, "nodes", spec)
        if product == "harvester":
            cluster = "{}-harvester".format(slug(name))
            # No add-ons: libs/harvester.py refuses a Harvester cluster that lists any. A join token is generated
            # here because it is a secret the cluster needs and nobody has to choose. A lab definition builds
            # Harvester on a cloud, from a prebuilt image, which on AWS needs nested virtualization; a cluster on
            # a hypervisor is built by setup_harvester_cluster.py instead, which this workflow does not write.
            kclusters[cluster] = {"clu_type": "harvester", "clu_rel": _cluster_release("harvester"),
                                  "mydomain": domain,
                                  "harvester_token": _token()}
            ami = str(_answer(answers, "ami", spec) or "")
            if ami:
                common["SOURCE_IMAGE"] = ami
            for index in range(1, count + 1):
                nodes[fqdn("harvester{}".format(index))] = _node(
                    "harvester", answers, extra={"kcluster": cluster, "aws_nested_virtualization": "true"})
        else:
            for index in range(1, count + 1):
                nodes[fqdn("kvm{}".format(index))] = _node("kvm", answers, addons=apps)

    else:
        for index in range(1, _count(answers, "count", spec) + 1):
            nodes[fqdn("vm{}".format(index))] = _node("vm", answers, addons=apps)

    return _finish(common, nodes, kclusters, {})


def _finish(common, nodes, kclusters, sections):
    """
    The definition, with common carrying the sizes the schema requires of it: the smallest any node asks for, so
    that a node which needs more states it and no node is silently given more than its own job needs.
    """
    sizes = [(int(n.get("VM_CPU", 0) or 0), int(n.get("VM_MEM", 0) or 0), int(n.get("VM_DSK", 0) or 0))
             for n in nodes.values()]
    floor = sizing.combined([])
    common = dict(common)
    common["VM_CPU"] = str(min([s[0] for s in sizes if s[0]] or [2]))
    common["VM_MEM"] = str(min([s[1] for s in sizes if s[1]] or [4096]))
    common["VM_DSK"] = str(min([s[2] for s in sizes if s[2]] or [floor["disk_gb"]]))
    definition = {"common": common, "nodes": nodes}
    if kclusters:
        definition["kclusters"] = kclusters
    definition.update(sections)
    return definition
