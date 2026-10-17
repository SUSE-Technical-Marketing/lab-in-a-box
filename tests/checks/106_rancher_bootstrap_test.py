#!/usr/bin/env python3.11
# install_rancher's finish_bootstrap() — see 106_rancher_bootstrap.sh.
import json
import sys
from pathlib import Path
from unittest import mock

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "libs"))

import apps  # noqa: E402

rancher = apps.load_addon_module("install_rancher", str(_REPO / "scripts"))

failures = []


def check(desc, cond):
    if not cond:
        failures.append(desc)
        print("FAIL:", desc)


class _Rancher:
    """
    Stands in for a Rancher over SSH. `bootstrap_works` is a Rancher nobody has logged into yet; `admin_works` is
    one already bootstrapped; neither is one that cannot be reached. `server_url` is what the setting ends up as.
    """

    def __init__(self, bootstrap_works=True, admin_works=False, server_url_sticks=True):
        self.cmds = []
        self.bootstrap_works = bootstrap_works
        self.admin_works = admin_works
        self.server_url_sticks = server_url_sticks
        self.server_url = ""

    def output(self, hostname, cmd, **kwargs):
        self.cmds.append(cmd)
        if "action=login" in cmd:
            if "the-bootstrap-one" in cmd and self.bootstrap_works:
                return "token-abc:secret"
            if "the-admin-one" in cmd and self.admin_works:
                return "token-xyz:secret"
            return ""
        if "settings.management.cattle.io server-url" in cmd:
            return self.server_url if self.server_url_sticks else ""
        return ""

    def run(self, hostname, cmd, **kwargs):
        self.cmds.append(cmd)
        if "settings/server-url" in cmd and "PUT" in cmd:
            self.server_url = "https://rancher.lab.example"
        return mock.Mock(returncode=0, stdout="")

    def joined(self):
        return "\n".join(self.cmds)


def _finish(fake, bootstrap="the-bootstrap-one", admin="the-admin-one"):
    with mock.patch.object(rancher, "ssh_output", side_effect=fake.output), \
         mock.patch.object(rancher, "ssh_run", side_effect=fake.run):
        return rancher.finish_bootstrap("n1", "rancher.lab.example", bootstrap, admin)


# ── a Rancher nobody has logged into yet ──────────────────────────────────
fake = _Rancher()
returned = _finish(fake)
text = fake.joined()
check("it logs in with the password Rancher generated", "action=login" in text and "the-bootstrap-one" in text)
check("it changes the password to the one the lab asked for",
      "action=changepassword" in text and "the-admin-one" in text)
check("the password it reports is the lab's own", returned == "the-admin-one")
check("it sets the server URL to the hostname Rancher serves",
      "PUT https://rancher.lab.example/v3/settings/server-url" in text
      and "https://rancher.lab.example" in text)
check("the login comes before the password change, which comes before the URL",
      text.index("action=login") < text.index("action=changepassword") < text.index("settings/server-url"))
check("the URL is read back, rather than assumed to have stuck",
      "settings.management.cattle.io server-url" in text)
check("the password travels quoted, never bare in a shell command",
      "'{\"currentPassword\"" in text or '"currentPassword"' in text)

# No new password asked for: the generated one stays, and nothing is changed.
fake = _Rancher()
returned = _finish(fake, admin="the-bootstrap-one")
check("a lab that names no password keeps the generated one",
      returned == "the-bootstrap-one" and "action=changepassword" not in fake.joined())
check("...and its server URL is still set", "settings/server-url" in fake.joined())

# ── a Rancher that is already bootstrapped ───────────────────────────────
# Confirmed live on 2026-10-17 against a real Rancher: the generated password stops working once it has been
# changed, and that is not a failure. It is also what makes a second run of the add-on safe.
fake = _Rancher(bootstrap_works=False, admin_works=True)
returned = _finish(fake)
check("it falls back to the lab's own password", returned == "the-admin-one")
check("nothing is changed on a Rancher that is already bootstrapped",
      "action=changepassword" not in fake.joined())
check("its server URL is still asserted", "settings/server-url" in fake.joined())

# ── a Rancher that cannot be reached ─────────────────────────────────────
fake = _Rancher(bootstrap_works=False, admin_works=False)
warned = []
with mock.patch.object(rancher, "warn", side_effect=lambda m: warned.append(m)):
    returned = _finish(fake)
