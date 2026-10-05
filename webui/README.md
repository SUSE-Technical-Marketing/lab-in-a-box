# lab-builder — dynamic web UI for lab-in-a-box

A web interface that builds lab definitions by **introspecting the project's own
libraries at run time**. It lists every `install_*` component, and when you pick
one it renders a form from that component's `--schema` output. Add a new
component, or a new field to an existing one, and the UI picks it up
automatically — there is no per-component code in the UI.

## Design

Thin web layer over the existing Python libraries — **no subprocess fan-out**:

```
htdocs/            single-page app (vanilla HTML/CSS/JS)
 └─ app.js         generic schema walker: fields recognised by shape (name+type),
                   `fields`/`sections` treated as structural wrappers
lib/discovery.py   imports scripts/lab_schema (parse_script) and libs/primary
                   (validate_definition) in-process
lib/api.py         transport-agnostic request dispatch (one place)
cgi-bin/labbuilder.py   Apache CGI shim
run-local.py       zero-dependency dev server (no Apache needed)
apache/lab-builder.conf Apache drop-in
```

The only fixed convention is the schema vocabulary: a **field** is any object
with `name` + `type`; `fields`/`sections` are structural; a section may carry
`repeatable`. Everything else is discovered.

The add-on palette is split by where each add-on can be dropped, read from the
`targets` in its own `PLUGIN` dict: **Cluster add-ons** (`container` only — drop
on a Kubernetes cluster), **VM add-ons** (`vm`/`baremetal` only), **Cluster or
VM add-ons** (both), and **Lab services** (pxe, on the automation VM).

## Try it online (GitHub Pages)

