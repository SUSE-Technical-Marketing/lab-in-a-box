# Schema versions — mandatory, and enforced by CI

Every schema in this project — the lab definition itself, and each add-on's own JSON
section — is versioned `MAJOR.MINOR`. This is not a style preference: `CLAUDE.md` calls it
mandatory, and `scripts/schema_versions.py check` runs it in `tests/checks/80_schema_versions.sh`,
in both the pre-commit hook and CI. A schema change that skips this step passes locally (the
pre-commit hook tests the *working tree*, where an untracked file you just wrote is still
physically present) and then fails in CI (which checks out a clean tree, so a declared version
with no backing file, or a changed field with no recorded bump, shows up for the first time
there). This exact failure mode happened in this project's own history — an add-on's schema
entry stayed in `schemas/compatibility.json` after the add-on script itself was never committed,
and nothing caught it until a PR's CI run did.

## The moving parts

- **The declared version** lives in the script itself: a `# Schema version: X.Y` header comment
  (bash/addon scripts) or the equivalent for the lab definition (`libs/lab_schema_base.py`'s
  `LAB_SCHEMA_VERSION`). This is what the *current code* claims its own shape is.
- **`schemas/compatibility.json`** is the append-only history of every version that schema has
  ever had, each entry with a `changes` description and, for a MAJOR bump, a `migration` key.
  It is **not** derived from the current code and never will be — once a field is renamed, the
  old name only survives here. See `scripts/schema_versions.py`'s own `--help` for why this is a
  ledger, not a cache (`schema_versions.py check` is literally comparing the code's claim against
  this independent record).
- **`schemas/snapshots/<kind>/<name>-<version>.json`** is a snapshot of that version's actual
  field shape, written by `record` below — used to compute the diff between versions.
- **`libs/migrations.py`** holds the migration step for any MAJOR bump — a function that rewrites
  an old file into the new shape. `migrate_config.py` (and `migrations.ensure_current()`, called
  internally by `setup_*.py`/`setup_vm.py`/`setup_automation_node.py`) applies it automatically.

## The actual workflow, every time you touch a schema's fields

1. Change the fields (add/remove/rename one, change a default, etc.).
2. Decide MAJOR or MINOR: MAJOR only if an *existing* file written against the old version would
   now be read wrong or rejected (a renamed/removed/required-made field). Anything additive and
   backward-compatible is MINOR.
3. Bump the `# Schema version: X.Y` header comment (or `LAB_SCHEMA_VERSION`) to the new version —
   in the same change as the field edit, not a follow-up.
4. Run, from a repo checkout:
   ```bash
   python3.11 scripts/schema_versions.py diff <schema>      # addon:<name>, lab, or config:<file>
   python3.11 scripts/schema_versions.py record <schema> "<one-line description of the change>" \
       [--migration <step_name>]   # --migration is required for a MAJOR bump
   ```
   This writes the new snapshot and appends the `compatibility.json` entry for you — don't
   hand-edit `compatibility.json`.
5. If MAJOR, write the migration function in `libs/migrations.py` and pass its name as
   `--migration` above, **in the same commit** as the field change — CLAUDE.md is explicit that a
   breaking schema change ships with its migration, not as a follow-up PR.
6. Before committing, run `python3.11 scripts/schema_versions.py check` yourself (or just trust
   the pre-commit hook, which runs the whole suite including it) — don't rely on CI to be the
   first place this gets checked.

## The trap that actually bit this project

`schema_versions.py check` reads the **working tree on disk**, not git's index. If you're
staging a partial commit (some files committed, others — like a whole new untracked addon —
deliberately left out for now), the *check still sees the untracked file on disk* and will pass
locally even though a commit of just the schema-registry change, without the file, would be
inconsistent. If you ever split a commit this way (e.g. `git stash`/`cp` a file out of the
staged set), verify what the *committed* state actually looks like, not just what passed on your
disk: `git show <sha>:schemas/compatibility.json` vs. `git cat-file -e <sha>:scripts/install_<name>`
— don't assume "it passed before I committed" means "it's consistent after."

A `git stash push -- <pathspec>` does **not** capture untracked files, only tracked ones — if
you're using the stash-and-restore trick to split a commit, an untracked file sitting next to
tracked changes in the same working tree isn't covered by it.
