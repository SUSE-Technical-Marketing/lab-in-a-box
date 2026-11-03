#!/usr/bin/env python3.11
# QR code + public live-status page — see 112_live_status_page.sh.
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "libs"))
sys.path.insert(0, str(_REPO / "scripts"))
sys.path.insert(0, str(_REPO / "webui" / "lib"))

import qr_util  # noqa: E402

failures = []


def check(desc, cond):
    if not cond:
        failures.append(desc)
        print("FAIL:", desc)


tmp = Path(tempfile.mkdtemp())


def fake_bin(name, body):
    """A directory on its own, holding one executable `name` that runs `body` (a shell script)."""
    d = tmp / ("bin-" + name)
    d.mkdir(exist_ok=True)
    p = d / name
    p.write_text("#!/bin/bash\n" + body)
    p.chmod(p.stat().st_mode | stat.S_IEXEC)
    return str(d)


# ── qr_util: degrades to None with no qrencode, renders text through it when present ────────────────
no_qrencode_path = os.pathsep.join(
    p for p in os.environ.get("PATH", "").split(os.pathsep) if not (Path(p) / "qrencode").exists())
old_path = os.environ.get("PATH", "")

os.environ["PATH"] = no_qrencode_path
check("available() is False with no qrencode on PATH", qr_util.available() is False)
check("render_ascii() is None with no qrencode", qr_util.render_ascii("https://x/y") is None)
check("render_svg() is None with no qrencode", qr_util.render_svg("https://x/y") is None)
check("render_ascii('') is None even with no text", qr_util.render_ascii("") is None)

fake_qrencode_dir = fake_bin("qrencode", 'for a in "$@"; do [ "$a" = "--" ] && seen=1 && continue; '
                             '[ "${seen:-0}" = 1 ] && echo "SAW:$a"; done\n')
os.environ["PATH"] = fake_qrencode_dir + os.pathsep + no_qrencode_path
check("available() is True once qrencode is on PATH", qr_util.available() is True)
check("render_ascii() runs qrencode and returns its output",
      qr_util.render_ascii("https://example/x") == "SAW:https://example/x")
check("render_svg() runs qrencode and returns its output",
      (qr_util.render_svg("https://example/y") or "").rstrip("\n") == "SAW:https://example/y")

os.environ["PATH"] = old_path

# ── lab_builder_helper.py: token, status_url/qr_svg, pub-status, and that token never leaks back ────
labs, jobs = tmp / "labs", tmp / "jobs"
labs.mkdir()
(labs / "mylab.json").write_text(json.dumps({"common": {}, "nodes": {}}))
fake_setup_lab = tmp / "fake-setup-lab"
fake_setup_lab.write_text(
    "#!/bin/bash\n"
    "echo \"job=$LAB_STATUS_JOB token=$LAB_STATUS_TOKEN\"\n"
    "touch %s/seen\n" % tmp)
fake_setup_lab.chmod(fake_setup_lab.stat().st_mode | stat.S_IEXEC)

env = dict(os.environ, LABBUILDER_LABS_DIR=str(labs), LABBUILDER_JOBS_DIR=str(jobs),
           LABBUILDER_SETUP_LAB=str(fake_setup_lab), PATH=fake_qrencode_dir + os.pathsep + old_path)
helper = [sys.executable, str(_REPO / "scripts" / "lab_builder_helper.py")]


