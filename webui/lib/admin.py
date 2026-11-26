"""
lab-builder actions that need a login: saved labs, credentials and lab creation.

They are served only on the login endpoint (Apache: /lab-builder/admin with
Basic auth; run-local.py: /admin, checked by check_login()) and only over HTTPS.
Credentials and lab creation run through lab-builder-helper, as root: directly
when this process is root, otherwise through `sudo -n` (the sudoers rule that
install_automation_node_scripts.sh writes allows only that helper).

Authentication says who the caller is; authorized() says whether they may use these actions. The two are the
same thing only for the local login file, where having an account IS the grant. An identity provider
authenticates its whole directory, so with SSO in front (see ../apache/lab-builder-sso.conf.example) the
allowlist is what keeps lab-builder from being open to everyone in that directory.

Configuration (env-overridable):
  LABBUILDER_HELPER       the helper (/usr/local/sbin/lab-builder-helper)
  LABBUILDER_USERS        login file, lines "user:<crypt(3) hash>" (/etc/lab-builder/htpasswd)
  LABBUILDER_AUTHORIZED   allowlist of identities permitted to use these actions
                          (/etc/lab-builder/authorized); see authorized()
"""
import base64
import hmac
import json
import os
import subprocess
import syslog

import discovery

ACTIONS = {"labs", "lab", "save", "credentials", "credentials-add", "credentials-encrypt",
           "credentials-delete", "create", "validate-credentials", "job", "jobs", "power"}

# Served without a login, and answered by api.py, not here: the step-by-step builder asks questions and returns a
# definition, which is the public half's work, the same class as "schema" and "validate". Nothing it does reaches
# the hypervisor or the credential store. What a login adds is this module's cloud_accounts()/service_accounts(),
# which name the accounts the store holds: api.dispatch passes those in only for an authorized user, so the
# builder offers an anonymous caller this hypervisor and names no account. Creating or saving the lab the builder
# wrote is an ACTIONS entry like any other.
WIZARD_ACTIONS = {"wizard", "wizard-steps", "wizard-build"}


def helper_path():
    return os.environ.get("LABBUILDER_HELPER", "/usr/local/sbin/lab-builder-helper")


def users_file():
    return os.environ.get("LABBUILDER_USERS", "/etc/lab-builder/htpasswd")


def authorized_file():
    return os.environ.get("LABBUILDER_AUTHORIZED", "/etc/lab-builder/authorized")


def _entries(path):
    """The non-empty, non-comment lines of `path`, stripped. [] when it cannot be read."""
    try:
        with open(path) as f:
            return [line.strip() for line in f
                    if line.strip() and not line.strip().startswith("#")]
    except OSError:
        return []


def has_local_account(user):
    """True when `user` has a line in the login file, i.e. an account lab-builder-passwd created."""
    if not user:
        return False
    for line in _entries(users_file()):
        name, sep, _hashed = line.partition(":")
        if sep and name == user:
            return True
    return False


def authorized(user):
    """
    Whether `user`, already authenticated, may use the actions in ACTIONS.

    The allowlist is authorized_file(), one identity per line, "#" comments, "*" allowing any authenticated
    identity:
      - present: `user` must be listed, so an identity provider's whole directory is not admitted with it.
      - absent:  a local account is authorized, which is the behaviour before any allowlist existed; any other
                 identity is refused, so configuring SSO and forgetting the allowlist denies rather than admits.
    """
    if not user:
        return False
    entries = _entries(authorized_file())
    if entries:
        return "*" in entries or user in entries
    return has_local_account(user)


def audit(user, action, outcome="ok"):
    """
    Record one admin action and who asked for it, to syslog (LOG_AUTH).

    The helper runs as root and sees only that the web process called it, so without this a privileged action has
    no actor. Failing to write the record never fails the action.
    """
    try:
        syslog.openlog("lab-builder", syslog.LOG_PID, syslog.LOG_AUTH)
        syslog.syslog(syslog.LOG_NOTICE, "user={} action={} outcome={}".format(user or "-", action, outcome))
    except OSError:
        pass


def login_configured():
    """
    True when there is any way to reach the actions in ACTIONS: a local account in the login file, or an
    allowlist, which is what an identity provider in front of this is granted by.

    The UI asks this to decide whether to offer the server actions at all. A deployment that replaced local
    logins with SSO has an empty login file by design, so answering only from that file would grey the actions
    out and tell the operator to create an account they do not need.
    """
    try:
        with open(users_file()) as f:
            if any(":" in line and not line.strip().startswith("#") for line in f):
                return True
    except OSError:
        pass
    return bool(_entries(authorized_file()))