check("it warns instead of dying, since the lab's own VMs are already built", returned == "the-admin-one")
check("the warning says what the consequence is",
      warned and "cannot be imported" in warned[0])
check("nothing is written to a Rancher it could not log in to", "settings/server-url" not in fake.joined())

# A URL that does not stick is reported: silence there would leave an import failing for no visible reason.
fake = _Rancher(server_url_sticks=False)
warned = []
with mock.patch.object(rancher, "warn", side_effect=lambda m: warned.append(m)):
    _finish(fake)
check("a server URL that did not stick is reported",
      warned and "server-url" in warned[0] and "importing a cluster will not work" in warned[0])

# ── the install does it, and the field is documented ─────────────────────
source = (_REPO / "scripts" / "install_rancher").read_text()
check("the install calls it", "finish_bootstrap(hostname, hostname_fqdn, initial_rancher_pwd," in source)
check("it no longer just prints the generated password and stops",
      "# Admin password:" in source and "# Initial password:" not in source)
check("the new field is documented in the add-on's own header", "rancher_admin_pwd" in source)
check("the field is in its schema",
      "rancher_admin_pwd" in [f["name"] for f in __import__("json").loads(
          __import__("subprocess").run([sys.executable, str(_REPO / "scripts" / "install_rancher"),
                                        "--schema", "json"], stdout=-1,
                                       universal_newlines=True).stdout)["fields"]])

# ── importing a cluster ───────────────────────────────────────────────────
class _Import:
    """
    Stands in for a Rancher and the cluster being imported. `has_id` is a cluster Rancher has already given an id,
    `manifest` what the token collection answers, and `apply_fails` the other cluster refusing the manifest.
    """

    def __init__(self, has_id="", manifest="https://rancher.lab.example/v3/import/tok_c-m-abc123.yaml",
                 apply_fails=False, ready=True):
        self.cmds = []
        self.has_id = has_id
        self.manifest = manifest
        self.apply_fails = apply_fails
        self.ready = ready
        self.applied_body = None

    def output(self, hostname, cmd, **kwargs):
        self.cmds.append((hostname, cmd))
        if "action=login" in cmd:
            return "token-abc:secret"
        if "status.clusterName" in cmd:
            return self.has_id or "c-m-abc123"
        if "clusterregistrationtokens?clusterId" in cmd:
            return self.manifest
        if "status.ready" in cmd:
            return "true" if self.ready else ""
        return ""

    def run(self, hostname, cmd, input_text=None, **kwargs):
        self.cmds.append((hostname, cmd))
        if input_text:
            self.applied_body = input_text
        if "kubectl apply" in cmd and "curl" in cmd and self.apply_fails:
            return mock.Mock(returncode=1, stdout="error: no objects passed to apply", stderr="")
        if "status.clusterName" in cmd:
            return mock.Mock(returncode=0 if self.has_id else 1, stdout=self.has_id)
        return mock.Mock(returncode=0, stdout="")

    def sent_to(self, host):
        return "\n".join(cmd for h, cmd in self.cmds if h == host)


def _import(fake, name="kubelab", target="kube1.lab", insecure_tls=True):
    with mock.patch.object(rancher, "ssh_output", side_effect=fake.output), \
         mock.patch.object(rancher, "ssh_run", side_effect=fake.run), \
         mock.patch.object(rancher.time, "sleep"):
        return rancher.import_cluster("rancher1", "rancher.lab.example", "pw", name, target,
                                      insecure_tls=insecure_tls)

fake = _Import()
got = _import(fake)
check("the cluster is imported and its Rancher id reported back", got == "c-m-abc123")
applied = json.loads(fake.applied_body)
check("it is registered as a provisioning.cattle.io/v1 Cluster in fleet-default",
      applied["apiVersion"] == "provisioning.cattle.io/v1" and applied["kind"] == "Cluster"
      and applied["metadata"] == {"name": "kubelab", "namespace": "fleet-default"})
check("the agent is told to accept this Rancher's own certificate, which a lab self-signs",
      applied["spec"]["agentEnvVars"] == [{"name": "CATTLE_INSECURE_TLS", "value": "true"}])
plain = _Import()
_import(plain, insecure_tls=False)
check("insecure_tls false leaves the agent to verify the certificate",
      "agentEnvVars" not in json.loads(plain.applied_body)["spec"])

