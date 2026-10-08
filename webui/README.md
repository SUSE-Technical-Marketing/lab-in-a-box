# lab-builder — dynamic web UI for lab-in-a-box

A web interface that builds lab definitions by **introspecting the project's own
libraries at run time**. It lists every `install_*` component, and when you pick
one it renders a form from that component's `--schema` output. Add a new
component, or a new field to an existing one, and the UI picks it up
automatically — there is no per-component code in the UI.

## Design

Thin web layer over the existing Python libraries. Add-ons are executables in
any language, so each one is asked for its own `install_<name> --schema json`
(run in parallel, cached per file in `~/.cache/lab_creation/addons.json` or
`$LAB_ADDON_CACHE`); everything else is imported in-process:

```
htdocs/            single-page app (vanilla HTML/CSS/JS)
 └─ app.js         generic schema walker: fields recognised by shape (name+type),
                   `fields`/`sections` treated as structural wrappers
lib/discovery.py   add-ons via libs/apps (describe: --schema json); imports
                   scripts/lab_schema (base_lab_schema) and libs/primary
                   (validate_definition) in-process
lib/api.py         transport-agnostic request dispatch (one place)
cgi-bin/labbuilder.py   Apache CGI shim
run-local.py       zero-dependency dev server (no Apache needed)
apache/lab-builder.conf Apache drop-in
```

The only fixed convention is the schema vocabulary: a **field** is any object
with `name` + `type`; `fields`/`sections` are structural; a section may carry
`repeatable`. Everything else is discovered.

The add-on palette is split by where each add-on can be attached, read from the
`layers` in the `capabilities` of its `--schema json` output: **Kubernetes cluster
add-ons** (`kubernetes` only), **VM add-ons** (`standalone-container` and/or
`os-native` only), **Kubernetes cluster or VM add-ons** (both), and
**Infrastructure** (pxe: lab-wide, on the automation node). A layer badge is
shown only when the section heading does not already state it. The canvas
enforces the same rule when a block is dropped; any add-on can also be dropped on
Common settings, which defines its settings without attaching it.

The lab.json panel is editable and has **Open…** for a `.json`/`.yaml` file
(YAML through the vendored `js-yaml`); the canvas is rebuilt from the definition.
It lists the lab's errors and warnings as you build (`lintLab()` in `app.js`),
and Download and Save to server stay greyed out while there are errors.

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
Refresh images, Validate and Save to server. Drag VMs, Kubernetes clusters and
add-ons onto the canvas or open a lab file, edit their settings, and use Download
to get the lab.json.

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

**TLS**: on by default (`_webui_tls=1`) for both deploy modes. The webui uses
the automation node's self-signed cert/key at `/etc/lab_creation/tls/{cert,key}.pem`,
generated once by `install_automation_node_scripts.sh` (an existing
`/etc/lab-builder/tls` pair is reused). `run-local.py` wraps its own socket;
in Apache mode the whole document root is served on port 443 by
`templates/apache/lab_creation-ssl.conf`, and `lab-builder-ssl.conf` adds an
HTTP→HTTPS redirect for `/lab-builder`. Set `_webui_tls=0` for plain HTTP only
(no redirect; port 443 stays open for the provisioning files). Browsers warn
once on the self-signed cert.

## Configuration (env vars, all optional)

| var | meaning | default |
|-----|---------|---------|
| `LABBUILDER_SCRIPTS_DIR` | dir with `install_*` + `lab_schema` | auto |
| `LABBUILDER_LIBS_DIR`    | dir with the python `libs` package | auto |
| `LABBUILDER_OUTPUT_DIR`  | where generated labs are written | `~/.lab-builder/labs` |
| `LABBUILDER_STATUS_FILE` | cached hypervisor status snapshot (see below) | `/srv/www/lab-builder/status.json` |
| `LABBUILDER_TLS_CERT`/`LABBUILDER_TLS_KEY` | TLS cert/key for `run-local.py` (`service` mode) | unset (plain HTTP) |

## Endpoints

