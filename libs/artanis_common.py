#!/usr/bin/env python3
# Part of lab-in-a-box — shared Kubernetes-manifest shape for GNU Artanis (Guile Scheme web
# framework) applications, reused by install_colt.py and install_wikimusic.py.
# Author/s: Raul Mahiques
# License: GPLv3
"""
Both Colt (a git-backed blog engine) and WikiMusic (a musical-knowledge CMS) are built on GNU
Artanis and share the same basic runtime shape: one web-facing container, listening on a
configurable port (3000 by default, matching Artanis' own convention), needing persistent storage
for its content (git objects for Colt, a SQLite database for WikiMusic), exposed via one Ingress.

Neither project publishes a pre-built container image. Colt's repo has a Dockerfile at its root,
buildable with `docker build .`, but no image is pushed to a registry. WikiMusic's repo has no
Dockerfile and builds with GNU Guix (channels.scm/manifest.scm) instead.
So unlike almost every other addon in this project, BOTH `<name>_image` fields here are MANDATORY —
the operator must build and push an image themselves (Colt: `docker build` the repo directly;
WikiMusic: `guix pack -f docker` or an equivalent Guix-based build) before running either addon.
"""

APP_MANIFEST_TEMPLATE = """---
apiVersion: v1
kind: Namespace
metadata:
  name: {ns}
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: {name}
  namespace: {ns}
spec:
  replicas: 1
  selector:
    matchLabels:
      app: {name}
  template:
    metadata:
      labels:
        app: {name}
    spec:
      containers:
      - name: {name}
        image: {image}
        ports:
        - containerPort: {port}
{env_block}        volumeMounts:
        - name: data
          mountPath: {data_path}
      volumes:
      - name: data
        emptyDir: {{}}
---
apiVersion: v1
kind: Service
metadata:
  name: {name}
  namespace: {ns}
spec:
  ports:
    - port: {port}
      name: web
  selector:
    app: {name}
---
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: {name}
  namespace: {ns}
spec:
  rules:
    - host: {host}
      http:
        paths:
          - backend:
              service:
                name: {name}
                port:
                  name: web
            path: /
            pathType: Prefix
"""


def render_artanis_app(name, image, ns, host, port=3000, data_path="/data", env=None):
    """
    Render a Namespace+Deployment+Service+Ingress manifest for an Artanis-based app.

    name      : Kubernetes object name (also the app= label/selector value)
    image     : full image reference (operator-supplied — see module docstring)
    ns        : namespace
    host      : ingress hostname
    port      : container port the app listens on (Artanis' own default is 3000)
    data_path : where the app's persistent content/database lives inside the container
    env       : optional dict of extra env vars to set
    """
    env_block = ""
    if env:
        lines = ["      env:"]
        for key, value in env.items():
            lines.append("        - name: {}".format(key))
            lines.append("          value: \"{}\"".format(value))
        env_block = "\n".join(lines) + "\n"

    return APP_MANIFEST_TEMPLATE.format(
        name=name, image=image, ns=ns, host=host, port=port, data_path=data_path, env_block=env_block,
    )
