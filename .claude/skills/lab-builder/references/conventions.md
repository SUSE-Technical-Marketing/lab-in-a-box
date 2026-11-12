# Hard operational rules

These aren't style preferences — each one exists because violating it caused a real,
previously-reported problem in this project. Follow them regardless of how small the
change looks.

## Tests only run in containers

Never run an individual file under `tests/checks/` directly on the host, and never probe
behavior with a throwaway `python3 -c ...` or `podman run --rm` against real project code
"just to check something quickly." The *only* sanctioned way to run any test is:

```bash
./tests/run_tests.sh
```

This runs every script in `tests/checks/` in its own disposable podman container (see
`tests/Containerfile`) so a crash or leftover state in one check can never bleed into
another, and so nothing ever touches the host directly. It's also wired into
`.githooks/pre-commit`, so a `git commit` in this repo re-runs the full suite automatically
— if a commit seems to "hang" for several minutes, that's the test suite running, not a
bug. Note the test container's `python3` is old (3.6.x): `unittest.mock`'s `call.args`/
`call.kwargs` don't behave as in modern Python there — index `call` objects as plain
tuples instead if a test needs to inspect call arguments.

## Deploy only through the install script

Never `cp` a file straight into `/usr/local/lib/lab_creation/`, `/usr/local/bin/`, or
`/usr/share/lab_creation/templates/addons/` on the automation VM. The only sanctioned way
to push local changes live is:

```bash
LAB_DEPLOY_UNTRACKED=1 bash install_automation_node_scripts.sh
```

`install_automation_node_scripts.sh` does a backup first, fixes shebangs, and installs
everything consistently; a manual `cp` skips all of that and is how drift between the repo
and a running automation VM happens.

## Commits and attribution

- Never commit or push anything unless the user explicitly asks, for that specific
  change. Make and verify changes, then leave them uncommitted otherwise.
- Never add Claude/AI attribution or co-authorship to a commit, PR, code file, or doc of
  any kind, even if a session default says otherwise — this project has a standing rule
  against it.
- Stage files by name (`git add <path> <path>`), never `git add -A` or `git add .`. Any
  root-level `*.json` lab file, `cluster*/` directory, `TODO`, or similar is a runtime
  artifact from actually running the tooling, not source code — it must never be
  committed as a side effect of staging broadly, even when it's sitting modified in the
  working tree next to the real change.
- `CLAUDE.md` itself, and anything under a Claude memory/`.claude` config directory other
  than this skill's own files, must never be committed.

## SMLM/Uyuni steps need forever retry, not a timeout

Any provisioning step that depends on SUSE Multi-Linux Manager / Uyuni server-side async
state — channel sync, activation-key creation, client registration, maintenance-window
scheduling, anything that can legitimately still be "in progress" server-side when your
script's own call returns — must use the forever-retry pattern already built for this:
`launch_forever_retry()` / `run_forever_retry_worker()` in `libs/spacecmd_common.py`.
A fixed retry count or deadline on one of these has caused real live failures before
(the server can take well over an hour to converge under load). If you add a `verify`
callable, make it genuinely read-only and check the real end state through the API —
don't just trust that the mutating call returned without raising; a step can return
cleanly while handing off to its own background worker that hasn't actually finished yet.

## Shared logic lives in `libs/`

If logic would be useful to more than one `scripts/install_*` file, it belongs in a
`libs/*.py` module, not copy-pasted into each script. This is why `libs/db_common.py`
exists at all — it used to be duplicated MariaDB-install code inside two different addons
and was extracted once that became a maintenance problem. If you're writing a second
addon that needs something a first addon already does, go extract the shared piece into
`libs/` rather than copying it.

## Database-backed addons never roll their own container

An addon that needs a companion MariaDB or PostgreSQL instance (Nextcloud, Seafile, etc.)
calls `setup_mariadb_os()` / `setup_postgresql_os()` from `libs/db_common.py`. Don't stand
up an ad-hoc database container inside the addon itself — `db_common.py` already handles
OS detection (SUSE/RHEL-family/Debian-family) and the install differences between them;
duplicating that inline is exactly the kind of divergence `libs/` extraction exists to
prevent.

## Cloud backends change the ground rules

A lab whose nodes run on a cloud backend (AWS, etc. — see `libs/backends.py`) is not just
"KVM with extra config": cloud nodes typically have no static `myip`, often can't reach
the on-prem BIND DNS server directly, and their firewalling goes through security groups,
not `firewalld`/`iptables` on the guest. If a task touches a cloud-backed lab, check
`libs/backends.py` for `get_backend()`, `ensure_cloud_dns_vm()`, and the per-backend
"open ports" helpers before assuming an on-prem pattern will just work.

## Some actions require the user to do them, or to say yes first

Opening a security group / firewall rule to `0.0.0.0/0`, pushing to a remote, deploying to
a live automation node, and destroying a VM or lab are all real, hard-to-reverse, or
externally-visible actions. Implement the *code* for these freely; don't execute the
live, mutating version of them without the user's explicit go-ahead for that specific
action, even if an earlier similar action in the same session was approved.
