#!/usr/bin/env python3
# Unit tests for scripts/install_voicestudio and libs/podman_service.py — mocked ssh_run/ssh_output, no real SSH
# and no container. What is asserted is the podman command the lab's fields produce, that the API key is passed
# through a file podman reads rather than on the command line, and that the systemd unit is written so the service
# survives a reboot. Run from 147_voicestudio.sh, in its own container — see tests/run_tests.sh.
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "libs"))
sys.path.insert(0, str(_REPO / "scripts"))

import addon_common as ac  # noqa: E402
import apps  # noqa: E402
import podman_service as ps  # noqa: E402

vs = apps.load_addon_module("install_voicestudio", str(_REPO / "scripts"))  # no .py: loaded by path

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
outputs = {}
logs = []


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


for module in (ps, vs):
    module.ssh_run = _fake_ssh_run
    module.ssh_output = _fake_ssh_output
    module.log = lambda m: logs.append(m)
ps.ensure_podman = lambda host: True
ps.warn = lambda m: logs.append(m)
vs.ac.resolve_credential = lambda cfg, kind, field_map, account_key=None, config=None: \
    {"voicestudio_api_key": cfg.get("voicestudio_api_key") or ""}


def reset():
    del calls[:]
    del logs[:]
    returncodes.clear()
    outputs.clear()


def sent():
    return "\n".join(c[0] for c in calls)


# ── podman_service.run_command(): what the library builds ─────────────────
command = ps.run_command("svc", "example.org/img:1", ports=["0.0.0.0:80:8080"],
                         env={"B": "two", "A": "one"}, volumes=["/srv/data:/data"],
                         options=["--shm-size 256m"], args=["serve"])
check("the container is named, detached and restarted by podman itself",
      command.startswith("podman run -d --name svc --restart=always"))
check("the port, the environment and the volume are all passed",
      "-p 0.0.0.0:80:8080" in command and "-e A=one" in command and "-e B=two" in command
      and "-v /srv/data:/data" in command)
check("the environment is ordered, so the same lab produces the same command twice",
      command.index("-e A=one") < command.index("-e B=two"))
check("further options and the container's own arguments keep their order, after the image",
      "--shm-size 256m" in command and command.rstrip().endswith("example.org/img:1 serve"))

host_net = ps.run_command("svc", "img", ports=["80:80"], network_host=True)
check("a host-network service publishes no ports, because it has the node's own",
      "--network host" in host_net and "-p " not in host_net)

with_file = ps.run_command("svc", "img", env_file="/etc/svc/env")
check("a secret goes in an env file podman reads, never in the command",
      "--env-file /etc/svc/env" in with_file)

unit = ps.unit_text("svc", "A service (lab-in-a-box)")
check("the unit starts and stops that container by name, and is wanted at boot",
      "podman start -a svc" in unit and "podman stop -t 10 svc" in unit
      and "WantedBy=multi-user.target" in unit)

reset()
ps.deploy("n1.mydemo.lab", "svc", "example.org/img:1", description="A service")
check("a deploy pulls the image, replaces whatever ran before, runs it and writes the unit",
      "podman pull example.org/img:1" in sent() and "podman rm -f svc" in sent()
      and "podman run -d --name svc" in sent() and "/etc/systemd/system/svc.service" in sent()
      and "systemctl enable svc.service" in sent())
check("the pull comes before the run, and the unit after it",
      sent().index("podman pull") < sent().index("podman run")
      and sent().index("podman run") < sent().index("svc.service"))

reset()
returncodes["curl -fsS"] = 1
ps.wait_for_http("n1.mydemo.lab", "http://127.0.0.1:3900/health", timeout=5, what="A service")
check("a service that never answers is reported rather than raised, since the container is there either way",
      any("did not answer" in m for m in logs))
check("...and the wait happens on the node, not here", "seq 1 5" in sent())


# ── the container this add-on asks for ────────────────────────────────────
reset()
vs.setup_voicestudio("n1.mydemo.lab", {})
check("the image is upstream's headless one, at the stable tag",
      "podman pull ghcr.io/debpalash/voicestudio:stable" in sent())
check("the port is published on every address, so the other nodes of the lab can reach it",
      "-p 0.0.0.0:3900:3900" in sent())
check("its data and its model cache are directories on the node, so a rebuilt container keeps them",
      "-v /var/lib/voicestudio:/app/omnivoice_data" in sent()
      and "-v /var/cache/voicestudio/huggingface:/root/.cache/huggingface" in sent())
