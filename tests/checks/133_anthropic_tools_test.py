#!/usr/bin/env python3
# Unit tests for scripts/install_anthropic_tools — mocked ssh_run and package calls, no real SSH.
# Run from 133_anthropic_tools.sh, in its own container — see tests/run_tests.sh.
import json
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "libs"))
sys.path.insert(0, str(_REPO / "scripts"))

import apps  # noqa: E402

at = apps.load_addon_module("install_anthropic_tools", str(_REPO / "scripts"))  # no .py: loaded by path

failures = []


def check(desc, cond):
    if not cond:
        failures.append(desc)
        print("FAIL:", desc)


class FakeResult:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


calls = []
installed = []
results = {}


def _fake_ssh_run(hostname, cmd, check=True, input_text=None, capture=False, user="root"):
    calls.append((hostname, cmd, input_text))
    for marker, result in results.items():
        if marker in cmd:
            return result
    return FakeResult(returncode=0)


def _fake_install_packages(host, pkgs, manager="", reboot=True, required=True, extra_args=""):
    installed.append((host, list(pkgs), manager, extra_args))
    return manager or "zypper"


at.ssh_run = _fake_ssh_run
at.install_packages = _fake_install_packages
at.package_available = lambda host, pkg, manager="": pkg in _AVAILABLE
at.package_manager = lambda host: _MANAGER[0]
# The Node packages and the package-set choice live in libs/nodejs.py, which resolves these for itself, so the
# same doubles go there too or it reaches for a real ssh.
import nodejs  # noqa: E402

nodejs.install_packages = _fake_install_packages
nodejs.package_available = at.package_available
nodejs.package_manager = at.package_manager
died = []
at.die = lambda m: died.append(m) or (_ for _ in ()).throw(SystemExit)

_AVAILABLE = {"python3-pip", "nodejs-default", "npm-default", "go", "curl"}
_MANAGER = ["zypper"]


def ran(marker):
    """Whether any command run so far contains `marker`."""
    return any(marker in c for _, c, _ in calls)


def written(marker):
    """The stdin of the first command run so far containing `marker`, None when none did."""
    for _host, cmd, input_text in calls:
        if marker in cmd:
            return input_text
    return None


def reset():
    del calls[:]
    del installed[:]
    del died[:]
    results.clear()


# ── Field readers ────────────────────────────────────────────────────────────
check("components: nothing selected installs the Claude Code CLI only",
      at.components({}) == ["claude_code"])
check("components: a selection comes back in install order, whatever order it was written in",
      at.components({"anthropic_tools_components": ["ant_cli", "settings", "claude_code"]}) ==
      ["claude_code", "ant_cli", "settings"])
check("components: an unknown name is left out (it is a validation error, not a crash)",
      at.components({"anthropic_tools_components": ["claude_code", "nonsense"]}) == ["claude_code"])
check("components: a comma-separated string is a list too",
      at.components({"anthropic_tools_components": "claude_code, api_sdk"}) == ["claude_code", "api_sdk"])
check("install_users: root by default", at.install_users({}) == ["root"])
check("install_users: a list is taken as it stands",
      at.install_users({"anthropic_tools_users": ["root", "demo"]}) == ["root", "demo"])
check("release: latest by default", at.release({}) == "latest")
check("method: native by default", at.method({}) == "native")
check("channel: an exact version still installs from the latest channel",
      at.channel({"anthropic_tools_release": "2.1.89"}) == "latest")
check("channel: a channel release is that channel",
      at.channel({"anthropic_tools_release": "stable"}) == "stable")

# ── Command builders ─────────────────────────────────────────────────────────
check("native_command: the default installer, told which release to install",
      at.native_command({}) == "curl -fsSL https://claude.ai/install.sh | bash -s latest")
check("native_command: an exact version is passed to the installer",
      at.native_command({"anthropic_tools_release": "2.1.89"}).endswith("bash -s 2.1.89"))
