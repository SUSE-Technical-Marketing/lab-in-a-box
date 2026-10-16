#!/usr/bin/env python3.11
# Creating a lab whose credentials are encrypted, without a terminal — see 95_passphrase_without_terminal.sh.
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "libs"))
sys.path.insert(0, str(_REPO / "webui" / "lib"))

import crypto_store  # noqa: E402
import primary  # noqa: E402

failures = []


def check(desc, cond):
    if not cond:
        failures.append(desc)
        print("FAIL:", desc)


# ── the passphrase in the environment ────────────────────────────────────────
os.environ.pop(crypto_store.PASSPHRASE_ENV, None)
check("no variable set means no passphrase", crypto_store.passphrase_from_env() is None)
os.environ[crypto_store.PASSPHRASE_ENV] = ""
check("an empty variable is no passphrase either", crypto_store.passphrase_from_env() is None)
os.environ[crypto_store.PASSPHRASE_ENV] = "hunter2"
check("a set variable is the passphrase", crypto_store.passphrase_from_env() == "hunter2")

envelope = crypto_store.encrypt_cascade("hunter2", b"AWS_SECRET_ACCESS_KEY: shhh\n")

prompts = []


def _prompt(label):
    prompts.append(label)
    return "typed-instead"


primary._PASSPHRASE_CACHE.clear()
plaintext, err = primary._decrypt_value(envelope, "acct", "aws-sandbox", _prompt)
check("the environment's passphrase decrypts without asking",
      err is None and b"shhh" in plaintext and prompts == [])
check("a passphrase that worked is cached", primary._PASSPHRASE_CACHE.get("acct") == "hunter2")

# A wrong one in the environment: asked once instead, never retried with the same value.
primary._PASSPHRASE_CACHE.clear()
prompts.clear()
os.environ[crypto_store.PASSPHRASE_ENV] = "wrong"
other = crypto_store.encrypt_cascade("typed-instead", b"ok\n")
plaintext, err = primary._decrypt_value(other, "acct2", "aws-other", _prompt)
check("a wrong environment passphrase falls back to asking", err is None and plaintext == b"ok\n")
check("...and the environment's value is not tried again", len(prompts) == 1)

# No way to ask at all: a clear error naming the variable, not a crash or a hang.
primary._PASSPHRASE_CACHE.clear()
os.environ.pop(crypto_store.PASSPHRASE_ENV, None)
plaintext, err = primary._decrypt_value(envelope, "acct3", "aws-sandbox", None)
if sys.stdin.isatty():
    print("note: this check needs a non-interactive stdin; skipped")
else:
    check("with no variable and no terminal, the error names the variable",
          plaintext is None and crypto_store.PASSPHRASE_ENV in err)
    os.environ[crypto_store.PASSPHRASE_ENV] = "wrong"
    plaintext, err = primary._decrypt_value(envelope, "acct4", "aws-sandbox", None)
    check("a wrong variable with no terminal is an error that says where it came from",
          plaintext is None and "from $" + crypto_store.PASSPHRASE_ENV in err)
os.environ.pop(crypto_store.PASSPHRASE_ENV, None)

# ── the helper hands it to the job, and to nothing else ──────────────────────
tmp = Path(tempfile.mkdtemp())
labs, jobs = tmp / "labs", tmp / "jobs"
labs.mkdir()
(labs / "mylab.json").write_text(json.dumps({"common": {}, "nodes": {}}))
fake_setup_lab = tmp / "fake-setup-lab"
fake_setup_lab.write_text(
    "#!/bin/bash\n"
    "echo \"lab=$*\"\n"
    "echo \"passphrase=${LAB_CREDENTIALS_PASSPHRASE:-<unset>}\" > %s/seen\n" % tmp)
fake_setup_lab.chmod(0o755)

env = dict(os.environ, LABBUILDER_LABS_DIR=str(labs), LABBUILDER_JOBS_DIR=str(jobs),
           LABBUILDER_SETUP_LAB=str(fake_setup_lab))
helper = [sys.executable, str(_REPO / "scripts" / "lab_builder_helper.py")]


def run_create(stdin_obj):
    r = subprocess.run(helper + ["lab-create", "mylab.json"], input=json.dumps(stdin_obj).encode(),
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
    return r, json.loads(r.stdout.decode() or "{}")


r, out = run_create({"passphrase": "hunter2"})
check("lab-create starts the job", r.returncode == 0 and out.get("job"))
job_dir = jobs / out["job"]
for _ in range(200):
    if (tmp / "seen").is_file() and (job_dir / "rc").is_file():
        break
    time.sleep(0.05)
check("the job ran", (tmp / "seen").is_file())
check("the passphrase reaches the job's own environment",
      "passphrase=hunter2" in (tmp / "seen").read_text())
check("it is not written into the job record",
      "hunter2" not in (job_dir / "job.json").read_text())
check("it is not written into the job log", "hunter2" not in (job_dir / "log").read_text())
check("it is not echoed back in the answer", "hunter2" not in r.stdout.decode())

(tmp / "seen").unlink()
r, out = run_create({})
check("a second job starts once the first has finished", r.returncode == 0 and out.get("job"))
for _ in range(200):
    if (tmp / "seen").is_file():
        break
    time.sleep(0.05)
check("a job started without a passphrase has none in its environment",
      "passphrase=<unset>" in (tmp / "seen").read_text())

# ── the UI asks for it and keeps it nowhere ──────────────────────────────────
index_html = (_REPO / "webui" / "htdocs" / "index.html").read_text()
app_js = (_REPO / "webui" / "htdocs" / "app.js").read_text()
admin_py = (_REPO / "webui" / "lib" / "admin.py").read_text()
check("the Create dialog asks for a passphrase", 'id="createPass"' in index_html
      and 'type="password"' in index_html)
check("it is sent with the create request", "passphrase }" in app_js or "passphrase," in app_js)
check("the field is cleared when the dialog opens and right after it is sent",
      app_js.count('$("#createPass").value = ""') == 2)
check("it is never put in the page's own state", "state.passphrase" not in app_js)
check("admin passes it to the helper on stdin, not as an argument",
      '{"passphrase": data.get("passphrase", "")}' in admin_py)

if failures:
    print("{} check(s) failed".format(len(failures)))
    sys.exit(1)
print("all passphrase-without-terminal checks passed")
