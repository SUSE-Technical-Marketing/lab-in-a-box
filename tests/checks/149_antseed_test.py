#!/usr/bin/env python3
# Unit tests for scripts/install_antseed — mocked ssh_run/ssh_output and package installs, no real SSH and no
# download. What is asserted is which commands each role produces, which desktop asset a node's architecture and
# package manager select, that the identity and passphrase only ever reach a root-only file, and what --check
# compares. Run from 149_antseed.sh, in its own container — see tests/run_tests.sh.
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "libs"))
sys.path.insert(0, str(_REPO / "scripts"))

import addon_common as ac  # noqa: E402
import apps  # noqa: E402

an = apps.load_addon_module("install_antseed", str(_REPO / "scripts"))  # no .py: loaded by path

failures = []


def check(desc, cond):
    if not cond:
        failures.append(desc)
        print("FAIL:", desc)


class FakeResult(object):
    def __init__(self, returncode=0):
        self.returncode = returncode
        self.stdout = ""
        self.stderr = ""


calls = []
returncodes = {}
outputs = {"uname -m": "x86_64", "--version": "0.1.261", "node --version": "v22.11.0"}
logs = []
warnings = []


def _fake_ssh_run(hostname, cmd, check=True, input_text=None, capture=False, user="root"):
    calls.append((cmd, input_text))
    for needle, code in returncodes.items():
        if needle in cmd:
            return FakeResult(returncode=code)
    return FakeResult()


def _fake_ssh_output(hostname, cmd, *a, **kw):
    calls.append((cmd, None))
    for needle, text in outputs.items():
        if needle in cmd:
            return text
    return ""


an.ssh_run = _fake_ssh_run
an.ssh_output = _fake_ssh_output
installed = []


def _fake_install_packages(host, pkgs, manager="", reboot=True, required=True, extra_args=""):
    installed.append((list(pkgs), manager))
    return manager or "zypper"


an.install_packages = _fake_install_packages
an.package_manager = lambda host: _MANAGER[0]
an.ensure_node = lambda host, manager=None: "zypper"
an.log = lambda m: logs.append(m)
an.warn = lambda m: warnings.append(m)
an.ac.resolve_credential = lambda cfg, kind, field_map, account_key=None, config=None: {
    "antseed_identity_hex": cfg.get("antseed_identity_hex") or "",
    "antseed_passphrase": cfg.get("antseed_passphrase") or "",
}
_MANAGER = ["zypper"]


def reset():
    del calls[:]
    del logs[:]
    del warnings[:]
    del installed[:]
    returncodes.clear()
    outputs.clear()
    outputs.update({"uname -m": "x86_64", "--version": "0.1.261", "node --version": "v22.11.0"})
    _MANAGER[0] = "zypper"


def sent():
    return "\n".join(c[0] for c in calls)


# ── roles ────────────────────────────────────────────────────────────────
check("a lab that names no role is a buyer, which is the side that consumes AI",
      an.roles_of({}) == ["buyer"])
check("\"both\" runs each role, the buyer first", an.roles_of({"antseed_role": "both"}) == ["buyer", "seller"])
check("a seller runs only itself", an.roles_of({"antseed_role": "seller"}) == ["seller"])
check("the proxy port has upstream's own default", an.port_of({}) == 8377)
check("a lab can move it", an.port_of({"antseed_proxy_port": "9000"}) == 9000)


# ── the CLI ──────────────────────────────────────────────────────────────
check("the latest is installed when nothing is pinned", an.package_spec({}) == "@antseed/cli")
check("a pinned version is pinned in npm", an.package_spec({"antseed_version": "0.1.261"})
      == "@antseed/cli@0.1.261")

reset()
an.install_cli("n1.mydemo.lab", {})
check("the CLI is installed globally with npm", "npm install -g @antseed/cli" in sent())
check("and its version is read back, so the log says what landed", any("0.1.261" in m for m in logs))

reset()
outputs["node --version"] = "v20.11.0"
an.install_cli("n1.mydemo.lab", {})
check("a Node older than the one Antseed is built against is warned about, not worked around",
      warnings and "22" in warnings[0])

reset()
an.install_cli("n1.mydemo.lab", {"antseed_user": "demo"})
check("another user's install runs through their own login shell, so ~/.antseed is theirs",
      "su - demo -c" in sent())
check("...and npm installs under their own prefix rather than the system one",
      "npm config set prefix" in sent())