check("native_command: anthropic_tools_install_url replaces the installer",
      "https://mirror.example.com/install.sh" in
      at.native_command({"anthropic_tools_install_url": "https://mirror.example.com/install.sh"}))
check("as_user: root runs the command as it stands", at.as_user("root", "claude --version") == "claude --version")
check("as_user: another user runs it through su with a login shell",
      at.as_user("demo", "claude --version") == "su - demo -s /bin/bash -c 'claude --version'")
check("claude_command: reaches a native install no non-interactive shell has on its PATH",
      at.claude_command("--version") == 'PATH="$HOME/.local/bin:$PATH" claude --version')
check("profile_line_command: writes the line only when ~/.bashrc does not have it",
      at.profile_line_command("export X=1").startswith('grep -qxF \'export X=1\' "$HOME/.bashrc"'))
check("write_file_command: creates the directory, writes stdin and sets the mode",
      at.write_file_command(".claude/settings.json") ==
      'mkdir -p "$(dirname "$HOME"/.claude/settings.json)" && umask 077 && '
      'cat > "$HOME"/.claude/settings.json && chmod 600 "$HOME"/.claude/settings.json')
check("pip_command: falls back to --break-system-packages on an externally-managed Python",
      at.pip_command("anthropic") ==
      "python3 -m pip install --user anthropic || "
      "python3 -m pip install --user --break-system-packages anthropic")
check("npm_command: root installs globally as it is",
      at.npm_command("root", "@anthropic-ai/sdk") == "npm install -g @anthropic-ai/sdk")
check("npm_command: another user installs under their own prefix",
      at.npm_command("demo", "@anthropic-ai/sdk") ==
      'npm config set prefix "$HOME/.local" && npm install -g @anthropic-ai/sdk')
check("go_command: builds the ant CLI from Anthropic's own module",
      at.go_command() == "go install github.com/anthropics/anthropic-cli/v2/cmd/ant@latest")

# ── The signed package repositories ──────────────────────────────────────────
check("repo_key_command: an rpm family trusts the key with rpm --import",
      at.repo_key_command("zypper") == "rpm --import https://downloads.claude.ai/keys/claude-code.asc")
check("repo_key_command: zypper and dnf trust it the same way",
      at.repo_key_command("zypper") == at.repo_key_command("dnf"))
check("repo_key_command: apt puts it in its own keyring directory",
      "/etc/apt/keyrings/claude-code.asc" in at.repo_key_command("apt-get"))
check("repo_key_command: apk takes its own key format",
      at.repo_key_command("apk") ==
      "wget -O /etc/apk/keys/claude-code.rsa.pub https://downloads.claude.ai/keys/claude-code.rsa.pub")
check("repo_key_command: a package manager Anthropic publishes nothing for has no key command",
      at.repo_key_command("pacman") == "")

_zypp_path, _zypp = at.repo_file("zypper", "https://downloads.claude.ai/claude-code", "stable")
_dnf_path, _dnf = at.repo_file("dnf", "https://downloads.claude.ai/claude-code", "stable")
check("repo_file: zypper reads the repository from /etc/zypp/repos.d",
      _zypp_path == "/etc/zypp/repos.d/claude-code.repo")
check("repo_file: dnf reads the same definition from its own directory",
      _dnf_path == "/etc/yum.repos.d/claude-code.repo" and _dnf == _zypp)
check("repo_file: the rpm repository is the channel's own, with signature checking on",
      "baseurl=https://downloads.claude.ai/claude-code/rpm/stable" in _zypp and "gpgcheck=1" in _zypp)
_apt_path, _apt = at.repo_file("apt-get", "https://downloads.claude.ai/claude-code", "latest")
check("repo_file: apt's list file names the keyring that signs it",
      _apt_path == "/etc/apt/sources.list.d/claude-code.list" and
      _apt == ("deb [signed-by=/etc/apt/keyrings/claude-code.asc] "
               "https://downloads.claude.ai/claude-code/apt/latest latest main\n"))
