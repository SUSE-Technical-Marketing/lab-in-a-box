# lab-builder — dynamic web UI for lab-in-a-box

A web interface that builds lab definitions (`lab.json`) by **introspecting the
project's own Python libraries at run time**. It lists every installable
component, and when you pick one it renders a form from that component's schema.
Add a new `install_*` component, or a new field to an existing one, and the UI
picks it up automatically — there is **no per-component code in the UI**.

Lives in [`webui/`](webui/).

---

## Contents

- [What it does](#what-it-does)
- [How it stays 100% dynamic](#how-it-stays-100-dynamic)
- [Architecture](#architecture)
- [Setup — everything, from scratch](#setup--everything-from-scratch)
  - [0. Prerequisites](#0-prerequisites)
  - [1. Install the automation-node scripts + libraries](#1-install-the-automation-node-scripts--libraries)
  - [2a. Run locally (fastest, no Apache)](#2a-run-locally-fastest-no-apache)
  - [2b. Deploy on Apache (production, on the automation VM)](#2b-deploy-on-apache-production-on-the-automation-vm)
  - [2c. Deploy as a standalone service (no Apache)](#2c-deploy-as-a-standalone-service-no-apache)
- [Configuration](#configuration)
- [Using it — build a lab](#using-it--build-a-lab)
- [HTTP API](#http-api)
- [Scope](#scope)
- [Troubleshooting](#troubleshooting)

---

## What it does

- Shows a live **count of components** discovered by reading the definitions
  (61 `install_*` addons today).
- **▚ Lab topology** (pinned) renders the base lab definition: `common`
  (singleton) plus `nodes` and `kclusters` as **repeatable** keyed maps
  (add/remove instances — hostname → settings, cluster name → settings).
- Selecting any addon renders a form from its `--schema`: required fields
  flagged, defaults as placeholders, passwords masked, booleans as toggles,
  arrays as comma-lists, descriptions as help text.
- Assembles everything into a `lab.json`, **validates** it through the project's
  own `libs/primary`, then lets you **download** it or **save** it on the server.

## How it stays 100% dynamic

The frontend contains no knowledge of any specific script. It walks the schema
tree with one rule set ("Option B"):

- A **field** is any object with `name` + `type`, wherever it appears.
- `fields` / `sections` are **structural wrappers** (they never become output keys).
- Any other named object is a **group** that becomes an output key; a group
  marked `repeatable` becomes a keyed map of instances.

So new fields, new components and new nested sections all render with zero UI
changes. The only fixed convention is that small schema vocabulary — and it
lives in the definitions, not the UI.

## Architecture

Thin web layer over the existing Python libraries — **no subprocess fan-out**:

```
webui/
├── htdocs/                 single-page app (vanilla HTML/CSS/JS)
│   ├── index.html
│   ├── app.js              generic schema walker + lab assembly
│   └── style.css           theme-aware (light/dark)
├── lib/
│   ├── discovery.py        imports scripts/lab_schema (parse_script,
│   │                       base_lab_schema) and libs/primary (validate) in-process
│   └── api.py              transport-agnostic dispatch() — the only routing logic
├── cgi-bin/labbuilder.py   Apache mod_cgi shim
├── run-local.py            zero-dependency dev server (stdlib only)
├── apache/lab-builder.conf Apache drop-in
└── README.md               short version of this file
```

| Concern | Handled by | Notes |
|---------|-----------|-------|
| Component list + count | `lab_schema.parse_script()` over `install_*` | in-process |
| Base topology schema | `lab_schema.base_lab_schema()` | single source; `setup_lab.py --schema` emits the same |
| Validation | `libs/primary.validate_definition()` | stderr captured, `SystemExit` caught |
| Save | writes to `LABBUILDER_OUTPUT_DIR` | filename sanitised |

---

## Setup — everything, from scratch

### 0. Prerequisites

- The **automation VM** of a lab-in-a-box deployment (see the main project
  README for standing that up). It already runs Apache for the
  provisioning HTTP server, so the web server is present.
- **Python 3.11** — the toolchain pins to it explicitly (most distros ship an older default `python3` alongside it).
- `curl` and `jq` are handy for the smoke tests below.

### 1. Install the automation-node scripts + libraries

The web app reads the **installed** scripts and libraries. Make sure they're
present and current (this also installs `lab_schema` with `--base`, which the
base-topology form needs):

```bash
# on the automation VM, from the repo checkout:
./install_automation_node_scripts.sh
```

This puts `install_*`, `setup_lab.py`, `lab_schema` in `/usr/local/bin/` and the
Python libraries in `/usr/local/lib/lab_creation/` — exactly where the web app
auto-detects them.

> If you change a script/definition later, re-run `install_automation_node_scripts.sh`
> so the installed copies (and therefore the web UI) pick it up.

### 2a. Run locally (fastest, no Apache)

Zero dependencies — just Python 3:

```bash
python3 webui/run-local.py            # http://localhost:8677/
python3 webui/run-local.py 9000       # custom port
```

It auto-detects the scripts/libs directories: the installed
`/usr/local/bin` + `/usr/local/lib/lab_creation` if present, otherwise the
repo's `scripts/` and `libs/`. To force the repo copies (e.g. testing local edits
before installing):

```bash
LABBUILDER_SCRIPTS_DIR="$PWD/scripts" \
LABBUILDER_LIBS_DIR="$PWD/libs" \
python3 webui/run-local.py
```

Smoke-test it:

```bash
curl -s "http://localhost:8677/api?action=components" | jq '.count'
curl -s "http://localhost:8677/api?action=base" | jq '.sections | keys'
```

### 2b. Deploy on Apache (production, on the automation VM)

The app is a static page plus one CGI endpoint (`mod_cgi`).

```bash
# 1. Copy the app somewhere Apache can serve it
sudo cp -r webui /srv/www/lab-builder

# 2. Create a writable output dir for saved labs (owned by the Apache user)
sudo mkdir -p /srv/www/lab-builder/labs
sudo chown wwwrun /srv/www/lab-builder/labs      # 'apache' on RHEL family

# 3. Drop in the vhost config
sudo cp webui/apache/lab-builder.conf /etc/apache2/vhosts.d/    # SLES/openSUSE
#   RHEL family:  sudo cp webui/apache/lab-builder.conf /etc/httpd/conf.d/

# 4. Make sure CGI is enabled
#   SLES/openSUSE: add 'cgid' to APACHE_MODULES in /etc/sysconfig/apache2, e.g.
#     sudo sed -i 's/^APACHE_MODULES="\(.*\)"/APACHE_MODULES="\1 cgid"/' /etc/sysconfig/apache2
#   RHEL family:   mod_cgid is on by default
sudo systemctl restart apache2                    # 'httpd' on RHEL family
```

Then browse to:

```
http://<automation-vm>/lab-builder/
```

The `apache/lab-builder.conf` sets `LABBUILDER_OUTPUT_DIR=/srv/www/lab-builder/labs`
and leaves scripts/libs auto-detected. Uncomment the `SetEnv` lines in it to
override.

The CGI runs as the Apache user. It only **reads** schemas and **writes** lab
JSON under the output dir — it never builds labs or runs anything privileged.

### 2c. Deploy as a standalone service (no Apache)

`install_automation_node_scripts.sh` can deploy the whole thing for you —
see [1. Install the automation-node scripts + libraries](#1-install-the-automation-node-scripts--libraries)
above, and set `_webui_mode`:

```bash
_webui_mode=service _webui_port=8677 ./install_automation_node_scripts.sh
```

This runs `run-local.py` itself (no Apache/mod_cgi involved at all) as a
persistent background process, bound to `0.0.0.0:${_webui_port}` (default
`8677`). How it's started/stopped is **not tied to systemd**: the installer
checks for `/run/systemd/system` — the standard signal that a system is
actually running under systemd, not just that a `systemctl` binary happens
to exist — and:

- **systemd present**: installs and enables `lab-builder.service`
  (`Type=simple`, `Restart=always`), managed the normal way:
  ```bash
  systemctl {start|stop|restart|status} lab-builder
  ```
- **no systemd**: falls back to a small, init-system-independent control
  script that does the same job with a plain PID file, no unit file, no
  service manager involved:
  ```bash
  lab-builder-ctl {start|stop|restart|status}
  ```
  On a target like this, add `lab-builder-ctl start` to whatever mechanism
  that system actually uses to run things at boot (there's no single
  detection-and-hook that covers every non-systemd init, so this only
  starts it for the current session on its own).

Either way, browse to `http://<automation-vm>:<port>/` — there's no
`/lab-builder/` path prefix in this mode (that prefix comes from the Apache
vhost config, which isn't in play here).

Use `_webui_mode=off` to skip deploying the web UI at all.

---

## Configuration

All optional; env vars (set in the shell for `run-local.py`, or via `SetEnv` in
the Apache config):

| var | meaning | default |
|-----|---------|---------|
| `LABBUILDER_SCRIPTS_DIR` | dir with `install_*` + `lab_schema` | `/usr/local/bin`, else `<repo>/scripts` |
| `LABBUILDER_LIBS_DIR`    | dir with the python `libs` package  | `/usr/local/lib/lab_creation`, else `<repo>/libs` |
| `LABBUILDER_OUTPUT_DIR`  | where generated labs are written    | `~/.lab-builder/labs` |

---

## Using it — build a lab

1. **Lab topology** — click *▚ Lab topology*. Fill `common` (image, RAM, disk,
   CPU, network). Under `nodes`, click **+ add nodes** for each VM: enter the
   hostname (FQDN) as the key, its `myip`, `kcluster`, and role. Under
   `kclusters`, add each cluster (name as key; `clu_type`, `clu_rel`, `mydomain`,
   and the `addons` list). Click **Add to lab**.
2. **Addons** — pick e.g. `longhorn`, fill its options, **Add to lab**. Repeat.
   (Remember to list the addon name in the relevant `kclusters.<name>.addons`
   array so it actually gets installed.)
3. Watch the **lab.json** panel assemble on the right.
4. **Validate** — runs `libs/primary` server-side; shows structural errors.
5. **Download** it, or **Save to server** (writes to the output dir). Then run it
   the usual way: `setup_lab.py <lab>.json`.

## HTTP API

Same-origin JSON. Under Apache the base is `/lab-builder/api`; under `run-local`
it's `/api`.

| method | path | purpose |
|--------|------|---------|
| GET  | `api?action=components` | list components + live count |
| GET  | `api?action=schema&name=install_longhorn` | one addon's schema |
| GET  | `api?action=base` | base lab-definition schema (common/nodes/kclusters) |
| POST | `api?action=validate` | `{ "config": {…} }` → validate via `libs/primary` |
| POST | `api?action=save` | `{ "filename": "mylab", "config": {…} }` → write file |

## Scope

Builds a **complete `lab.json`**: base topology (`common`/`nodes`/`kclusters`)
plus every addon config section. **Not** built (by design): executing a lab from
the web UI — you save the JSON and run `setup_lab.py` yourself. Running via
`setup_lab.py`'s phase functions as a library is a possible future increment.

## Troubleshooting

| Symptom | Cause / fix |
|---------|-------------|
| `action=base` returns HTTP 500 (`AttributeError: base_lab_schema`) | Installed `/usr/local/bin/lab_schema` is older than `--base`. Re-run `install_automation_node_scripts.sh`, or point at the repo copy with `LABBUILDER_SCRIPTS_DIR=$PWD/scripts`. |
| `setup_lab.py --schema` errors `lab_schema: command not found` | `lab_schema` isn't on `PATH`. It's in `/usr/local/bin` after install; in the repo run it from `scripts/` or add that dir to `PATH`. |
| `ImportError: cannot import name 'ThreadingHTTPServer'` | Old Python. `run-local.py` already ships a 3.6-compatible shim; make sure you're running the current file. |
| Apache serves the page but `/lab-builder/api` 404/500 | CGI not enabled, or `cgi-bin/labbuilder.py` not executable. Enable `cgid` (see setup 2b) and `chmod +x webui/cgi-bin/labbuilder.py`. |
| `Save` fails with a permissions error | The output dir isn't writable by the Apache user. `chown` it (setup 2b). |
| Component count is `!` in the header | The API call failed — check the browser console and the server log; usually a wrong scripts/libs dir. |
