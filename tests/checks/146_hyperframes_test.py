#!/usr/bin/env python3
# Unit tests for scripts/install_hyperframes — mocked ssh_run/ssh_output and package installs, no real SSH and no
# npx. What is asserted is which commands each agent's connector sends, that "all" means every agent, that an
# agent whose CLI is not on the node is skipped with a warning, and that work for another user goes through a
# login shell. Run from 146_hyperframes.sh, in its own container — see tests/run_tests.sh.
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "libs"))
sys.path.insert(0, str(_REPO / "scripts"))

import addon_common as ac  # noqa: E402
import apps  # noqa: E402

hf = apps.load_addon_module("install_hyperframes", str(_REPO / "scripts"))  # no .py: loaded by path

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
missing = set()      # commands this node does NOT have
outputs = {}
logs = []
warnings = []


def _fake_ssh_run(hostname, cmd, check=True, input_text=None, capture=False, user="root"):
    calls.append(cmd)
    for name in missing:
        if "command -v {}".format(name) in cmd:
            return FakeResult(returncode=1)
    if "test -e" in cmd:
        return FakeResult(returncode=1)      # nothing exists yet, so a scaffold happens
    return FakeResult()


def _fake_ssh_output(hostname, cmd, *a, **kw):
    calls.append(cmd)
    for needle, text in outputs.items():
        if needle in cmd:
            return text
    return ""


hf.ssh_run = _fake_ssh_run
hf.ssh_output = _fake_ssh_output
hf.install_packages = lambda *a, **kw: "zypper"
hf.package_manager = lambda host: "zypper"
hf.ensure_node = lambda host, manager=None: "zypper"
hf.first_available = lambda host, candidates, manager: list((candidates or [[]])[0])
hf.log = lambda m: logs.append(m)
hf.warn = lambda m: warnings.append(m)


def reset():
    del calls[:]
    del logs[:]
    del warnings[:]
    missing.clear()
    outputs.clear()


def sent():
    return "\n".join(calls)


# ── which agents a lab asks for ───────────────────────────────────────────
check("a lab that names none gets Claude Code, the one this project already installs elsewhere",
      hf.agents_of({}) == ["claude-code"])
check("\"all\" is every agent this add-on knows",
      hf.agents_of({"hyperframes_agents": "all"}) == list(hf.AGENTS))
check("the agents are connected in a fixed order, whatever order the lab wrote them in",
      hf.agents_of({"hyperframes_agents": ["gemini-cli", "claude-code"]})
      == hf.agents_of({"hyperframes_agents": ["claude-code", "gemini-cli"]}) == ["claude-code", "gemini-cli"])
check("one agent written as a string counts as a list of one",
      hf.agents_of({"hyperframes_agents": "cursor"}) == ["cursor"])
check("an explicit empty list connects nothing, which is a lab that only wants the CLI",
      hf.agents_of({"hyperframes_agents": []}) == []
      and hf.agents_of({"hyperframes_agents": ["  "]}) == [])
check("a name that is not an agent is reported rather than silently dropped",
      hf.unknown_agents({"hyperframes_agents": ["claude-code", "aider"]}) == ["aider"])
check("the five agents the request named are all connectable, plus OpenCode",
      set(hf.AGENTS) == {"claude-code", "codex", "copilot-cli", "cursor", "gemini-cli", "opencode"})


# ── as_user(): another account's plugins live in that account's home ──────
check("root's work is run as it is", hf.as_user("root", "npx hyperframes --version")
      == "npx hyperframes --version")
check("another user's work goes through a login shell, so their own home and PATH apply",
      hf.as_user("demo", "npx hyperframes --version")
      == "su - demo -c 'npx hyperframes --version'")


# ── the npx specifier ────────────────────────────────────────────────────
check("the latest is what an unpinned lab runs, as upstream's quickstart does", hf.npx_package({}) == "hyperframes")
check("a pinned version is pinned in npx too", hf.npx_package({"hyperframes_version": "1.4.0"})
      == "hyperframes@1.4.0")
check("a version written with the v is not given two",
      hf.npx_package({"hyperframes_version": "v1.4.0"}) == "hyperframes@1.4.0")


# ── the prerequisites ────────────────────────────────────────────────────
reset()
hf.install_prerequisites("n1.mydemo.lab", {})
check("ffmpeg's absence is reported, since without it a render cannot write an MP4", not warnings)

reset()
missing.add("ffmpeg")
hf.install_prerequisites("n1.mydemo.lab", {})
check("a node whose repositories carry no ffmpeg is warned about, naming what to do",
      warnings and "ffmpeg" in warnings[0] and "hyperframes_install_ffmpeg" in warnings[0])

reset()
hf.install_prerequisites("n1.mydemo.lab", {"hyperframes_install_ffmpeg": "false"})
check("a lab that brought its own ffmpeg is not second-guessed", not warnings and "command -v ffmpeg" not in sent())


# ── each agent's own connector ───────────────────────────────────────────
reset()
hf.connect_agent("n1.mydemo.lab", {}, "claude-code")
check("Claude Code gets the marketplace and then the plugin, in that order",
      "claude plugin marketplace add heygen-com/hyperframes" in sent()
      and "claude plugin install hyperframes@hyperframes" in sent()
      and sent().index("marketplace") < sent().index("plugin install"))