# ── the secrets ──────────────────────────────────────────────────────────
reset()
written = an.write_environment("n1.mydemo.lab", {"antseed_identity_hex": "deadbeef",
                                                 "antseed_passphrase": "open sesame",
                                                 "antseed_env": {"ANTSEED_MIN_REPUTATION": "80"}})
check("the identity, the passphrase and the extra variables all land in the file",
      written == ["ANTSEED_IDENTITY_HEX", "ANTSEED_MIN_REPUTATION", "ANTSEED_PASSPHRASE"])
env_write = [c for c in calls if c[0].startswith("umask 077 && cat > /etc/antseed/env")]
check("the file is written with a tight umask, through stdin", env_write)
check("it holds one variable per line", env_write
      and "ANTSEED_IDENTITY_HEX=deadbeef\n" in env_write[0][1]
      and "ANTSEED_MIN_REPUTATION=80\n" in env_write[0][1])
check("no secret ever reaches a command line, where the node's process list would show it",
      "deadbeef" not in sent() and "open sesame" not in sent())
check("the directory is created root-only first", "chmod 0700 /etc/antseed" in sent())
check("the log names the variables without printing a value",
      any("ANTSEED_IDENTITY_HEX" in m for m in logs) and not any("deadbeef" in m for m in logs))

reset()
check("a lab with no secrets still gets an empty file, so the unit's EnvironmentFile always exists",
      an.write_environment("n1.mydemo.lab", {}) == []
      and any(c[0].startswith("umask 077 && cat > /etc/antseed/env") for c in calls))


# ── the services ─────────────────────────────────────────────────────────
unit = an.service_unit({}, "buyer")
check("the unit runs the role's own start command", "ExecStart=/usr/bin/env antseed buyer start" in unit)
check("it reads the secrets from the file rather than carrying them in its text",
      "EnvironmentFile=-/etc/antseed/env" in unit and "ANTSEED" not in unit.replace("ANTSEED_", ""))
check("it runs as the account the lab named", "User=root" in unit
      and "User=demo" in an.service_unit({"antseed_user": "demo"}, "buyer"))
check("it comes back by itself and at boot", "Restart=always" in unit
      and "WantedBy=multi-user.target" in unit)

reset()
an.start_roles("n1.mydemo.lab", {"antseed_role": "both"})
check("both roles get a unit of their own, enabled and started now",
      "/etc/systemd/system/antseed-buyer.service" in sent()
      and "/etc/systemd/system/antseed-seller.service" in sent()
      and "enable --now antseed-buyer.service" in sent()
      and "enable --now antseed-seller.service" in sent())


# ── the desktop ──────────────────────────────────────────────────────────
check("an apt node takes the .deb, named with the Debian architecture",
      an.desktop_asset("x86_64", "apt-get") == ("Antseed-Desktop-{version}-amd64.deb", "deb"))
check("anything else takes the AppImage, named with the machine architecture",
      an.desktop_asset("x86_64", "zypper") == ("Antseed-Desktop-{version}-x86_64.AppImage", "appimage"))
check("an arm64 node takes the arm64 asset of either kind",
      an.desktop_asset("aarch64", "zypper")[0] == "Antseed-Desktop-{version}-arm64.AppImage"
      and an.desktop_asset("aarch64", "apt-get")[0] == "Antseed-Desktop-{version}-arm64.deb")
check("an architecture upstream publishes neither for is no asset at all",
      an.desktop_asset("s390x", "zypper") == ("", ""))

reset()
an.install_desktop("n1.mydemo.lab", {})
check("the AppImage is fetched from the release of that version",
      "releases/download/v0.2.95/Antseed-Desktop-0.2.95-x86_64.AppImage" in sent())
check("it is made executable and linked onto the PATH, since upstream ships no installer for it",
      "chmod 0755" in sent() and "ln -sf" in sent() and "/usr/local/bin/antseed-desktop" in sent())
check("the log says it needs a desktop to run, which a lab node may not have",
      any("xfce" in m for m in logs))

reset()
_MANAGER[0] = "apt-get"
an.install_desktop("n1.mydemo.lab", {})
check("an apt node installs the .deb through the project's own package helper, so apt resolves its dependencies",
      "Antseed-Desktop-0.2.95-amd64.deb" in sent()
      and installed == [(["/opt/antseed/Antseed-Desktop-0.2.95-amd64.deb"], "apt-get")])

reset()
an.install_desktop("n1.mydemo.lab", {"antseed_desktop_url": "http://mirror.lab/antseed",
                                     "antseed_desktop_version": "v0.3.0"})
check("a mirror and a version the lab names are both honoured, with the same file names",
      "http://mirror.lab/antseed/v0.3.0/Antseed-Desktop-0.3.0-x86_64.AppImage" in sent()
      and "github.com" not in sent())

