<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="media/brand/lockup-horizontal-dark.svg">
    <img src="media/brand/lockup-horizontal-light.svg" alt="lab-in-a-box" width="420">
  </picture>
</p>

[![CI](https://github.com/SUSE-Technical-Marketing/lab-in-a-box/actions/workflows/ci.yml/badge.svg)](https://github.com/SUSE-Technical-Marketing/lab-in-a-box/actions/workflows/ci.yml)
![License](https://img.shields.io/badge/license-GPLv3-12a99d.svg)
![Python](https://img.shields.io/badge/python-3.11-1f2f4a.svg)

lab-in-a-box builds complete labs from a single JSON or YAML file: the VMs, DNS, Kubernetes clusters and add-ons you describe. The VMs can run on your own KVM hosts, on Harvester, or on any of eight public clouds, and the same file works across them.

**Documentation:** https://suse-technical-marketing.github.io/lab-in-a-box/

## Index

- [English documentation](https://suse-technical-marketing.github.io/lab-in-a-box/) — architecture, quick start, lab format, add-ons, configuration, testing
- [Web UI (lab-builder)](https://suse-technical-marketing.github.io/lab-in-a-box/webui.html)
- Translations: [Español](https://suse-technical-marketing.github.io/lab-in-a-box/es.html) · [Deutsch](https://suse-technical-marketing.github.io/lab-in-a-box/de.html) · [Français](https://suse-technical-marketing.github.io/lab-in-a-box/fr.html) · [Português (Brasil)](https://suse-technical-marketing.github.io/lab-in-a-box/pt-BR.html) · [日本語](https://suse-technical-marketing.github.io/lab-in-a-box/ja.html) · [简体中文](https://suse-technical-marketing.github.io/lab-in-a-box/zh-CN.html)

## Repository layout

- `docs/src/` — the documentation sources (HTML fragments)
- `docs/` — the generated site (GitHub Pages). Rebuild with `python3 docs/build_docs.py`
- `scripts/`, `libs/`, `webui/`, `templates/` — the code
- `tests/` — the containerized test suite (`tests/run_tests.sh`)

Contributing and security: see `CONTRIBUTING.md` and `SECURITY.md`.
