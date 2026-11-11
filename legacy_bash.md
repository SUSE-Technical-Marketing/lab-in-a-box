# legacy_bash (removed from the working tree)

This repo used to carry a `legacy_bash/` directory: the retired, pre-`python_migration`
Bash implementation (addon scripts, orchestration scripts, the old repo-root `setup_lab.py`
YAML-to-JSON wrapper, and a few stale pre-Phase4/5 `.py` library forks), kept as a frozen,
read-only reference archive after the cutover to the Python addons that now live in this
repo's own top-level `scripts/` and `libs/`.

It was never deployed, never referenced by `install_automation_node_scripts.sh`, and nothing
in the live tree sourced it — it existed purely for human reference/rollback, matching the
"frozen archive, never audit, edit or report on it" rule this project already applied to it
(see the user-facing memory this project's own assistant keeps: never touch `legacy_bash/`
contents). Carrying 54 files / ~600 KB of dead weight in every checkout for that purpose
stopped being worth it, so the directory itself is gone — its content is not; it's in git
history, exactly where it always was.

## How to actually look at it

It's a normal part of this repo's commit history, so any ordinary git command reaches it:

```bash
# Find exactly when it was created, added to, and removed:
git log --all --oneline -- legacy_bash/

# Read one file as it was, at a specific commit (e.g. its own README):
git show <commit>:legacy_bash/README.md

# Get the whole directory back, as a real checkout, at a specific commit:
git checkout <commit> -- legacy_bash/
```

Key commits, as last recorded (run the `git log` command above for the authoritative,
up-to-date list — this file is not revisited every time something in that history changes):

- `fcd9f50` — "Finish moving superseded bash addons/orchestration to legacy_bash/": the
  commit that created the archive during the `python_migration` cutover.
- `042d772` (2026-10-13) — the last commit that touched anything under `legacy_bash/` before
  this note replaced the directory.
- The commit that actually removes `legacy_bash/` and adds this file in its place is the
  next one after `042d772` in `git log --all -- legacy_bash/ legacy_bash.md` — look there
  first if you need its exact hash.