check("repo_file: apk has no repository file of its own", at.repo_file("apk", "https://x", "stable") == ("", ""))
check("apk_repo_command: appends the repository only once",
      at.apk_repo_command("https://downloads.claude.ai/claude-code", "stable") ==
      "grep -qxF https://downloads.claude.ai/claude-code/apk/stable /etc/apk/repositories || "
      'printf "%s\\n" https://downloads.claude.ai/claude-code/apk/stable >> /etc/apk/repositories')

# ── Credentials and settings, as payloads ────────────────────────────────────
check("credential_env: no credential sets nothing", at.credential_env({}) == {})
check("credential_env: a subscription token is CLAUDE_CODE_OAUTH_TOKEN",
      at.credential_env({"anthropic_oauth_token": "sk-ant-oat-x"}) ==
      {"CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat-x"})
check("credential_env: an API key is ANTHROPIC_API_KEY",
      at.credential_env({"anthropic_api_key": "sk-ant-api-y"}) == {"ANTHROPIC_API_KEY": "sk-ant-api-y"})
check("env_file_content: one exported, quoted variable per line",
      at.env_file_content({"ANTHROPIC_API_KEY": "a b"}) == "export ANTHROPIC_API_KEY='a b'\n")

check("merge_settings: a key that is not written is kept",
      at.merge_settings({"keep": 1}, {"model": "m"}) == {"keep": 1, "model": "m"})
check("merge_settings: an object merges key by key instead of replacing",
      at.merge_settings({"env": {"KEEP": "1"}}, {"env": {"NEW": "2"}}) == {"env": {"KEEP": "1", "NEW": "2"}})
check("merge_settings: a scalar replaces", at.merge_settings({"model": "old"}, {"model": "new"})["model"] == "new")

check("settings_payload: nothing configured writes nothing", at.settings_payload({}) == {})
check("settings_payload: the named fields become Claude Code's own keys",
      at.settings_payload({"anthropic_tools_model": "claude-opus-5",
                           "anthropic_tools_disable_autoupdate": "true",
                           "anthropic_tools_release": "stable"}) ==
      {"model": "claude-opus-5", "env": {"DISABLE_AUTOUPDATER": "1"}, "autoUpdatesChannel": "stable"})
check("settings_payload: the package method pins no update channel, since it never auto-updates",
      "autoUpdatesChannel" not in at.settings_payload({"anthropic_tools_release": "stable",
                                                       "anthropic_tools_install_method": "package"}))
check("settings_payload: anthropic_tools_settings carries what no field names",
      at.settings_payload({"anthropic_tools_settings": {"permissions": {"allow": ["Bash(ls:*)"]}}}) ==
      {"permissions": {"allow": ["Bash(ls:*)"]}})
check("settings_payload: a named field wins over the same key in anthropic_tools_settings",
      at.settings_payload({"anthropic_tools_settings": {"model": "from-the-object"},
                           "anthropic_tools_model": "from-the-field"})["model"] == "from-the-field")
check("settings_payload: a credential joins the env block without dropping what is in the object",
      at.settings_payload({"anthropic_tools_settings": {"env": {"KEEP": "1"}}},
                          {"ANTHROPIC_API_KEY": "k"}) == {"env": {"KEEP": "1", "ANTHROPIC_API_KEY": "k"}})


# ── _validate ────────────────────────────────────────────────────────────────
class FakeValidator:
    def __init__(self, definition):
        self.definition = definition
        self.errors = []

    def _get(self, section, field):
        return (self.definition.get(section, {}) or {}).get(field, "") or ""

    def vbool(self, section, field):
        value = self._get(section, field)
        if value and str(value) not in ("true", "false"):
            self.errors.append("[ERROR] {}.{} must be 'true' or 'false'".format(section, field))

    def vurl_field(self, section, field):
        value = self._get(section, field)
        if value and not str(value).startswith(("http://", "https://")):
            self.errors.append("[ERROR] {}.{} must be a URL".format(section, field))