reset()
outputs["uname -m"] = "s390x"
an.install_desktop("n1.mydemo.lab", {})
check("an architecture with no asset is reported and skipped, not guessed at",
      warnings and "s390x" in warnings[0] and "curl" not in sent())


# ── the whole run ────────────────────────────────────────────────────────
reset()
an.setup_antseed("n1.mydemo.lab", {})
check("the default run installs the CLI, the desktop and the buyer service",
      "npm install -g @antseed/cli" in sent() and "AppImage" in sent()
      and "enable --now antseed-buyer.service" in sent())
check("and says where the proxy is, in both base-URL shapes",
      any("ANTHROPIC_BASE_URL" in m and "OPENAI_BASE_URL" in m for m in logs))

reset()
an.setup_antseed("n1.mydemo.lab", {"antseed_desktop": "false", "antseed_start": "false"})
check("a lab that wants neither the desktop nor a running service gets just the CLI",
      "AppImage" not in sent() and "systemctl" not in sent() and "npm install -g" in sent())

reset()
an.setup_antseed("n1.mydemo.lab", {"antseed_apps": ["claude-code", "codex"]})
check("each application named is pointed at the proxy with Antseed's own command",
      "antseed apps connect claude-code --port 8377" in sent()
      and "antseed apps connect codex --port 8377" in sent())

reset()
returncodes["apps connect codex"] = 1
an.connect_apps("n1.mydemo.lab", {"antseed_apps": ["codex"]})
check("an application Antseed does not know is reported rather than failing the node",
      warnings and "codex" in warnings[0])

reset()
an.setup_antseed("n1.mydemo.lab", {"antseed_role": "seller", "antseed_apps": ["codex"]})
check("a seller has no proxy, so nothing is pointed at one",
      "apps connect" not in sent() and "enable --now antseed-seller.service" in sent())


# ── --validate ───────────────────────────────────────────────────────────
def errors_for(section):
    v = ac.Validator({"antseed": section})
    an._validate(v)
    return "\n".join(v.errors)


check("a plain lab passes", errors_for({}) == "")
check("a role it does not have is refused", "must be one of" in errors_for({"antseed_role": "broker"}))
check("a port that is not one is refused", "must be a port" in errors_for({"antseed_proxy_port": "proxy"}))
check("the applications written as something else are refused",
      "must be a list" in errors_for({"antseed_apps": {"a": 1}}))
check("the extra environment must be a mapping",
      "must be a mapping" in errors_for({"antseed_env": ["A=b"]}))
check("the booleans are checked", "must be 'true' or 'false'" in errors_for({"antseed_desktop": "yes"}))


# ── --check ──────────────────────────────────────────────────────────────
reset()
chk = ac.Checker({"nodes": {}}, vm_name="n1.mydemo.lab")
an._check(chk)
check("a lab with no antseed section is not a failure", not chk.problems and not calls)

reset()
outputs["systemctl is-active"] = "active"
outputs["test -x"] = ""
chk = ac.Checker({"antseed": {}}, vm_name="n1.mydemo.lab")
an._check(chk)
check("a node with the CLI, a running buyer and the desktop passes", not chk.problems)

reset()
outputs["systemctl is-active"] = "failed"
chk = ac.Checker({"antseed": {}}, vm_name="n1.mydemo.lab")
an._check(chk)
check("a service that is not running is a difference", any("antseed-buyer" in p for p in chk.problems))

reset()
outputs["systemctl is-active"] = "active"
returncodes["antseed-desktop"] = 1
chk = ac.Checker({"antseed": {}}, vm_name="n1.mydemo.lab")
an._check(chk)
check("a missing desktop is a difference when the lab asked for one",
      any("Desktop" in p for p in chk.problems))

reset()
outputs["--version"] = ""
chk = ac.Checker({"antseed": {}}, vm_name="n1.mydemo.lab")
an._check(chk)
check("a CLI that does not answer is the difference that matters most",
      any("CLI" in p for p in chk.problems))


# ── the plugin the registry reads ───────────────────────────────────────
check("it installs on the node's own OS", an.PLUGIN["layers"] == ["os-native"])
check("it implements --check", an.PLUGIN["check"] is True)
check("it needs nothing of a cluster, being an OS-native install",
      an.PLUGIN["requires_cluster"] == [] and an.PLUGIN["requires_kubernetes"] is None)

if failures:
    print("{} check(s) failed".format(len(failures)))
    sys.exit(1)
print("all antseed checks passed")
