#!/usr/bin/env python3
# lab-builder's authorization layer. Authentication answers who the caller is; authorized() answers whether they
# may use the admin actions. The two are the same thing only for the local login file, where having an account IS
# the grant — an identity provider authenticates its whole directory, so with SSO in front the allowlist is what
# keeps the UI from being open to everyone in it.
# Nothing here reaches Apache, an identity provider, sudo or the helper: admin.dispatch is faked, so what is
# asserted is the decision api.dispatch() makes before any privileged work starts.
# Run from 140_labbuilder_authorization.sh, in its own container — see tests/run_tests.sh.
import os
import sys
import tempfile
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "libs"))
sys.path.insert(0, str(_REPO / "scripts"))
sys.path.insert(0, str(_REPO / "webui" / "lib"))

import admin  # noqa: E402
import api  # noqa: E402

failures = []


def check(desc, cond):
    if not cond:
        failures.append(desc)
        print("FAIL:", desc)


_tmp = tempfile.mkdtemp()
_users = os.path.join(_tmp, "htpasswd")
_allowed = os.path.join(_tmp, "authorized")
os.environ["LABBUILDER_USERS"] = _users
os.environ["LABBUILDER_AUTHORIZED"] = _allowed

# A local account, as lab-builder-passwd writes it: "user:<crypt hash>".
with open(_users, "w") as f:
    f.write("# a comment line\nlocaluser:$6$salt$hash\n")


def _write_allowlist(*lines):
    with open(_allowed, "w") as f:
        f.write("\n".join(lines) + "\n")


def _no_allowlist():
    if os.path.exists(_allowed):
        os.unlink(_allowed)


# ── has_local_account(): only a real line in the login file counts ──────────
check("has_local_account: a user with a line in the login file", admin.has_local_account("localuser"))
check("has_local_account: a user with no line", not admin.has_local_account("someone@example.com"))
check("has_local_account: a comment is not an account", not admin.has_local_account("# a comment line"))
check("has_local_account: no user is not an account", not admin.has_local_account(""))


# ── authorized() with NO allowlist: exactly the behaviour before one existed ──
_no_allowlist()
check("no allowlist: a local account is authorized, as before", admin.authorized("localuser"))
check("no allowlist: an identity that is NOT a local account is refused, so turning on SSO and forgetting the "
      "allowlist denies rather than admits the whole directory",
      not admin.authorized("someone@example.com"))
check("no allowlist: no user is never authorized", not admin.authorized(""))
check("no allowlist: None is never authorized", not admin.authorized(None))


# ── authorized() WITH an allowlist: the listed identities, and only those ───
_write_allowlist("# who may use lab-builder", "someone@example.com", "  other@example.com  ")
check("allowlist: a listed identity is authorized", admin.authorized("someone@example.com"))
check("allowlist: surrounding whitespace in the file is ignored", admin.authorized("other@example.com"))
check("allowlist: an unlisted identity is refused", not admin.authorized("nobody@example.com"))
check("allowlist: a comment line is not an identity", not admin.authorized("# who may use lab-builder"))
check("allowlist: once present, it REPLACES the local-account rule, so a local account not listed is refused",
      not admin.authorized("localuser"))

_write_allowlist("*")
check("allowlist '*': any authenticated identity is authorized", admin.authorized("anyone@example.com"))
check("allowlist '*': still never an empty user", not admin.authorized(""))

_write_allowlist("#  only comments and blank lines", "")
check("an allowlist of only comments is treated as absent, not as an empty allowlist that locks everyone out",
      admin.authorized("localuser") and not admin.authorized("someone@example.com"))


# ── api.dispatch(): the order of the checks before any privileged work ──────
_called = []
admin.dispatch = lambda action, method, params, data: _called.append(action) or {"ok": True}
_audits = []
admin.audit = lambda user, action, outcome="ok": _audits.append((user, action, outcome))

_action = sorted(admin.ACTIONS)[0]
check("an admin action exists to test with", bool(_action))

_called[:] = []
status, obj = api.dispatch(_action, "GET", {}, b"", user="someone@example.com", https=False)
check("no HTTPS is refused with 403", status == 403)
check("no HTTPS does no privileged work", _called == [])

_called[:] = []
status, obj = api.dispatch(_action, "GET", {}, b"", user=None, https=True)
check("no user is refused with 401", status == 401)
check("no user does no privileged work", _called == [])

