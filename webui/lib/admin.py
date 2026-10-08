"""
lab-builder actions that need a login: saved labs, credentials and lab creation.

They are served only on the login endpoint (Apache: /lab-builder/admin with
Basic auth; run-local.py: /admin, checked by check_login()) and only over HTTPS.
Credentials and lab creation run through lab-builder-helper, as root: directly
when this process is root, otherwise through `sudo -n` (the sudoers rule that
install_automation_node_scripts.sh writes allows only that helper).

Configuration (env-overridable):
  LABBUILDER_HELPER   the helper (/usr/local/sbin/lab-builder-helper)
  LABBUILDER_USERS    login file, lines "user:<crypt(3) hash>" (/etc/lab-builder/htpasswd)
"""
import base64
import hmac
import json
import os
import subprocess

import discovery

ACTIONS = {"labs", "lab", "save", "credentials", "credentials-add", "credentials-encrypt",
           "credentials-delete", "create", "job", "jobs"}


def helper_path():
    return os.environ.get("LABBUILDER_HELPER", "/usr/local/sbin/lab-builder-helper")


def users_file():
    return os.environ.get("LABBUILDER_USERS", "/etc/lab-builder/htpasswd")


def login_configured():
    """True when the login file holds at least one user."""
    try:
        with open(users_file()) as f:
            return any(":" in line and not line.startswith("#") for line in f)
    except OSError:
        return False


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
        if os.path.basename(data.get("filename", "")) != data.get("filename", ""):
            raise ValueError("invalid filename")
        name = discovery._safe_name(data.get("filename", ""))
        return helper(["lab-create", name] + (["--keep"] if data.get("keep") else []))
    if action == "job" and method == "GET":
        return helper(["lab-status", one("id")])
    if action == "jobs" and method == "GET":
        return helper(["lab-jobs"])
    raise ValueError("unknown action %r (%s)" % (action, method))