reset()
hf.connect_agent("n1.mydemo.lab", {}, "copilot-cli")
check("the Copilot CLI takes the same two commands under its own name",
      "copilot plugin marketplace add heygen-com/hyperframes" in sent()
      and "copilot plugin install hyperframes@hyperframes" in sent())

reset()
hf.connect_agent("n1.mydemo.lab", {}, "gemini-cli")
check("the Gemini CLI installs the extension from the repository, tracking upstream by default",
      "gemini extensions install https://github.com/heygen-com/hyperframes --auto-update" in sent())

reset()
hf.connect_agent("n1.mydemo.lab", {"hyperframes_version": "1.4.0"}, "gemini-cli")
check("a pinned CLI version pins the extension to the matching release tag instead",
      "--ref v1.4.0" in sent() and "--auto-update" not in sent())

for name in ("codex", "cursor", "opencode"):
    reset()
    hf.connect_agent("n1.mydemo.lab", {}, name)
    check("{}, which upstream gives no command for, gets the skills instead".format(name),
          "npx -y skills add heygen-com/hyperframes" in sent())

reset()
missing.add("claude")
connected = hf.connect_agent("n1.mydemo.lab", {}, "claude-code")
check("an agent whose CLI is not on the node is skipped and reported, not installed around",
      connected is False and warnings and "Claude Code" in warnings[0]
      and "plugin install" not in sent())

reset()
missing.add("claude")
hf.setup_hyperframes("n1.mydemo.lab", {"hyperframes_agents": ["claude-code", "gemini-cli"]})
check("...and the agents the node does have are still connected",
      "gemini extensions install" in sent() and warnings)


# ── the whole run ────────────────────────────────────────────────────────
reset()
hf.setup_hyperframes("n1.mydemo.lab", {"hyperframes_project": "/srv/video/demo"})
check("the CLI is fetched once up front, which both proves it resolves and caches it for the agents",
      "npx -y hyperframes --version" in sent())
check("the skills are installed by default, which is what upstream recommends first",
      "npx -y skills add heygen-com/hyperframes" in sent())
check("the project is scaffolded in its parent directory, under its own name",
      "mkdir -p /srv/video" in sent() and "npx -y hyperframes init demo" in sent())

reset()
hf.setup_hyperframes("n1.mydemo.lab", {"hyperframes_skills": "false", "hyperframes_agents": []})
check("a lab that wants neither skills nor an agent gets only the CLI",
      "skills add" not in sent() and "plugin install" not in sent() and "npx -y hyperframes --version" in sent())

reset()
hf.setup_hyperframes("n1.mydemo.lab", {"hyperframes_user": "demo", "hyperframes_agents": "claude-code"})
check("every command of another user's install runs as that user",
      "su - demo -c" in sent() and "\nnpx -y" not in "\n" + sent())


# ── --validate ───────────────────────────────────────────────────────────
def errors_for(section):
    v = ac.Validator({"hyperframes": section})
    hf._validate(v)
    return "\n".join(v.errors)


check("an agent this add-on does not connect is refused, listing the ones it does",
      "is not an agent" in errors_for({"hyperframes_agents": ["aider"]}))
check("\"all\" is accepted", errors_for({"hyperframes_agents": "all"}) == "")
check("the agents written as something other than a list are refused",
      "must be a list" in errors_for({"hyperframes_agents": {"a": 1}}))
check("a version that is not one is refused", "not a valid version"
      in errors_for({"hyperframes_version": "newest"}))
check("the booleans are checked", "must be 'true' or 'false'"
      in errors_for({"hyperframes_skills": "yes"}))
check("a plain lab passes", errors_for({}) == "")


# ── --check ──────────────────────────────────────────────────────────────
reset()
chk = ac.Checker({"nodes": {}}, vm_name="n1.mydemo.lab")
hf._check(chk)
check("a lab with no hyperframes section is not a failure", not chk.problems and not calls)

reset()
outputs["plugin list"] = "hyperframes@hyperframes  enabled"
chk = ac.Checker({"hyperframes": {}}, vm_name="n1.mydemo.lab")
hf._check(chk)
check("a node with the CLI, ffmpeg and the plugin passes", not chk.problems)

reset()
chk = ac.Checker({"hyperframes": {}}, vm_name="n1.mydemo.lab")
hf._check(chk)
check("an agent that does not report the plugin is a difference", chk.problems)

reset()
missing.add("ffmpeg")
outputs["plugin list"] = "hyperframes@hyperframes"
chk = ac.Checker({"hyperframes": {}}, vm_name="n1.mydemo.lab")
hf._check(chk)
check("a node that lost ffmpeg is a difference, since a render would fail on it",
      any("ffmpeg" in p for p in chk.problems))


# ── the plugin the registry reads ───────────────────────────────────────
check("it installs on the node's own OS, not on a cluster", hf.PLUGIN["layers"] == ["os-native"])
check("it is not a Kubernetes add-on", set(hf.PLUGIN["targets"]) == {"vm", "baremetal"})
check("it implements --check", hf.PLUGIN["check"] is True)
check("it needs nothing of a cluster, being an OS-native install", hf.PLUGIN["requires_cluster"] == [] and hf.PLUGIN["requires_kubernetes"] is None)

if failures:
    print("{} check(s) failed".format(len(failures)))
    sys.exit(1)
print("all hyperframes checks passed")
