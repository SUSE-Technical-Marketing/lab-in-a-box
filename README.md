# lab-in-a-box

[![CI](https://github.com/SUSE-Technical-Marketing/lab-in-a-box/actions/workflows/ci.yml/badge.svg)](https://github.com/SUSE-Technical-Marketing/lab-in-a-box/actions/workflows/ci.yml)
![License](https://img.shields.io/badge/license-GPLv3-12a99d.svg)
![Python](https://img.shields.io/badge/python-3.11-1f2f4a.svg)

lab-in-a-box turns a single bare-metal machine into a self-contained lab factory. Point it at a JSON or YAML file describing the VMs, Kubernetes clusters and software you want, and it builds the whole lab: DNS, provisioning, cluster bring-up and add-ons.

**Documentation:** https://suse-technical-marketing.github.io/lab-in-a-box/

## Index

- [English documentation](https://suse-technical-marketing.github.io/lab-in-a-box/) — architecture, quick start, lab format, add-ons, configuration, testing
- [Web UI (lab-builder)](https://suse-technical-marketing.github.io/lab-in-a-box/webui.html)
- Translations: [Español](https://suse-technical-marketing.github.io/lab-in-a-box/es.html) · [Deutsch](https://suse-technical-marketing.github.io/lab-in-a-box/de.html) · [Français](https://suse-technical-marketing.github.io/lab-in-a-box/fr.html) · [Português (Brasil)](https://suse-technical-marketing.github.io/lab-in-a-box/pt-BR.html) · [日本語](https://suse-technical-marketing.github.io/lab-in-a-box/ja.html) · [简体中文](https://suse-technical-marketing.github.io/lab-in-a-box/zh-CN.html)

## Repository layout

- `docs/md/` — the documentation sources (Markdown)
- `docs/` — the generated site (GitHub Pages). Rebuild with `python3 docs/build_docs.py`
- `scripts/`, `libs/`, `webui/`, `templates/` — the code
- `tests/` — the containerized test suite (`tests/run_tests.sh`)

Contributing and security: see `CONTRIBUTING.md` and `SECURITY.md`.
