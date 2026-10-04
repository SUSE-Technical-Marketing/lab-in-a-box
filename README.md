<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="media/brand/lockup-horizontal-dark.svg">
    <img src="media/brand/lockup-horizontal-light.svg" alt="lab-in-a-box" width="420">
  </picture>
</p>

[![CI](https://github.com/SUSE-Technical-Marketing/lab-in-a-box/actions/workflows/ci.yml/badge.svg)](https://github.com/SUSE-Technical-Marketing/lab-in-a-box/actions/workflows/ci.yml)
![License](https://img.shields.io/badge/license-GPLv3-12a99d.svg)
![Python](https://img.shields.io/badge/python-3.11-1f2f4a.svg)

**lab-in-a-box** builds complete labs from a single JSON or YAML file: the VMs, DNS, Kubernetes clusters and add-ons you describe. The VMs can run on your own KVM hosts, on Harvester, or on any of eight public clouds, and the same file works across them.

Documentation: https://rmahique.github.io/lab-in-a-box/

## Why lab-in-a-box?

- **One file, one command.** Describe VMs, Kubernetes clusters (RKE2 or K3s) and add-ons declaratively. `setup_lab.py` builds them in dependency order, without you running `virt-install` or Ansible by hand.
- **Home lab or cloud, same definition.** Lab VMs run on KVM hosts you own, on Harvester HCI, or on AWS, Google Cloud, Alibaba Cloud, Hetzner, Scaleway, UpCloud, OVHcloud or Exoscale. Labs can span several KVM hosts, and a single lab can mix clouds and sites through a [cross-cloud WireGuard overlay](https://rmahique.github.io/lab-in-a-box/#cross-cloud-wireguard-overlay).
- **72 ready-made add-ons.** Rancher, Longhorn, NeuVector, Harbor, Keycloak, Jenkins, Argo CD, SUSE Multi-Linux Manager/Uyuni, Nextcloud and more, plus intentionally vulnerable demo apps for security training.
- **A web UI that follows the code.** [lab-builder](https://rmahique.github.io/lab-in-a-box/#web-ui-lab-builder) renders each form from the add-on's own schema, so a new field in a script shows up in the UI with no front-end change.
- **Pluggable provisioning.** Ignition and Combustion for SLE Micro, cloud-init for openSUSE and Ubuntu, `virt-customize` for legacy images, or a scripted install from an installer ISO (AutoYaST, Kickstart, Preseed or AutoInstall).
- **Credentials kept out of the lab file.** Cloud and service credentials can live in per-account encrypted files (Argon2id key derivation, then AES-256-GCM and ChaCha20-Poly1305 in cascade).
- **Containerized tests.** Every check runs in a disposable podman container, and a pre-commit hook runs the relevant ones.

---

## Table of Contents

- [Architecture](https://rmahique.github.io/lab-in-a-box/#architecture)
- [How it works](https://rmahique.github.io/lab-in-a-box/#how-it-works)
- [Quick Start](https://rmahique.github.io/lab-in-a-box/#quick-start)
- [Web UI (lab-builder)](https://rmahique.github.io/lab-in-a-box/#web-ui-lab-builder)
- [Lab definition format](https://rmahique.github.io/lab-in-a-box/#lab-definition-format)
- [Examples](https://rmahique.github.io/lab-in-a-box/#examples)
- [Step-by-Step Walkthroughs](https://rmahique.github.io/lab-in-a-box/#step-by-step-walkthroughs)
- [Available commands](https://rmahique.github.io/lab-in-a-box/#available-commands)
- [Available addons](https://rmahique.github.io/lab-in-a-box/#available-addons)
- [Configuration reference](https://rmahique.github.io/lab-in-a-box/#configuration-reference)
- [Testing](https://rmahique.github.io/lab-in-a-box/#testing)
- [Contributing / Developer setup](https://rmahique.github.io/lab-in-a-box/#contributing--developer-setup)

Translations: [Español](https://rmahique.github.io/lab-in-a-box/es.html) · [Deutsch](https://rmahique.github.io/lab-in-a-box/de.html) · [Français](https://rmahique.github.io/lab-in-a-box/fr.html) · [Português (Brasil)](https://rmahique.github.io/lab-in-a-box/pt-BR.html) · [日本語](https://rmahique.github.io/lab-in-a-box/ja.html) · [简体中文](https://rmahique.github.io/lab-in-a-box/zh-CN.html)

## Repository layout

- `docs/src/` — the documentation sources (HTML fragments)
- `docs/` — the generated site (GitHub Pages). Rebuild with `python3 docs/build_docs.py`
- `scripts/`, `libs/`, `webui/`, `templates/` — the code
- `tests/` — the containerized test suite (`tests/run_tests.sh`)

Contributing and security: see `CONTRIBUTING.md` and `SECURITY.md`.
