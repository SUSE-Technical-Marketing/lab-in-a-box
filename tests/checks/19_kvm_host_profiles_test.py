#!/usr/bin/env python3
# Pure-logic unit tests for libs/kvm_host_profiles.py.
# No real /etc/os-release read, no real subprocess/systemctl calls — all
# mocked. Run from 19_kvm_host_profiles.sh, in its own container — see
# tests/run_tests.sh.
import subprocess
import sys
from pathlib import Path
from unittest import mock

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "libs"))

import kvm_host_profiles as khp  # noqa: E402

failures = []


def check(desc, cond):
    if not cond:
        failures.append(desc)
        print("FAIL:", desc)


def _profile_for(os_info):
    with mock.patch.object(khp, "_read_os_release", return_value=os_info):
        return khp.detect_profile()


# ── detect_profile(): one profile per (ID, major version) ────────────────────
for os_info, cls in [
        ({"ID": "opensuse-leap", "VERSION_ID": "15.6"}, khp.OpenSUSELeap15Profile),
        ({"ID": "opensuse-leap", "VERSION_ID": "16.0"}, khp.OpenSUSELeap16Profile),
        ({"ID": "sles", "VERSION_ID": "15.6"}, khp.SLES15Profile),
        ({"ID": "sles", "VERSION_ID": "16.0"}, khp.SLES16Profile),
        ({"ID": "debian", "VERSION_ID": "12"}, khp.Debian12Profile),
        ({"ID": "debian", "VERSION_ID": "13"}, khp.Debian13Profile),
        ({"ID": "ubuntu", "VERSION_ID": "22.04", "ID_LIKE": "debian"}, khp.Ubuntu2204Profile),
        ({"ID": "ubuntu", "VERSION_ID": "24.04", "ID_LIKE": "debian"}, khp.Ubuntu2404Profile),
        ({"ID": "rocky", "VERSION_ID": "9.8", "ID_LIKE": "rhel centos fedora"}, khp.EL9Profile),
        ({"ID": "almalinux", "VERSION_ID": "10.2", "ID_LIKE": "rhel centos fedora"}, khp.EL10Profile),
        ({"ID": "rhel", "VERSION_ID": "9.6", "ID_LIKE": "fedora"}, khp.EL9Profile),
        ({"ID": "centos", "VERSION_ID": "10", "ID_LIKE": "rhel fedora"}, khp.EL10Profile),
        ({"ID": "fedora", "VERSION_ID": "44"}, khp.FedoraProfile)]:
    p = _profile_for(os_info)
    check("{} {} resolves to {}".format(os_info["ID"], os_info["VERSION_ID"], cls.__name__),
          type(p) is cls and p.verified)

p = _profile_for({"ID": "opensuse-leap", "VERSION_ID": "17.1"})
check("an unknown Leap major version gets the newest Leap profile, marked unverified",
      isinstance(p, khp.OpenSUSELeap16Profile) and not p.verified)
p = _profile_for({"ID": "linuxmint", "VERSION_ID": "22", "ID_LIKE": "ubuntu debian"})
check("an Ubuntu derivative gets the newest Ubuntu profile, marked unverified",
      isinstance(p, khp.Ubuntu2404Profile) and not p.verified)
p = _profile_for({"ID": "ol", "VERSION_ID": "9.4", "ID_LIKE": "fedora rhel"})
check("an EL derivative gets the newest EL profile, marked unverified",
      isinstance(p, khp.EL10Profile) and not p.verified)
p = _profile_for({"ID": "", "ID_LIKE": "suse opensuse", "VERSION_ID": "15.6"})
check("ID_LIKE=suse alone resolves to an openSUSE Leap profile", isinstance(p, khp._SuseZypperProfile))
check("a genuinely unrecognised OS returns None", _profile_for({"ID": "unknownos", "VERSION_ID": "1"}) is None)

# ── package lists ─────────────────────────────────────────────────────────────
for cls in (khp.OpenSUSELeap15Profile, khp.SLES16Profile, khp.Debian12Profile, khp.EL9Profile, khp.FedoraProfile):
    check("{} requires fuse3 (guestmount needs fusermount3 to flush its writes)".format(cls.__name__),
          "fuse3" in cls.packages)
    check("{} keeps conveniences out of the required list".format(cls.__name__),
          not set(cls.packages) & set(cls.extra_packages + cls.unmapped_packages))
check("the EL list leaves curl out (curl-minimal conflicts with it)", "curl" not in khp.EL9Profile.packages)
check("SLES 15 and Leap 15 share their package lists, as do SLES 16 and Leap 16",
      khp.SLES15Profile.packages == khp.OpenSUSELeap15Profile.packages
      and khp.SLES16Profile.packages == khp.OpenSUSELeap16Profile.packages
      and khp.SLES16Profile.extra_packages == khp.OpenSUSELeap16Profile.extra_packages)
check("Leap/SLES 15 pin kubectl as an extra; 16 has no kubectl package",
      khp.OpenSUSELeap15Profile.kubectl_package in khp.OpenSUSELeap15Profile.extra_packages
      and khp.OpenSUSELeap16Profile.kubectl_package == "")