_no_allowlist()
_called[:] = []
_audits[:] = []
status, obj = api.dispatch(_action, "GET", {}, b"", user="someone@example.com", https=True)
check("authenticated but not authorized is refused with 403, not 401", status == 403)
check("an unauthorized caller does NO privileged work", _called == [])
check("the refusal names the file to add the identity to", admin.authorized_file() in str(obj))
check("the refusal is audited with the user and why", _audits
      and _audits[0][0] == "someone@example.com" and "not authorized" in _audits[0][2])

_called[:] = []
_audits[:] = []
status, obj = api.dispatch(_action, "GET", {}, b"", user="localuser", https=True)
check("an authorized caller reaches the action", status == 200 and _called == [_action])
check("a successful action is audited with its user", _audits == [("localuser", _action, "ok")])

# A failing action must still be attributable, and must not be reported as a success.
_audits[:] = []


def _boom(action, method, params, data):
    raise ValueError("nope")


admin.dispatch = _boom
status, obj = api.dispatch(_action, "GET", {}, b"", user="localuser", https=True)
check("a failing action is audited as failed, not as ok",
      _audits and _audits[0][0] == "localuser" and _audits[0][2].startswith("failed"))
check("a failing action does not return 200", status != 200)


# ── every privileged action goes through that gate ──────────────────────────
# api.dispatch routes ONLY admin.ACTIONS to admin.dispatch, so an action added to admin without being listed
# there would be unreachable, and one listed without the gate would be reachable unauthenticated.
_api_src = (_REPO / "webui" / "lib" / "api.py").read_text()
check("api.dispatch gates on admin.ACTIONS", "action in admin.ACTIONS" in _api_src)
check("api.dispatch calls admin.authorized() for those actions", "admin.authorized(user)" in _api_src)
check("the authorization check comes before the body is parsed and the action runs",
      _api_src.index("admin.authorized(user)") < _api_src.index("admin.dispatch(action"))


# ── login_configured(): whether the server actions can be used AT ALL ───────
# The UI greys the server actions out when this is false. A deployment that replaced local logins with SSO has an
# empty login file by design, so answering only from that file would disable the actions and tell the operator to
# create an account they do not need.
_no_allowlist()
with open(_users, "w") as f:
    f.write("localuser:$6$salt$hash\n")
check("login_configured: a local account is a way in", admin.login_configured())
with open(_users, "w") as f:
    f.write("# only a comment\n")
check("login_configured: a login file of only comments is not a way in", not admin.login_configured())
_write_allowlist("someone@example.com")
check("login_configured: an allowlist alone is a way in, which is the SSO-only deployment",
      admin.login_configured())
_write_allowlist("# nothing listed")
check("login_configured: an allowlist of only comments is not a way in", not admin.login_configured())
os.environ["LABBUILDER_USERS"] = os.path.join(_tmp, "missing-htpasswd")
_no_allowlist()
check("login_configured: no login file and no allowlist is not a way in", not admin.login_configured())
os.environ["LABBUILDER_USERS"] = _users
with open(_users, "w") as f:
    f.write("# a comment line\nlocaluser:$6$salt$hash\n")

_js = (_REPO / "webui" / "htdocs" / "app.js").read_text()
check("the UI's own hint names both ways in, not just lab-builder-passwd",
      "lab-builder-passwd" in _js and "/etc/lab-builder/authorized" in _js)


# ── the shipped SSO example: documentation, never a live config or a secret ──
_example = _REPO / "webui" / "apache" / "lab-builder-sso.conf.example"
check("the SSO example ships", _example.is_file())
_ex = _example.read_text()
check("it covers OIDC", "mod_auth_openidc" in _ex)
check("it covers SAML", "mod_auth_mellon" in _ex)
check("every Apache directive in it is commented out, so copying the file in activates nothing",
      not [l for l in _ex.splitlines()
           if l.strip() and not l.lstrip().startswith("#")])
check("it says authorization is separate and required", "authorized" in _ex)
check("it keeps the client secret out of the Apache config",
      "oidc-secret.conf" in _ex and "OIDCClientSecret <secret>" in _ex)
for _placeholder in ("YOUR-IDP", "YOUR-AUTOMATION-NODE"):
    check("it carries no real deployment value in place of {}".format(_placeholder), _placeholder in _ex)

# The installer ships it as an example and activates no SSO config of its own.
_inst = (_REPO / "install_automation_node_scripts.sh").read_text()
check("the installer ships the example", "lab-builder-sso.conf.example" in _inst)
check("the installer activates no SSO Apache config",
      "_apache_install_conf webui/apache/lab-builder-sso" not in _inst)

print("Failed: {}".format(len(failures)))
if failures:
    sys.exit(1)
print("all lab-builder authorization checks passed")
