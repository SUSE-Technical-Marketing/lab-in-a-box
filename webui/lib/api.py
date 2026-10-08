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


def dispatch(action, method, params, body, user=None, https=False):
    """
    action  : str            e.g. "components", "schema", "validate", "save"
    method  : "GET"|"POST"
    params  : dict[str, list] parsed query string
    body    : bytes          request body (POST)
    user    : str|None       the logged-in user (login endpoint only)
    https   : bool           the request came over HTTPS
    returns : (http_status:int, obj:dict|list)

    The actions in admin.ACTIONS need both a user and HTTPS.
    """
    try:
        if action in admin.ACTIONS:
            if not https:
                return 403, {"error": "You must connect via HTTPS to use this UI"}
            if not user:
                return 401, {"error": "log in to use %s" % action}
            data = json.loads(body.decode("utf-8") or "{}") if body else {}
            return 200, admin.dispatch(action, method, params, data)

        if action == "auth" and method == "GET":
            return 200, {"login_configured": admin.login_configured(), "user": user or ""}

        if action == "components" and method == "GET":
            comps = discovery.discover()
            return 200, {
                "count": len(comps),
                "components": comps,
                "scripts_dir": discovery.scripts_dir(),
                "libs_dir": discovery.libs_dir(),
            }

        if action == "schema" and method == "GET":
            return 200, discovery.schema(_one(params.get("name")))

        if action == "base" and method == "GET":
            return 200, discovery.base_schema()

        if action == "status" and method == "GET":
            return 200, discovery.status()

        if method == "POST":
            data = json.loads(body.decode("utf-8") or "{}") if body else {}
            if action == "validate":
                return 200, discovery.validate_lab(data.get("config", {}))

        return 400, {"error": "unknown action %r (%s)" % (action, method)}

    except (ValueError, FileNotFoundError) as e:
        return 400, {"error": str(e)}
    except Exception as e:  # never leak a stack trace as an HTTP 500 body
        return 500, {"error": "%s: %s" % (type(e).__name__, e)}