def _verify(password, hashed):
    """True when `password` matches crypt(3) hash `hashed` ($6$, $5$ or $1$), checked with openssl."""
    parts = hashed.split("$")
    if len(parts) != 4 or parts[1] not in ("1", "5", "6"):
        return False
    r = subprocess.run(["openssl", "passwd", "-" + parts[1], "-salt", parts[2], "-stdin"],
                       input=password.encode("utf-8"), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    return r.returncode == 0 and hmac.compare_digest(r.stdout.decode().strip(), hashed)


def check_login(authorization):
    """The user name an HTTP Basic `authorization` header logs in as, or None."""
    if not authorization or not authorization.startswith("Basic "):
        return None
    try:
        user, _, password = base64.b64decode(authorization[6:]).decode("utf-8").partition(":")
        with open(users_file()) as f:
            for line in f:
                name, _, hashed = line.strip().partition(":")
                if name == user and hashed and _verify(password, hashed):
                    return user
    except (ValueError, OSError):
        return None
    return None


def _credentials():
    """
    The credential store as the helper reports it: [{"name", "kind", "type", ...}], names and types only.

    It has to come through the helper. The files are root-only on purpose, so the web process cannot read them
    itself (see CLAUDE.md's credentials model), and anything here that listed them by reading the directory would
    find nothing and silently offer no accounts at all.
    """
    try:
        return helper(["credentials-list"]).get("credentials") or []
    except (OSError, ValueError, RuntimeError):
        return []


def cloud_accounts():
    """[(name, cloudtype)] of the cloud accounts, for the wizard's "where should it be built" question."""
    return [(c.get("name"), c.get("type")) for c in _credentials()
            if c.get("kind") == "cloud" and c.get("name") and c.get("type")]


def service_accounts():
    """{kind: [name, ...]} of the service credentials, for a workflow that needs one of a particular kind."""
    out = {}
    for credential in _credentials():
        if credential.get("kind") == "service" and credential.get("name") and credential.get("type"):
            out.setdefault(credential["type"], []).append(credential["name"])
    return {kind: sorted(names) for kind, names in out.items()}


def helper(args, stdin=None):
    """Run lab-builder-helper with `args` (JSON `stdin` if given); returns its answer.
    Raises ValueError with the helper's message when it refuses."""
    cmd = [helper_path()] + list(args)
    if os.geteuid() != 0:
        cmd = ["sudo", "-n"] + cmd
    r = subprocess.run(cmd, input=json.dumps(stdin).encode() if stdin is not None else b"",
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        out = json.loads(r.stdout.decode("utf-8") or "{}")
    except ValueError:
        out = {}
    if r.returncode != 0 or "error" in out:
        raise ValueError(out.get("error") or r.stderr.decode("utf-8", "replace").strip()
                         or "lab-builder-helper failed (exit {})".format(r.returncode))
    return out


def list_labs():
    """The saved labs: [{"name", "modified"}], newest first."""
    d = discovery.output_dir()
    labs = [{"name": f, "modified": int(os.path.getmtime(os.path.join(d, f)))}
            for f in os.listdir(d) if f.endswith(".json") and os.path.isfile(os.path.join(d, f))]
    return sorted(labs, key=lambda x: x["modified"], reverse=True)


def read_lab(name):
    """Saved lab `name` as an object."""
    with open(os.path.join(discovery.output_dir(), discovery._safe_name(name))) as f:
        return json.load(f)


def dispatch(action, method, params, data):
    """Carry out login action `action`; returns the JSON answer. `params` is the
    parsed query string, `data` the parsed POST body."""
    one = lambda k: (params.get(k) or [""])[0]
    if action == "labs" and method == "GET":
        return {"labs": list_labs()}
    if action == "lab" and method == "GET":
        return {"name": one("name"), "lab": read_lab(one("name"))}
    if action == "save" and method == "POST":
        path = discovery.save_lab(data.get("filename", "lab"), data.get("config", {}))
        return {"saved": os.path.basename(path), "path": path}
    if action == "credentials" and method == "GET":
        return helper(["credentials-list"])
    if action == "credentials-add" and method == "POST":
        return helper(["credentials-add"], data)
    if action == "credentials-encrypt" and method == "POST":
        return helper(["credentials-encrypt", data.get("file", "")], {"passphrase": data.get("passphrase", "")})
    if action == "credentials-delete" and method == "POST":
        return helper(["credentials-delete", data.get("file", "")])
    if action == "create" and method == "POST":
        # Either a lab saved on the server, or an example lab by name: the helper materialises an example for the
        # run and keeps nothing, so launching one needs nothing saved here first.
        example = data.get("example", "")
        if example:
            args = ["lab-create", "--example", str(example)]
        else:
            if os.path.basename(data.get("filename", "")) != data.get("filename", ""):
                raise ValueError("invalid filename")
            args = ["lab-create", discovery._safe_name(data.get("filename", ""))]
        if data.get("keep"):
            args.append("--keep")
        if data.get("cloud_account"):
            args += ["--cloud-account", str(data["cloud_account"])]
        # The passphrase (for a lab whose credentials are encrypted) reaches the helper on stdin, never as an
        # argument other processes could read, and the helper puts it only in the job's own environment.
        return helper(args, {"passphrase": data.get("passphrase", "")})
    if action == "validate-credentials" and method == "POST":
        # The preflight with the lab's credentials, as root: the passphrase reaches the helper on stdin, for this one
        # request, and is never kept.
        return helper(["lab-validate"], {"config": data.get("config", {}), "passphrase": data.get("passphrase", "")})
    if action == "power" and method == "POST":
        # The power state of a lab's VMs, or starting/stopping them: a saved lab by file name, or an example lab
        # by name. Synchronous — the helper answers with the states themselves and with what each backend says
        # stopping does to the bill.
        args = ["lab-power", str(data.get("action", ""))]
        if data.get("example"):
            args += ["--example", str(data["example"])]
        else:
            args.append(discovery._safe_name(data.get("filename", "")))
        if data.get("cloud_account"):
            args += ["--cloud-account", str(data["cloud_account"])]
        return helper(args)
    if action == "job" and method == "GET":
        return helper(["lab-status", one("id")])
    if action == "jobs" and method == "GET":
        return helper(["lab-jobs"])
    raise ValueError("unknown action %r (%s)" % (action, method))


def pub_status(job, token):
    """A job's state and log for whoever holds its own token (see lab_builder_helper.pub_status()) — the
    one job action that needs neither login nor admin.ACTIONS, since the token itself is the credential.
    Called from api.dispatch() directly, not through this module's own login-gated dispatch()."""
    return helper(["pub-status", job], {"token": token})
