#!/usr/bin/env python3
"""
Apache CGI shim for lab-builder. All logic lives in ../lib; this only speaks CGI.
Deploy behind Apache with mod_cgi (see ../apache/lab-builder.conf).
"""
import json
import os
import sys
from urllib.parse import parse_qs

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib"))
import api  # noqa: E402

_STATUS = {200: "200 OK", 400: "400 Bad Request", 401: "401 Unauthorized", 403: "403 Forbidden",
           404: "404 Not Found", 500: "500 Internal Server Error"}


def main():
    method = os.environ.get("REQUEST_METHOD", "GET")
    params = parse_qs(os.environ.get("QUERY_STRING", ""))
    action = (params.get("action", [""]) or [""])[0]

    body = b""
    if method == "POST":
        try:
            n = int(os.environ.get("CONTENT_LENGTH", "0") or "0")
        except ValueError:
            n = 0
        if n > 0:
            body = sys.stdin.buffer.read(n)

    # REMOTE_USER is set by Apache only on the login endpoint (/lab-builder/admin), after Basic auth.
    status, obj = api.dispatch(action, method, params, body,
                               user=os.environ.get("REMOTE_USER") or None,
                               https=os.environ.get("HTTPS", "").lower() in ("on", "1"))
    payload = json.dumps(obj).encode("utf-8")

    out = sys.stdout.buffer
    out.write(("Status: %s\r\n" % _STATUS.get(status, "200 OK")).encode())
    out.write(b"Content-Type: application/json; charset=utf-8\r\n")
    out.write(("Content-Length: %d\r\n\r\n" % len(payload)).encode())
    out.write(payload)


if __name__ == "__main__":
    main()