def run(args, stdin_obj=None):
    r = subprocess.run(helper + args, input=json.dumps(stdin_obj or {}).encode(),
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
    try:
        return r, json.loads(r.stdout.decode() or "{}")
    except ValueError:
        return r, {}


r, out = run(["lab-create", "mylab.json"])
check("lab-create succeeds", r.returncode == 0 and out.get("job"))
job = out.get("job", "")
check("lab-create's answer carries a status_url", "status_url" in out and out["status_url"].startswith("https://"))
check("...with the job id in it", job in out.get("status_url", ""))
check("lab-create's answer carries qr_svg (fake qrencode on PATH)",
      out.get("qr_svg", "").startswith("SAW:") and out["status_url"] in out["qr_svg"])
check("the token is not echoed back in lab-create's own answer",
      "token" not in out)  # the token lives inside status_url, not as its own field

for _ in range(200):
    if (tmp / "seen").is_file():
        break
    time.sleep(0.05)
check("the job ran with LAB_STATUS_JOB/LAB_STATUS_TOKEN set", (tmp / "seen").is_file())

job_json = json.loads((jobs / job / "job.json").read_text())
check("job.json stores the token", bool(job_json.get("token")))
token = job_json["token"]
check("the token is in the status_url that was returned", token in out["status_url"])

r, out = run(["lab-status", job])
check("lab-status never leaks the token", "token" not in out and r.returncode == 0)
r, out = run(["lab-jobs"])
check("lab-jobs never leaks any job's token either",
      r.returncode == 0 and all("token" not in j for j in out.get("jobs", [])))

# pub-status: right token works, and a wrong job / wrong token give the exact same refusal
r, out = run(["pub-status", job], {"token": token})
check("pub-status with the right token succeeds", r.returncode == 0 and "state" in out)
check("...and does not itself echo the token back", "token" not in out)

r_badtoken, out_badtoken = run(["pub-status", job], {"token": "not-it"})
r_badjob, out_badjob = run(["pub-status", "no-such-job"], {"token": token})
check("a wrong token is refused (exit 1)", r_badtoken.returncode == 1)
check("a wrong job id is refused (exit 1)", r_badjob.returncode == 1)
check("wrong-token and wrong-job give the identical message (no oracle)",
      out_badtoken.get("error") and out_badtoken.get("error") == out_badjob.get("error"))

r_notoken, out_notoken = run(["pub-status", job], {})
check("an empty token is refused the same way", r_notoken.returncode == 1
      and out_notoken.get("error") == out_badtoken.get("error"))

# ── webui/lib/admin.py + api.py: the public route, gated on HTTPS but not on login ──────────────────
# admin.helper() shells out to lab_builder_helper.py itself (as root here, so no "sudo -n" prefix —
# these tests run as root in the container; see tests/Containerfile), so it needs the same
# LABBUILDER_* overrides as a real environment, not just the `env=` dict used for the direct CLI calls
# above.
os.environ.update(LABBUILDER_LABS_DIR=str(labs), LABBUILDER_JOBS_DIR=str(jobs),
                   LABBUILDER_SETUP_LAB=str(fake_setup_lab),
                   LABBUILDER_HELPER=str(_REPO / "scripts" / "lab_builder_helper.py"))

import admin  # noqa: E402
import api  # noqa: E402

check("pub-status is not one of admin's login-gated ACTIONS", "pub-status" not in admin.ACTIONS)

status, body = api.dispatch("pub-status", "GET", {"job": [job], "token": [token]}, b"", user=None, https=False)
check("pub-status over plain HTTP is refused (403), no login involved", status == 403 and "HTTPS" in body["error"])

status, body = api.dispatch("pub-status", "GET", {"job": [job], "token": [token]}, b"", user=None, https=True)
check("pub-status over HTTPS with no user succeeds (no login needed)",
      status == 200 and body.get("state") in ("running", "done", "failed"))

status, body = api.dispatch("pub-status", "GET", {"job": [job], "token": ["wrong"]}, b"", user=None, https=True)
check("a wrong token through api.dispatch is a clean 400, not a 500", status == 400 and "error" in body)

# ── setup_lab.py: the link it prints is only there when launched as one of these jobs ────────────────
import setup_lab  # noqa: E402

for k in ("LAB_STATUS_JOB", "LAB_STATUS_TOKEN"):
    os.environ.pop(k, None)
check("no link with neither env var set (a bare CLI run)", setup_lab._status_page_url() is None)
os.environ["LAB_STATUS_JOB"] = job
check("still no link with only the job id set", setup_lab._status_page_url() is None)
os.environ["LAB_STATUS_TOKEN"] = token
url = setup_lab._status_page_url()
check("both set: a real https:// link naming this job and token", url is not None and url.startswith("https://")
      and job in url and token in url)
for k in ("LAB_STATUS_JOB", "LAB_STATUS_TOKEN"):
    os.environ.pop(k, None)

# ── setup_lab.py: a run behind the status page always streams addon output, regardless of --debug ──
for k in ("LAB_STATUS_JOB", "LAB_STATUS_TOKEN"):
    os.environ.pop(k, None)
check("no env set: --debug absent means quiet", setup_lab._debug_wanted(["lab.json"]) is False)
check("no env set: --debug still works explicitly", setup_lab._debug_wanted(["--debug", "lab.json"]) is True)
os.environ["LAB_STATUS_JOB"] = job
check("only LAB_STATUS_JOB set (no token): still quiet", setup_lab._debug_wanted(["lab.json"]) is False)
os.environ["LAB_STATUS_TOKEN"] = token
check("both LAB_STATUS_JOB and LAB_STATUS_TOKEN set: always streams, even without --debug",
      setup_lab._debug_wanted(["lab.json"]) is True)
for k in ("LAB_STATUS_JOB", "LAB_STATUS_TOKEN"):
    os.environ.pop(k, None)

# ── setup_lab.py: _addon_status()/_RunReport.pending — an addon that exits 0 but handed a step off to a
# forever-retry worker reports "pending", not "ok", and that alone keeps the final banner honest ─────
retry_state_dir = tmp / "retry-state"
retry_state_dir.mkdir()
old_state_dir = setup_lab.sc._FOREVER_RETRY_STATE_DIR
setup_lab.sc._FOREVER_RETRY_STATE_DIR = str(retry_state_dir)
try:
    check("a non-zero exit is always FAILED, regardless of forever-retry state",
          setup_lab._addon_status(1, time.time()) == "FAILED (exit 1)")
    t0 = time.time()
    check("exit 0 with no forever-retry activity since t0 is ok", setup_lab._addon_status(0, t0) == "ok")
    (retry_state_dir / "some_step.pid").write_text("12345")
    check("exit 0 with a forever-retry pid file written after t0 is pending",
          setup_lab._addon_status(0, t0) == "pending")
    later = time.time() + 5
    check("...but not once that handoff is clearly BEFORE the window being checked",
          setup_lab._addon_status(0, later) == "ok")

    report = setup_lab._RunReport()
    check("a fresh report is not pending", report.pending is False)
    report.add_addon("node:x", "someaddon", "pending")
    check("add_addon('pending') makes the report pending but not failed",
          report.pending is True and report.failed is False)
finally:
    setup_lab.sc._FOREVER_RETRY_STATE_DIR = old_state_dir

# ── setup_lab.py: _node_access_lines() — real IP (via DNS) + ssh command, a password only when one is
# actually configured, never guessed ──────────────────────────────────────────────────────────────────
import socket as _socket  # noqa: E402

_real_gethostbyname = _socket.gethostbyname


def _fake_gethostbyname(name):
    if name in ("resolvable.example.lab", "withpass.example.lab"):
        return "10.1.2.3"
    raise OSError("no such host")


_socket.gethostbyname = _fake_gethostbyname
try:
    fake_def = {"common": {}, "nodes": {
        "resolvable.example.lab": {},
        "unresolvable.example.lab": {},
        "withpass.example.lab": {"VM_ROOT_PASS": "s3cret"},
    }}
    lines = "\n".join(setup_lab._node_access_lines(fake_def))
    check("a resolvable node gets a real ssh command with its IP",
          "resolvable.example.lab" in lines and "ssh root@10.1.2.3" in lines)
    check("an unresolvable node says so instead of inventing an address",
          "unresolvable.example.lab" in lines and "not resolvable yet" in lines)
    check("a node with an explicit VM_ROOT_PASS shows it",
          "withpass.example.lab" in lines and "password: s3cret" in lines)
    check("a node with no VM_ROOT_PASS never shows a made-up password",
          "resolvable.example.lab — " in lines and "password:" not in lines.split("\n")[0])

    fake_def_nopass = {"common": {}, "nodes": {"solo.example.lab": {}}}
    lines2 = "\n".join(setup_lab._node_access_lines(fake_def_nopass))
    check("with no node having a plaintext password anywhere, a clear pointer is added instead of silence",
          "no plaintext password is configured" in lines2)
finally:
    _socket.gethostbyname = _real_gethostbyname

# ── status.html / app.js / index.html wiring: static checks, same spirit as 95's ────────────────────
status_html = (_REPO / "webui" / "htdocs" / "status.html").read_text()
app_js = (_REPO / "webui" / "htdocs" / "app.js").read_text()
index_html = (_REPO / "webui" / "htdocs" / "index.html").read_text()
check("status.html polls pub-status", "action=pub-status" in status_html)
check("status.html refuses to run over plain HTTP", "location.protocol !== \"https:\"" in status_html)
check("status.html pulls the LAB SUMMARY block out of the log", "LAB SUMMARY" in status_html)
check("app.js shows the status link/QR right after create", "showStatusLink(r.status_url, r.qr_svg)" in app_js)
check("the create dialog has a place for the status link and QR", 'id="jobStatusLink"' in index_html
      and 'id="jobStatusQr"' in index_html)

if failures:
    print("{} check(s) failed".format(len(failures)))
    sys.exit(1)
print("all live-status-page checks passed")
