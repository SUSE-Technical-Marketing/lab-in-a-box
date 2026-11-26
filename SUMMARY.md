# Summary of the Markdown documents in this repository

Every Markdown file that is part of the project, what it is for, and who it is written for. A new `.md` file is
added here in the same change that creates it.

## Start here

| Document | What it covers |
|---|---|
| [README.md](README.md) | What lab-in-a-box is, the two-tier architecture (hypervisor plus automation node), installation, the operational commands, the lab definition format, and the add-on table. The entry point for a new reader. |
| [EXAMPLES.md](EXAMPLES.md) | Worked lab definitions, from a single VM to multi-cluster and multi-cloud labs, with the command that builds each one. |
| [CONTRIBUTING.md](CONTRIBUTING.md) | How to work on the project: the test suite, the comment and clean-code rules, and the mandatory schema-version procedure (every schema versioned, a breaking change raises MAJOR and ships its migration in the same change). |

## Policies

| Document | What it covers |
|---|---|
| [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) | The standards expected of everyone taking part, and how to report a problem. |
| [SECURITY.md](SECURITY.md) | Which versions get fixes, and how to report a vulnerability privately. |

## Component documents

| Document | What it covers |
|---|---|
| [webui/README.md](webui/README.md) | The lab-builder web UI: running it locally or under Apache, its HTTP API, and what a login allows. |
| [tests/examples/README.md](tests/examples/README.md) | The fixtures the test suite uses, and what each one exercises. |
| [legacy_bash.md](legacy_bash.md) | The retired Bash implementation used to be kept in the tree as `legacy_bash/`; this note replaces it, with the exact `git log`/`git show` commands to actually look at it from history. |

## GitHub templates

| Document | What it covers |
|---|---|
| [.github/PULL_REQUEST_TEMPLATE.md](.github/PULL_REQUEST_TEMPLATE.md) | What a pull request has to state before review. |
| [.github/ISSUE_TEMPLATE/bug_report.md](.github/ISSUE_TEMPLATE/bug_report.md) | What a bug report needs to be actionable. |
| [.github/ISSUE_TEMPLATE/feature_request.md](.github/ISSUE_TEMPLATE/feature_request.md) | What a feature request needs to be assessable. |

## Claude Code skill references

Shared tooling for any Claude Code session working on this repository, under
`.claude/skills/lab-builder/`. Closer to CONTRIBUTING.md than to notes: they carry the conventions and the
architecture a session has to know before it changes anything.

| Document | What it covers |
|---|---|
| [.claude/skills/lab-builder/SKILL.md](.claude/skills/lab-builder/SKILL.md) | Writing a lab definition, and adding a feature to the toolkit; which reference to read for which task. |
| [.claude/skills/lab-builder/references/conventions.md](.claude/skills/lab-builder/references/conventions.md) | The operational rules: how to test, how to deploy, when it is safe to commit. |
| [.claude/skills/lab-builder/references/lab-json-schema.md](.claude/skills/lab-builder/references/lab-json-schema.md) | The lab definition's full field reference, with a worked example. |
| [.claude/skills/lab-builder/references/addon-pattern.md](.claude/skills/lab-builder/references/addon-pattern.md) | The annotated skeleton and checklist every `scripts/install_<name>` follows. |
| [.claude/skills/lab-builder/references/schema-versions.md](.claude/skills/lab-builder/references/schema-versions.md) | The mandatory, CI-enforced schema-version procedure. |
| [.claude/skills/lab-builder/references/dns-architecture.md](.claude/skills/lab-builder/references/dns-architecture.md) | How a lab's names resolve: sites against zones, the two kinds of DNS server, the two views, why there is no zone transfer, what one write touches, the registries, and the addresses that move. |

## Not in this list

The documentation site under `docs/` is HTML, built from `docs/src/` by `docs/build_docs.py`, so it has no Markdown
of its own: the English page plus seven translations, the Web UI Guide and the Add-on developers' guide.
`TODO` and `CHANGELOG` are working files, not tracked in git.