**[→ Open lab-builder in your browser](https://rmahique.github.io/lab-in-a-box/lab-builder/)** — no backend required, fully static.

`scripts/build-cube-static.py` builds this page from `webui/htdocs/index.html`.
It embeds what the live API (`webui/lib/api.py`) answers for the add-on list,
every add-on's schema and the base schema, computed from the repo checkout, so
the static page shows the same add-ons, options and palette sections as a live
install. GitHub Actions rebuilds and redeploys it on every push to `main`/`dev`
that touches the add-ons, the libraries or the webui; if any add-on's schema
can't be read, the build fails and nothing is deployed.

With no server behind it, the static page hides the hypervisor status panel,
Refresh images, Validate and Save to server. Drag VMs, clusters and add-ons onto
the canvas, edit their settings, and use Download to get the lab.json.

Build it locally: `python3.11 scripts/build-cube-static.py [--output PATH]`
(default output: `webui/htdocs/lab-builder-static.html`).

## Run locally (any machine with Python 3)

```bash
python3 webui/run-local.py            # http://localhost:8677/
```

It auto-detects the scripts and libs directories:
`/usr/local/bin` + `/usr/local/lib/lab_creation` if installed, otherwise the
repo's `scripts/` and `libs/`.

## Deploy on the automation VM

`install_automation_node_scripts.sh` deploys this automatically; `_webui_mode`
picks how:

```bash
_webui_mode=apache  ./install_automation_node_scripts.sh   # default — Apache + mod_cgi
_webui_mode=service ./install_automation_node_scripts.sh   # standalone, no Apache — see below
_webui_mode=off      ./install_automation_node_scripts.sh   # skip webui entirely
```

**`apache`** (default, unchanged): copies the app to `/srv/www/lab-builder`,
drops in `webui/apache/lab-builder.conf`, enables `mod_cgi`, reloads Apache.
Equivalent manual steps:

```bash
cp -r webui /srv/www/lab-builder
mkdir -p /srv/www/lab-builder/labs && chown wwwrun /srv/www/lab-builder/labs
cp webui/apache/lab-builder.conf /etc/apache2/vhosts.d/
# ensure 'cgi' is enabled, then:
systemctl reload apache2
```

**`service`**: runs `run-local.py` (the zero-dependency stdlib server — no
Apache/CGI at all) as a persistent background process instead. Not tied to
systemd: the installer checks for `/run/systemd/system` and, if present,
installs+enables a `lab-builder.service` unit (`systemctl {start|stop|
restart|status} lab-builder`); otherwise it manages the same process through
a small init-independent control script, `/usr/local/bin/lab-builder-ctl
{start|stop|restart|status}` (a plain PID file under `/run/lab-builder.pid`
— no init system involved at all). `_webui_port` picks the port (default
`8677`). On a non-systemd target, add `lab-builder-ctl start` to whatever
that system uses for boot-time startup — there's no single portable way to
detect and hook every non-systemd init, so persistence across a reboot is
on you there.

Browse to `http://<automation-vm>/lab-builder/`.

**TLS**: on by default (`_webui_tls=1`) for both deploy modes — a self-signed
cert/key is generated once at `/etc/lab-builder/tls/{cert,key}.pem` and wired
into whichever mode is active (`run-local.py` wraps its own socket; Apache
gets an additional `lab-builder-ssl.conf` vhost + an HTTP→HTTPS redirect for
`/lab-builder`). Set `_webui_tls=0` for plain HTTP only. Browsers warn once
on the self-signed cert.

## Configuration (env vars, all optional)

| var | meaning | default |
|-----|---------|---------|
| `LABBUILDER_SCRIPTS_DIR` | dir with `install_*` + `lab_schema` | auto |
| `LABBUILDER_LIBS_DIR`    | dir with the python `libs` package | auto |
| `LABBUILDER_OUTPUT_DIR`  | where generated labs are written | `~/.lab-builder/labs` |
| `LABBUILDER_STATUS_FILE` | cached hypervisor status snapshot (see below) | `/srv/www/lab-builder/status.json` |
| `LABBUILDER_TLS_CERT`/`LABBUILDER_TLS_KEY` | TLS cert/key for `run-local.py` (`service` mode) | unset (plain HTTP) |

## Endpoints

| method | path | purpose |
|--------|------|---------|
| GET  | `api?action=components`     | list components + live count |
| GET  | `api?action=schema&name=install_longhorn` | one component's schema |
| GET  | `api?action=base`           | base topology schema (common/nodes/kclusters) |
| GET  | `api?action=status`         | cached hypervisor status snapshot (see below) |
| POST | `api?action=validate`       | validate a lab via `libs/primary` |
| POST | `api?action=save`           | write `lab.json` to the output dir |

## Hypervisor status

The top-of-page status panel and the live `ISO_IMAGE` dropdown are both fed
by one cached JSON snapshot at `LABBUILDER_STATUS_FILE`
(`/srv/www/lab-builder/status.json` by default) — the CGI **never** SSHes to
the hypervisor itself (it runs as the Apache user, and root's own SSH key
isn't readable by that user anyway). `scripts/refresh_hypervisor_status.py`
runs as root on a schedule (systemd timer, or `cron.d` without systemd —
installed automatically) and writes it: free CPU/RAM/disk per configured KVM
host, the `.iso`/`.qcow2` filenames at `ISO_LOC`, and a handful of non-secret
config values (`REMOTE_HOST`/`KVM_HOSTS`/`VIRT_SRV`/`ISO_LOC`). Any value
shaped like a secret by name (`*PASS*`/`*PWD*`/`*KEY*`/`*TOKEN*`/`*SECRET*`)
is masked to a fixed `********` before the file is ever written — never
partially revealed. `discovery.py`'s `schema()`/`base_schema()` read this
same file to give any field literally named `ISO_IMAGE` a live `enum` of
the discovered images, so it renders as a normal dropdown with zero
frontend changes.

## Scope

Builds a **complete lab.json**:

- **Base topology** — the pinned *▚ Lab topology* entry renders `common`
  (singleton) plus `nodes` and `kclusters` as **repeatable** keyed maps
  (add/remove instances). Its schema is the single source of truth in
  `lab_schema.base_lab_schema()`, which `setup_lab.sh --schema` also emits — so
  there is one definition, consumed in-process here (no subprocess).
- **Addon sections** — every `install_*` component (e.g. `longhorn: {…}`,
  `smlm: {…}`), rendered from its own `--schema`.

Possible next increment (deliberately not built): **execution** — running a
saved lab via `setup_lab.py`'s phase functions as a library.

## Deploy note

The base-topology feature needs the updated `lab_schema` / `setup_lab.sh` on the
automation VM. Redeploy the installed scripts the normal way
(`install_automation_node_scripts.sh`) so `/usr/local/bin/lab_schema` gains
`--base`; the web app auto-detects that installed copy.