check("kubevirt-virtctl is an extra on Leap/SLES 16", "kubevirt-virtctl" in khp.SLES16Profile.extra_packages)
check("Debian/Ubuntu require bridge-utils (ifupdown bridges need it)", "bridge-utils" in khp.Debian13Profile.packages)

# ── install(): required packages in one transaction, extras best-effort ───────
p = khp.Debian12Profile({"ID": "debian", "VERSION_ID": "12"})


def _apt(fail_pkgs):
    def fake_run(cmd, check=False, **kwargs):
        rc = 100 if any(pkg in cmd for pkg in fail_pkgs) else 0
        if check and rc:
            raise subprocess.CalledProcessError(rc, cmd)
        return subprocess.CompletedProcess(cmd, rc)
    return fake_run


with mock.patch.object(subprocess, "run", side_effect=_apt(["mc"])) as m:
    failed = p.install()
calls = [c[0][0] for c in m.call_args_list]
check("install() installs every required package in its first call",
      all(pkg in calls[0] for pkg in p.packages))
check("install() retries the extras one by one when the batch fails and reports only the failing one",
      failed == ["mc"])
raised = False
with mock.patch.object(subprocess, "run", side_effect=_apt(["qemu-utils"])):
    try:
        p.install()
    except subprocess.CalledProcessError:
        raised = True
check("install() fails when a required package cannot be installed", raised)
check("install() installs the upstream kubectl binary where the OS has no kubectl package",
      ["sh", "-c", khp.kubectl_binary_script()] in calls)
leap15 = khp.OpenSUSELeap15Profile({"ID": "opensuse-leap", "VERSION_ID": "15.6"})
with mock.patch.object(subprocess, "run", side_effect=_apt([])) as m:
    leap15.install()
check("install() relies on the kubectl package where the OS has one",
      not any(c[0][0][:2] == ["sh", "-c"] for c in m.call_args_list))
script = khp.kubectl_binary_script()
check("the upstream kubectl script pins KUBECTL_VERSION and verifies the published SHA-256",
      "/release/v{}/bin/linux/".format(khp.KUBECTL_VERSION) in script and "sha256sum -c" in script
      and khp.OpenSUSELeap15Profile.kubectl_package == "kubernetes{}-client".format(
          ".".join(khp.KUBECTL_VERSION.split(".")[:2])))

# ── register_repos(): EPEL + CRB on EL, nothing elsewhere ─────────────────────
rocky = khp.EL9Profile({"ID": "rocky", "VERSION_ID": "9.8"})
check("Rocky enables EPEL and CRB", ["dnf", "config-manager", "--set-enabled", "crb"] in rocky.repo_setup
      and ["dnf", "install", "-y", "epel-release", "dnf-plugins-core"] in rocky.repo_setup)
rhel = khp.EL10Profile({"ID": "rhel", "VERSION_ID": "10.0"})
check("RHEL enables CodeReady Builder through subscription-manager and EPEL 10 from its URL",
      rhel.repo_setup[0][:3] == ["subscription-manager", "repos", "--enable"]
      and "codeready-builder-for-rhel-10-" in rhel.repo_setup[0][3]
      and rhel.repo_setup[1][-1].endswith("epel-release-latest-10.noarch.rpm"))
check("Fedora and Debian need no repository setup",
      khp.FedoraProfile({"ID": "fedora"}).repo_setup == [] and p.repo_setup == [])

# ── _SuseRegisteredProfile.register_repos(): the registration code is required ──
# The base product is registered with the registration code before any module is added. Modules alone fail on an unregistered host.
p = khp.SLES15Profile({"ID": "sles", "VERSION_ID": "15.6"})
raised = False
with mock.patch.object(subprocess, "run") as m:
    try:
        p.register_repos()
    except RuntimeError:
        raised = True
check("register_repos without a regcode raises instead of silently skipping base registration",
      raised)
check("register_repos without a regcode never calls SUSEConnect at all", m.call_count == 0)

p = khp.SLES15Profile({"ID": "sles", "VERSION_ID": "15.6"})
p.regcode = "SOME-REAL-REGCODE"
with mock.patch.object(subprocess, "run") as m:
    p.register_repos()
    calls = [c[0][0] for c in m.call_args_list]
check("register_repos with a regcode set registers the base product first",
      calls and calls[0] == ["SUSEConnect", "--regcode", "SOME-REAL-REGCODE"])
check("register_repos then adds every configured module on top of the base registration",
      all(c[:2] == ["SUSEConnect", "--product"] for c in calls[1:])
      and len(calls) == 1 + len(p._products))

p = khp.SLES15Profile({"ID": "sles", "VERSION_ID": "15.6"})
p.regcode = "SOME-REAL-REGCODE"
p.suse_email = "me@example.com"
p.suse_url = "https://scc.suse.com"
with mock.patch.object(subprocess, "run") as m:
    p.register_repos()
    base_call = m.call_args_list[0][0][0]
check("register_repos passes --email/--url through when set",
      "--email" in base_call and "me@example.com" in base_call
      and "--url" in base_call and "https://scc.suse.com" in base_call)


if failures:
    print("{} check(s) failed".format(len(failures)))
    sys.exit(1)
print("all kvm_host_profiles checks passed")
