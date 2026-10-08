#!/usr/bin/env python3
"""
Portable local dev server for the lab-builder web UI — no Apache required.

    python3 webui/run-local.py [port]      # default 8677
    then open http://localhost:8677/

Serves htdocs/ statically and routes /api to the same dispatch the CGI uses.
/admin is the login endpoint: HTTP Basic auth against the users in
LABBUILDER_USERS (/etc/lab-builder/htpasswd, see lab-builder-passwd).
Stdlib only; works anywhere Python 3 runs.

Set LABBUILDER_TLS_CERT and LABBUILDER_TLS_KEY (both must be set) to serve
HTTPS instead of plain HTTP, using that cert/key pair — this is how
install_automation_node_scripts.sh's _webui_mode=service wires up HTTPS by
default; a bare manual `python3 run-local.py` (no env vars) keeps serving
plain HTTP exactly as before.
"""
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn
from urllib.parse import urlparse, parse_qs


class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    """Python 3.6-compatible stand-in for http.server.ThreadingHTTPServer."""
    daemon_threads = True

HERE = os.path.dirname(os.path.abspath(__file__))
HTDOCS = os.path.join(HERE, "htdocs")
sys.path.insert(0, os.path.join(HERE, "lib"))
import admin  # noqa: E402
import api  # noqa: E402

MIME = {".html": "text/html", ".js": "text/javascript", ".css": "text/css",
        ".json": "application/json", ".svg": "image/svg+xml", ".ico": "image/x-icon"}


class Handler(BaseHTTPRequestHandler):
    https = False

    def _api(self, method, login=False):
        u = urlparse(self.path)
        params = parse_qs(u.query)
        action = (params.get("action", [""]) or [""])[0]
        body = b""
        if method == "POST":
            n = int(self.headers.get("Content-Length", "0") or "0")
            body = self.rfile.read(n) if n else b""
        user = None
        if login:
            user = admin.check_login(self.headers.get("Authorization"))
            if not user:
                status, obj = 401, {"error": "log in to use the lab-builder's server actions"}
        if not login or user:
            status, obj = api.dispatch(action, method, params, body, user=user, https=self.https)
        payload = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        if status == 401 and login:
            self.send_header("WWW-Authenticate", 'Basic realm="lab-builder", charset="UTF-8"')
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/api", "/admin"):
            return self._api("GET", login=path == "/admin")
        self._static(path)

    def do_POST(self):
        path = urlparse(self.path).path
        if path in ("/api", "/admin"):
            return self._api("POST", login=path == "/admin")
        self.send_error(404)

    def _static(self, path):
        if path in ("", "/"):
            path = "/index.html"
        full = os.path.normpath(os.path.join(HTDOCS, path.lstrip("/")))
        if not full.startswith(HTDOCS) or not os.path.isfile(full):
            return self.send_error(404)
        data = open(full, "rb").read()
        self.send_response(200)
        self.send_header("Content-Type", MIME.get(os.path.splitext(full)[1], "application/octet-stream"))
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8677
    httpd = ThreadingHTTPServer(("0.0.0.0", port), Handler)

    tls_cert = os.environ.get("LABBUILDER_TLS_CERT")
    tls_key = os.environ.get("LABBUILDER_TLS_KEY")
    scheme = "http"
    if tls_cert and tls_key:
        import ssl
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(certfile=tls_cert, keyfile=tls_key)
        httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
        scheme = "https"
        Handler.https = True

    print("lab-builder dev server → %s://localhost:%d/  (Ctrl-C to stop)" % (scheme, port))
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