check("both of those are created first", "mkdir -p /var/lib/voicestudio /var/cache/voicestudio/huggingface" in sent())
check("the port it is told to serve on is the one that is published", "-e OMNIVOICE_PORT=3900" in sent())
check("it waits for the health endpoint, long enough for a first-boot model download",
      "/health" in sent() and "seq 1 600" in sent())
check("the log says where the API, the web UI and the MCP endpoint are",
      any("/v1" in m for m in logs) and any("/mcp/" in m for m in logs))

reset()
vs.setup_voicestudio("n1.mydemo.lab", {"voicestudio_listen": "127.0.0.1", "voicestudio_port": "3999",
                                       "voicestudio_image_tag": "0.5.8",
                                       "voicestudio_mcp_output_mode": "files"})
check("a lab that wants it private publishes on loopback only", "-p 127.0.0.1:3999:3999" in sent())
check("the tag the lab names is the one pulled", "voicestudio:0.5.8" in sent())
check("the MCP output mode is passed through", "-e OMNIVOICE_MCP_OUTPUT_MODE=files" in sent())


# ── the API key never reaches a command line ──────────────────────────────
reset()
vs.ensure_api_key("n1.mydemo.lab", {"voicestudio_api_key": "s3cret"})
written = [c for c in calls if c[0].startswith("umask 077 && cat > /etc/voicestudio/env")]
check("a key the lab configured is written to a root-only file", written
      and written[0][1] == "OMNIVOICE_API_KEY=s3cret\n")
check("...and never appears in a command", "s3cret" not in sent())

reset()
vs.ensure_api_key("n1.mydemo.lab", {})
check("a lab with no key gets one generated on the node, once",
      "openssl rand -hex 32" in sent() and "test -s /etc/voicestudio/env" in sent())
check("...and is told where to read it", any("/etc/voicestudio/env" in m for m in logs))

reset()
vs.setup_voicestudio("n1.mydemo.lab", {})
check("podman reads the key from that file, so the process list does not show it",
      "--env-file /etc/voicestudio/env" in sent())


# ── --validate ───────────────────────────────────────────────────────────
def errors_for(section):
    v = ac.Validator({"voicestudio": section})
    vs._validate(v)
    return "\n".join(v.errors)


check("a plain lab passes", errors_for({}) == "")
check("a port that is not one is refused", "must be a port" in errors_for({"voicestudio_port": "http"}))
check("an output mode it does not have is refused",
      "must be resources, files or both" in errors_for({"voicestudio_mcp_output_mode": "inline"}))
check("a wait that is not a number is refused", "number of seconds" in errors_for({"voicestudio_wait": "ages"}))


# ── --check ──────────────────────────────────────────────────────────────
reset()
chk = ac.Checker({"nodes": {}}, vm_name="n1.mydemo.lab")
vs._check(chk)
check("a lab with no voicestudio section is not a failure", not chk.problems and not calls)

reset()
outputs["podman inspect"] = "ghcr.io/debpalash/voicestudio:stable"
outputs["/languages"] = '[{"code":"en"}]'
chk = ac.Checker({"voicestudio": {}}, vm_name="n1.mydemo.lab")
vs._check(chk)
check("a running container with the right image and a healthy endpoint passes", not chk.problems)

reset()
returncodes["podman ps"] = 1
chk = ac.Checker({"voicestudio": {}}, vm_name="n1.mydemo.lab")
vs._check(chk)
check("a container that is not running is the first thing reported, and nothing else is asked",
      chk.problems and "not running" in chk.problems[0] and "/health" not in sent())

reset()
outputs["podman inspect"] = "ghcr.io/debpalash/voicestudio:0.4.0"
chk = ac.Checker({"voicestudio": {}}, vm_name="n1.mydemo.lab")
vs._check(chk)
check("a container running another image than the lab asked for is a difference", chk.problems)

reset()
outputs["podman inspect"] = "ghcr.io/debpalash/voicestudio:stable"
returncodes["/health"] = 1
chk = ac.Checker({"voicestudio": {}}, vm_name="n1.mydemo.lab")
vs._check(chk)
check("a container that runs but answers nothing is a difference",
      any("health" in p for p in chk.problems))


# ── the plugin the registry reads ───────────────────────────────────────
check("it is a container on a node, not a Kubernetes workload",
      vs.PLUGIN["layers"] == ["standalone-container"] and set(vs.PLUGIN["targets"]) == {"vm", "baremetal"})
check("it implements --check", vs.PLUGIN["check"] is True)
check("it needs nothing of a cluster", vs.PLUGIN["requires_cluster"] == [] and vs.PLUGIN["aux_services"] == [])

if failures:
    print("{} check(s) failed".format(len(failures)))
    sys.exit(1)
print("all voicestudio checks passed")