v = FakeValidator({"anthropic_tools": {}})
at._validate(v)
check("_validate: the defaults alone are valid", v.errors == [])

v = FakeValidator({"anthropic_tools": {"anthropic_tools_components": ["claude_code", "nonsense"]}})
at._validate(v)
check("_validate: an unknown component is an error", any("nonsense" in e for e in v.errors))

v = FakeValidator({"anthropic_tools": {"anthropic_tools_install_method": "rpm"}})
at._validate(v)
check("_validate: an unknown install method is an error",
      any("anthropic_tools_install_method" in e for e in v.errors))

v = FakeValidator({"anthropic_tools": {"anthropic_tools_install_method": "package",
                                       "anthropic_tools_release": "2.1.89"}})
at._validate(v)
check("_validate: the package method cannot pin an exact version",
      any("anthropic_tools_release" in e for e in v.errors))

v = FakeValidator({"anthropic_tools": {"anthropic_tools_install_method": "native",
                                       "anthropic_tools_release": "2.1.89"}})
at._validate(v)
check("_validate: the native method can pin an exact version", v.errors == [])

v = FakeValidator({"anthropic_tools": {"anthropic_tools_settings": "not-an-object"}})
at._validate(v)
check("_validate: anthropic_tools_settings must be an object",
      any("anthropic_tools_settings" in e for e in v.errors))

v = FakeValidator({"anthropic_tools": {"anthropic_tools_model": "claude-opus-5"}})
at._validate(v)
check("_validate: a settings field without the settings component is an error, not a silent no-op",
      any("anthropic_tools_model" in e for e in v.errors))

v = FakeValidator({"anthropic_tools": {"anthropic_tools_components": ["claude_code", "settings"],
                                       "anthropic_tools_model": "claude-opus-5"}})
at._validate(v)
check("_validate: the same field with the settings component is valid", v.errors == [])

v = FakeValidator({"anthropic_tools": {"anthropic_tools_components": ["settings"],
                                       "anthropic_tools_mcp_servers": {"lab": {"command": "/x"}}}})
at._validate(v)
check("_validate: MCP servers without the Claude Code CLI that registers them is an error",
      any("anthropic_tools_mcp_servers" in e for e in v.errors))

v = FakeValidator({"anthropic_tools": {"anthropic_tools_oauth_token": "t", "anthropic_tools_api_key": "k"}})
at._validate(v)
check("_validate: a subscription token and an API key together is an error, since the key would always win",
      any("not" in e and "both" in e for e in v.errors))

v = FakeValidator({"anthropic_tools": {"anthropic_tools_path": "yes"}})
at._validate(v)
check("_validate: anthropic_tools_path is a boolean field", any("anthropic_tools_path" in e for e in v.errors))

v = FakeValidator({"anthropic_tools": {"anthropic_tools_repo_url": "downloads.example.com"}})
at._validate(v)
check("_validate: anthropic_tools_repo_url must be a URL", any("anthropic_tools_repo_url" in e for e in v.errors))

# ── setup_anthropic_tools(): the default node ────────────────────────────────
reset()
results["claude --version"] = FakeResult(returncode=0, stdout="2.1.89 (Claude Code)\n")
at.setup_anthropic_tools("sol.mydemo.lab", {}, {})
check("setup: the default node runs Anthropic's own installer as root",
      ran("curl -fsSL https://claude.ai/install.sh | bash -s latest"))
check("setup: the default node installs no package", installed == [])
check("setup: the install is confirmed with claude --version", ran("claude --version"))
check("setup: ~/.local/bin lands on root's PATH", ran('export PATH="$HOME/.local/bin:$PATH"'))
check("setup: no credential writes no environment file", not ran(at.ENV_FILE))
check("setup: no settings component writes no settings file", not ran("settings.json"))
check("setup: a clean install does not die", died == [])

reset()
results["claude --version"] = FakeResult(returncode=1, stderr="claude: not found\n")
try:
    at.setup_anthropic_tools("sol.mydemo.lab", {}, {})