`api` is open; `admin` is the login endpoint (see below). Every `admin`
action needs a login and HTTPS: plain HTTP answers 403 "You must connect via
HTTPS to use this UI", no login answers 401.

| method | path | purpose |
|--------|------|---------|
| GET  | `api?action=components`     | list components + live count |
| GET  | `api?action=schema&name=install_longhorn` | one component's schema |
| GET  | `api?action=base`           | base topology schema (common/nodes/kclusters) |
| GET  | `api?action=status`         | cached hypervisor status snapshot (see below) |
| GET  | `api?action=auth`           | `{login_configured, user}`: whether a login exists |
| POST | `api?action=validate`       | validate a lab via `libs/primary` |
| POST | `admin?action=save`         | `{filename, config}`: write `lab.json` to the output dir |
| GET  | `admin?action=labs`         | the saved labs, newest first |
| GET  | `admin?action=lab&name=X.json` | one saved lab |
| GET  | `admin?action=credentials`  | credential files (names, kinds, field names; never values) and the fields each provider/service takes |
| POST | `admin?action=credentials-add` | `{kind: cloud\|service, type, account, fields, passphrase\|null}`: write `<type>-<account>.yaml` |
| POST | `admin?action=credentials-encrypt` | `{file, passphrase}`: write `<file>.encrypted.yaml` with its secrets encrypted |
| POST | `admin?action=credentials-delete` | `{file}`: delete a credential file |
| POST | `admin?action=create`       | `{filename, keep}`: run `setup_lab.py [--keep]` on a saved lab as a job |
| GET  | `admin?action=job&id=J`     | a job's state (`running`/`done`/`failed`), exit code and log tail |
| GET  | `admin?action=jobs`         | every job, newest first |

## Login, credentials and Create lab

Save to server, Saved labs, Credentials and Create lab run on the login
endpoint (`/lab-builder/admin` under Apache, `/admin` under `run-local.py`):

- **Logins** are in `/etc/lab-builder/htpasswd` (`LABBUILDER_USERS`), managed on
  the automation node with `lab-builder-passwd <user>` (`--delete <user>`,
  `--list`). The file starts empty, so these actions stay locked until a user is
  added. Apache asks for the login (HTTP Basic) over HTTPS only; plain HTTP is
  redirected to HTTPS first, and without TLS these actions are refused.
- **Root actions** (credentials, Create lab) run through
  `/usr/local/sbin/lab-builder-helper` (`scripts/lab_builder_helper.py`): fixed
  sub-commands, validated names, secrets on stdin. The Apache user may run it, and
  nothing else, through `/etc/sudoers.d/lab-builder`; in `service` mode the UI
  runs as root and calls it directly. A lab-builder login can therefore create
  labs and manage credentials as root: give one only to administrators.
- **Credentials** are the files `setup_credentials.py` writes (in
  `/etc/lab_creation/credentials`, or `CREDENTIALS_PATH`). New ones are encrypted
  with a passphrase unless unticked; Encrypt writes an `.encrypted.yaml` copy and
  leaves the original until you delete it.
- **Create lab** saves the lab, then runs `setup_lab.py` (with `--keep` unless
  unticked) as a background job; its log is in
  `/var/lib/lab-builder/jobs/<job>/log`. A lab whose credentials are encrypted asks
  for a passphrase, so create it with `setup_lab.py` on the command line.

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
  `lab_schema.base_lab_schema()`, which `setup_lab.py --schema` also emits — so
  there is one definition, consumed in-process here (no subprocess).
- **Addon sections** — every `install_*` component (e.g. `longhorn: {…}`,
  `smlm: {…}`), rendered from its own `--schema`.

Create lab runs a saved lab with `setup_lab.py` (see "Login, credentials and
Create lab").

## Deploy note

The base-topology feature needs the updated `lab_schema` / `setup_lab.sh` on the
automation VM. Redeploy the installed scripts the normal way
(`install_automation_node_scripts.sh`) so `/usr/local/bin/lab_schema` gains
`--base`; the web app auto-detects that installed copy.