rancher_side = fake.sent_to("rancher1")
check("the registration token is created through the v3 API, not as a CRD",
      "POST https://rancher.lab.example/v3/clusterregistrationtoken" in rancher_side
      and "clusterRegistrationToken" in rancher_side)
check("the manifest URL is read from the API too", "clusterregistrationtokens?clusterId=c-m-abc123" in rancher_side)
# The collection's own answer also carries a "manifestUrl" in its sort links, and applying that gives kubectl
# nothing to apply. Confirmed live on 2026-10-17: the first match was ...?clusterId=...&sort=manifestUrl.
check("only a real import manifest is accepted as the URL",
      "/v3/import/" in rancher_side and "\\.yaml" in rancher_side)
check("the manifest is applied on the other cluster, not on Rancher",
      "/v3/import/tok_c-m-abc123.yaml" in fake.sent_to("kube1.lab")
      and "kubectl apply -f -" in fake.sent_to("kube1.lab"))
check("Rancher reporting it ready is what ends the import", "status.ready" in rancher_side)

# A cluster already imported: no second Cluster resource, and the id comes back as it is.
fake = _Import(has_id="c-m-existing")
got = _import(fake)
check("a cluster already imported is left alone", got == "c-m-existing" and fake.applied_body is None)

# The manifest never arriving, and the other cluster refusing it, are both reported rather than passed over.
fake = _Import(manifest="")
warned = []
with mock.patch.object(rancher, "warn", side_effect=lambda m: warned.append(m)):
    got = _import(fake)
check("no registration manifest is a reported failure, naming the server URL as the cause",
      got == "" and warned and "server-url" in warned[0])

fake = _Import(apply_fails=True)
warned = []
with mock.patch.object(rancher, "warn", side_effect=lambda m: warned.append(m)):
    got = _import(fake)
check("a manifest the other cluster refuses is reported with its own message",
      got == "" and warned and "no objects passed to apply" in warned[0])

fake = _Import(ready=False)
warned = []
with mock.patch.object(rancher, "warn", side_effect=lambda m: warned.append(m)):
    got = _import(fake)
check("a cluster that never goes ready still reports its id, and says it may still be starting",
      got == "c-m-abc123" and warned and "has not reported it ready" in warned[0])

# ── which clusters, and how each is reached ──────────────────────────────
LAB = {"nodes": {"k1.lab": {"kcluster": "kubelab"}, "k2.lab": {"kcluster": "kubelab",
                                                               "KUBERNETES_NODE_TYPE": "agent"}},
       "kclusters": {"kubelab": {"clu_type": "kubeadm"}}}
calls = []
with mock.patch.object(rancher, "import_cluster",
                       side_effect=lambda h, f, p, name, target, insecure_tls=True:
                       calls.append((name, target, insecure_tls)) or "c-m-1"):
    got = rancher.import_clusters("rancher1", LAB, "rancher.lab.example", "pw",
                                  {"rancher_import_clusters": [
                                      {"kcluster": "kubelab"},
                                      {"name": "elsewhere", "node": "far.example", "insecure_tls": "false"},
                                      {"name": "nowhere"}]})
check("a kcluster of this lab is reached through its own node",
      ("kubelab", "k1.lab", True) in calls)
check("a cluster named by node is reached through that node, and can keep TLS verification",
      ("elsewhere", "far.example", False) in calls)
check("an entry with neither a kcluster nor a node is skipped, not guessed at",
      not any(c[0] == "nowhere" for c in calls))
check("what was imported is reported back", got == {"kubelab": "c-m-1", "elsewhere": "c-m-1"})
with mock.patch.object(rancher, "import_cluster") as never:
    check("a lab that asks for no import does nothing",
          rancher.import_clusters("r", LAB, "f", "p", {}) == {} and not never.called)

check("the install imports after it has bootstrapped, since importing needs the server URL",
      source.index("finish_bootstrap(hostname, hostname_fqdn, initial_rancher_pwd,")
      < source.index("import_clusters(hostname, definition, hostname_fqdn, admin_pwd, cfg)"))
check("--check looks at every cluster the lab asked to import",
      "is not imported into this Rancher" in source and "does not report it ready" in source)

if failures:
    print("{} check(s) failed".format(len(failures)))
    sys.exit(1)
print("all rancher-bootstrap checks passed")