except SystemExit:
    pass
check("setup: an installed CLI that does not answer dies", len(died) == 1 and "claude --version" in died[0])

# ── setup_anthropic_tools(): per user, and the package method ────────────────
reset()
results["claude --version"] = FakeResult(returncode=0, stdout="2.1.89 (Claude Code)\n")
at.setup_anthropic_tools("sol.mydemo.lab", {"anthropic_tools_users": ["root", "demo"]}, {})
check("setup: the native method runs the installer once per user, since it is per-user by design",
      len([c for _, c, _ in calls if "install.sh" in c]) == 2)
check("setup: a user that is not root is installed for through su",
      ran("su - demo -s /bin/bash -c 'curl -fsSL https://claude.ai/install.sh"))

reset()
_MANAGER[0] = "zypper"
results["claude --version"] = FakeResult(returncode=0, stdout="2.1.89 (Claude Code)\n")
at.setup_anthropic_tools("sol.mydemo.lab", {"anthropic_tools_install_method": "package",
                                            "anthropic_tools_users": ["root", "demo"],
                                            "anthropic_tools_release": "stable"}, {})
check("setup: the package method installs the claude-code package once for the whole node",
      [p for _h, p, _m, _e in installed if p == ["claude-code"]] == [["claude-code"]])
check("setup: the package method runs no native installer", not ran("install.sh"))
check("setup: zypper imports the repository key before installing",
      ran("rpm --import https://downloads.claude.ai/keys/claude-code.asc"))
check("setup: the repository definition is written to zypper's own directory",
      ran("/etc/zypp/repos.d/claude-code.repo") and
      "baseurl=https://downloads.claude.ai/claude-code/rpm/stable" in written("claude-code.repo"))
check("setup: zypper is told to import the repository's keys on install",
      [e for _h, p, _m, e in installed if p == ["claude-code"]] == ["--gpg-auto-import-keys"])

reset()
_MANAGER[0] = "pacman"
try:
    at.setup_anthropic_tools("sol.mydemo.lab", {"anthropic_tools_install_method": "package"}, {})
except SystemExit:
    pass
check("setup: the package method dies on a package manager Anthropic publishes no repository for",
      len(died) == 1 and "pacman" in died[0])
_MANAGER[0] = "zypper"

# ── setup_anthropic_tools(): the SDKs and the ant CLI ────────────────────────
reset()
at.setup_anthropic_tools("sol.mydemo.lab", {"anthropic_tools_components": ["agent_sdk", "api_sdk", "ant_cli"]}, {})
check("setup: pip and node come from the node's own repositories",
      ["python3-pip"] in [p for _h, p, _m, _e in installed] and
      ["nodejs-default", "npm-default"] in [p for _h, p, _m, _e in installed])
check("setup: go comes from the node's own repositories for the ant CLI",
      ["go"] in [p for _h, p, _m, _e in installed])
check("setup: the Claude Agent SDK is installed for both languages",
      ran("pip install --user claude-agent-sdk") and ran("npm install -g @anthropic-ai/claude-agent-sdk"))
check("setup: the Anthropic API SDK is installed for both languages",
      ran("pip install --user anthropic") and ran("npm install -g @anthropic-ai/sdk"))
check("setup: the ant CLI is built from Anthropic's own module",
      ran("go install github.com/anthropics/anthropic-cli/v2/cmd/ant@latest"))
check("setup: the Go bin directory lands on the PATH, as upstream documents it",
      ran('export PATH="$PATH:$(go env GOPATH)/bin"'))
check("setup: no Claude Code component installs no Claude Code", not ran("install.sh"))

reset()
_AVAILABLE.discard("nodejs-default")
_AVAILABLE.discard("npm-default")
_AVAILABLE.update({"nodejs22", "npm22"})
at.setup_anthropic_tools("sol.mydemo.lab", {"anthropic_tools_components": ["api_sdk"]}, {})
check("setup: a distribution without the first node package name gets the next one it carries",
      ["nodejs22", "npm22"] in [p for _h, p, _m, _e in installed])
