"""
Transport-agnostic request dispatch for lab-builder.

Both the Apache CGI shim (cgi-bin/labbuilder.py) and the local dev server
(run-local.py) call dispatch() so the routing logic lives in exactly one place.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import admin  # noqa: E402
import discovery  # noqa: E402


def _one(v, default=""):
    if isinstance(v, list):
        return v[0] if v else default
    return v if v is not None else default


def _wizard(action, method, params, data, clouds, services):
    """
    The step-by-step builder (admin.WIZARD_ACTIONS), which needs no login: it asks questions and returns a lab
    definition, never touching the hypervisor or the credential store.

    `clouds` ([(name, cloudtype)]) and `services` ({kind: [name]}) are the credential store's accounts, or ()/{}
    for a caller without a login: their names are not public, so without one the builder offers this hypervisor
    and names no account. Everything the finished lab then needs a login for (Create lab, Save to server) is an
    admin.ACTIONS entry and is gated there, as before.
    """
    if action == "wizard" and method == "GET":
        return {"workflows": discovery.wizard_workflows(service_accounts=services),
                "network": discovery.lab_network(), "accounts": bool(clouds)}
    if action == "wizard-steps" and method == "GET":
        # The answers so far ride along as JSON, because a GET is what the browser reads steps with and a later
        # question can depend on an earlier one. Anything unparseable is treated as no answers at all.
        try:
            answers = json.loads(_one(params.get("answers")) or "{}")
        except ValueError:
            answers = {}
        return discovery.wizard_steps(_one(params.get("name")), answers if isinstance(answers, dict) else {},
                                      cloud_accounts=clouds, service_accounts=services)
    if action == "wizard-build" and method == "POST":
        return discovery.wizard_build(data.get("workflow", ""), data.get("answers", {}),
                                      cloud_accounts=clouds, service_accounts=services)
    raise ValueError("unknown action %r (%s)" % (action, method))


def dispatch(action, method, params, body, user=None, https=False):
    """
    action  : str            e.g. "components", "schema", "credential-types", "examples", "validate", "save"
    method  : "GET"|"POST"
    params  : dict[str, list] parsed query string
    body    : bytes          request body (POST)
    user    : str|None       the logged-in user (login endpoint only)
    https   : bool           the request came over HTTPS
    returns : (http_status:int, obj:dict|list)

    The actions in admin.ACTIONS need HTTPS, a user, and that user on admin.authorized()'s allowlist. Being
    authenticated is not being permitted: an identity provider in front of this (see
    ../apache/lab-builder-sso.conf.example) authenticates its whole directory, so the allowlist is the grant.
    Every attempt at one of these actions is recorded with its user by admin.audit().
    """
    try:
        # No login: the builder asks its questions for anyone, like "schema" and "validate" do. An authorized
        # user on an HTTPS connection additionally gets the credential store's accounts by name, which the helper
        # reads as root; the names are not public, so they are not sent to anyone else or over plain HTTP.
        if action in admin.WIZARD_ACTIONS:
            data = json.loads(body.decode("utf-8") or "{}") if body else {}
            if user and https and admin.authorized(user):
                answer = _wizard(action, method, params, data, admin.cloud_accounts(), admin.service_accounts())
                admin.audit(user, action)
                return 200, answer
            return 200, _wizard(action, method, params, data, (), {})

        if action in admin.ACTIONS:
            if not https:
                return 403, {"error": "You must connect via HTTPS to use this UI"}
            if not user:
                return 401, {"error": "log in to use %s" % action}
            if not admin.authorized(user):
                admin.audit(user, action, "refused: not authorized")
                return 403, {"error": "'%s' is authenticated but not authorized to use lab-builder — add the "
                                      "identity to %s" % (user, admin.authorized_file())}
            data = json.loads(body.decode("utf-8") or "{}") if body else {}
            try:
                status, obj = 200, admin.dispatch(action, method, params, data)
            except Exception as e:
                admin.audit(user, action, "failed: {}".format(type(e).__name__))
                raise
            admin.audit(user, action)
            return status, obj

        if action == "auth" and method == "GET":
            return 200, {"login_configured": admin.login_configured(), "user": user or ""}

        # No login needed — the job id plus its random token (see admin.pub_status()) is the whole
        # credential, since this is the one page a QR code/direct link lets anyone reach. Still HTTPS-only:
        # the page it feeds holds the lab's own access credentials.
        if action == "pub-status" and method == "GET":
            if not https:
                return 403, {"error": "You must connect via HTTPS to view this page"}
            return 200, admin.pub_status(_one(params.get("job")), _one(params.get("token")))

        if action == "components" and method == "GET":
            comps = discovery.catalogue()
            return 200, {
                "count": len(comps),
                "components": comps,
                "facets": discovery.facets(comps),
                "scripts_dir": discovery.scripts_dir(),
                "libs_dir": discovery.libs_dir(),
            }

        if action == "schema" and method == "GET":
            return 200, discovery.schema(_one(params.get("name")))

        if action == "base" and method == "GET":
            return 200, discovery.base_schema()

        if action == "status" and method == "GET":
            return 200, discovery.status()

        if action == "credential-types" and method == "GET":
            return 200, discovery.credential_types()

        if action == "examples" and method == "GET":
            return 200, {"examples": discovery.examples()}

        if action == "example" and method == "GET":
            return 200, discovery.example(_one(params.get("name")))

        if method == "POST":
            data = json.loads(body.decode("utf-8") or "{}") if body else {}
            if action == "validate":
                return 200, discovery.validate_lab(data.get("config", {}))
            if action == "addresses":
                return 200, {"addresses": discovery.preview_addresses(data.get("config", {}))}

        return 400, {"error": "unknown action %r (%s)" % (action, method)}

    except (ValueError, FileNotFoundError) as e:
        return 400, {"error": str(e)}
    except Exception as e:  # never leak a stack trace as an HTTP 500 body
        return 500, {"error": "%s: %s" % (type(e).__name__, e)}