_AVAILABLE.update({"nodejs-default", "npm-default"})

# ── setup_anthropic_tools(): a configured credential ────────────────────────
reset()
results["claude --version"] = FakeResult(returncode=0, stdout="2.1.89 (Claude Code)\n")
at.setup_anthropic_tools("sol.mydemo.lab", {"anthropic_tools_components": ["claude_code"]},
                         {"CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat-x"})
_write_settings = 'umask 077 && cat > "$HOME"/.claude/settings.json'
check("setup: a credential reaches Claude Code through its own settings, with no settings component asked for",
      ran(_write_settings) and
      json.loads(written(_write_settings))["env"]["CLAUDE_CODE_OAUTH_TOKEN"] == "sk-ant-oat-x")
check("setup: a credential for Claude Code alone writes no environment file, which nothing would read",
      not ran(at.ENV_FILE))

reset()
at.setup_anthropic_tools("sol.mydemo.lab", {"anthropic_tools_components": ["api_sdk"]},
                         {"ANTHROPIC_API_KEY": "sk-ant-api-y"})
check("setup: a credential for the SDKs is written to an environment file they can read",
      written(at.ENV_FILE) == "export ANTHROPIC_API_KEY=sk-ant-api-y\n")
check("setup: that environment file is sourced from the user's own ~/.bashrc",
      ran(at.ENV_SOURCE_LINE.split("&&")[0].strip()))

# ── setup_anthropic_tools(): the settings component ──────────────────────────
reset()
results["claude --version"] = FakeResult(returncode=0, stdout="2.1.89 (Claude Code)\n")
results['cat "$HOME/.claude/settings.json"'] = FakeResult(returncode=0, stdout='{"theme": "dark"}')
at.setup_anthropic_tools("sol.mydemo.lab", {
    "anthropic_tools_components": ["claude_code", "settings"],
    "anthropic_tools_model": "claude-opus-5",
    "anthropic_tools_mcp_servers": {"lab": {"command": "/usr/local/bin/lab-mcp"}}}, {})
_settings = json.loads(written(_write_settings))
check("setup: the settings component writes the model it was given", _settings.get("model") == "claude-opus-5")
check("setup: a setting this add-on does not own survives being written to", _settings.get("theme") == "dark")
check("setup: an MCP server is registered through Claude Code itself, in the user's own scope",
      ran("mcp add-json lab") and ran("--scope user"))
check("setup: an MCP server is removed first, so a changed definition replaces the old one",
      [c for _h, c, _i in calls].index(
          [c for _h, c, _i in calls if "mcp remove lab" in c][0]) <
      [c for _h, c, _i in calls].index(
          [c for _h, c, _i in calls if "mcp add-json lab" in c][0]))

reset()
results["claude --version"] = FakeResult(returncode=0, stdout="2.1.89 (Claude Code)\n")
results['cat "$HOME/.claude/settings.json"'] = FakeResult(returncode=0, stdout="not json at all")
at.setup_anthropic_tools("sol.mydemo.lab", {"anthropic_tools_components": ["claude_code", "settings"],
                                            "anthropic_tools_model": "claude-opus-5"}, {})
check("setup: a settings file that is not JSON is left alone rather than overwritten",
      not ran(_write_settings))

reset()
at.setup_anthropic_tools("sol.mydemo.lab", {"anthropic_tools_components": []}, {})
check("setup: an empty component list installs nothing at all", calls == [] and installed == [])

reset()
results["claude --version"] = FakeResult(returncode=0, stdout="2.1.89 (Claude Code)\n")
at.setup_anthropic_tools("sol.mydemo.lab", {"anthropic_tools_path": "false"}, {})
check("setup: anthropic_tools_path 'false' leaves ~/.bashrc alone", not ran(".bashrc"))


if failures:
    print("{} failure(s)".format(len(failures)))
    sys.exit(1)
print("All install_anthropic_tools checks passed")
