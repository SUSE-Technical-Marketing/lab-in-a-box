#!/usr/bin/env python3.11
"""
lab-builder-helper — the actions of the lab-builder web UI that need root.

The web UI runs as the web server's user; it runs this helper through a sudoers
rule that allows only this file (or directly, when the UI itself runs as root).
Every command prints one JSON object on stdout. Secrets are read as one JSON
object on stdin, never from the command line. On a refused request the output
is {"error": "..."} and the exit status is 1.

Usage: lab-builder-helper <command> [argument]

  credentials-list            the stored credential files (names, kinds and
                              field names, never values) and the fields each
                              cloud provider and service kind takes
  credentials-add             write a new credential file; stdin:
                              {"kind": "cloud"|"service", "type": "<provider or
                              service kind>", "account": "<name>", "fields":
                              {...}, "passphrase": "<passphrase>" or null for a
                              plaintext file}
  credentials-encrypt FILE    write FILE's ".encrypted.yaml" copy with its
                              secret-looking fields encrypted; stdin:
                              {"passphrase": "..."}
  credentials-delete FILE     delete credential file FILE
  lab-create FILE [--keep]    start setup_lab.py on saved lab FILE in the
                              background (--keep: keep VMs that already exist);
                              prints the job id
  lab-status JOB              a job's state, exit code and the end of its log
  lab-jobs                    every job, newest first

Paths (environment overrides, for tests): LABBUILDER_LABS_DIR (saved labs,
/srv/www/lab-builder/labs), LABBUILDER_JOBS_DIR (job logs,
/var/lib/lab-builder/jobs), LABBUILDER_SETUP_LAB (/usr/local/bin/setup_lab.py),
CREDENTIALS_PATH in /etc/lab_creation.cfg (credential files).
"""

__version__ = "__LABVERSION__"

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

for _candidate in ("/usr/local/lib/lab_creation", str(Path(__file__).resolve().parent.parent / "libs")):
    if Path(_candidate).is_dir() and _candidate not in sys.path:
        sys.path.insert(0, _candidate)
for _candidate in ("/usr/local/bin", str(Path(__file__).resolve().parent)):
    if Path(_candidate, "setup_credentials.py").is_file() and _candidate not in sys.path:
        sys.path.append(_candidate)

import primary  # noqa: E402
import setup_credentials  # noqa: E402

LABS_DIR = Path(os.environ.get("LABBUILDER_LABS_DIR", "/srv/www/lab-builder/labs"))
JOBS_DIR = Path(os.environ.get("LABBUILDER_JOBS_DIR", "/var/lib/lab-builder/jobs"))
SETUP_LAB = os.environ.get("LABBUILDER_SETUP_LAB", "/usr/local/bin/setup_lab.py")
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
CREDENTIAL_EXTS = (".yaml", ".yml", ".json", ".cfg")
LOG_TAIL_LINES = 400


class Refused(Exception):
    """A request this helper does not carry out; its message goes back to the user."""


def safe_name(name: str, what: str) -> str:
    """`name` when it is a plain file name (no directory, no leading dot); Refused otherwise."""
    if not isinstance(name, str) or not NAME_RE.match(name) or ".." in name:
        raise Refused("invalid {} name {!r}".format(what, name))
    return name


def read_stdin() -> dict:
    """The JSON object on stdin, {} when stdin is empty."""
    text = sys.stdin.read()
    if not text.strip():
        return {}
    data = json.loads(text)
    if not isinstance(data, dict):
        raise Refused("stdin must hold a JSON object")
    return data


def credentials_dir() -> Path:
    """The directory new credential files go to (the first of primary.credentials_dirs())."""
    config = primary.load_config() if Path("/etc/lab_creation.cfg").exists() else {}
    return Path(primary.credentials_dirs(config)[0])


def load_credential(path: Path) -> dict:
    """Credential file `path` as a mapping; {} when it does not parse."""
    import yaml

    try:
        text = path.read_text()
        data = json.loads(text) if path.suffix == ".json" else (
            yaml.safe_load(text) if path.suffix in (".yaml", ".yml") else primary._parse_shell_vars(text))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def credential_entry(path: Path) -> dict:
    """What credentials-list shows for one file: never a value, only names and flags."""
    data = load_credential(path)
    lower = {k.lower(): v for k, v in data.items()}
    cloudtype = str(lower.get("cloudtype") or lower.get("cloud_type") or "")
    kind = str(lower.get("credential_kind") or lower.get("kind") or "")
    markers = {"cloudtype", "cloud_type", "credential_kind", "kind", "unencrypted"}
    whole = data.get("encrypted") is True
    fields = [] if whole else [k for k in data if k.lower() not in markers]
    return {
        "file": path.name,
        "name": path.stem,
        "kind": "cloud" if cloudtype else ("service" if kind else "unknown"),
        "type": cloudtype or kind,
        "fields": fields,
        "encrypted": whole,
        "encrypted_fields": [k for k in fields if isinstance(data[k], dict) and data[k].get("encrypted") is True],
        "plaintext_secrets": [] if whole else setup_credentials.sensitive_plaintext_fields(data),
    }


def credential_file(name: str) -> Path:
    """Existing credential file `name` in the credentials directory; Refused otherwise."""
    path = credentials_dir() / safe_name(name, "credential file")
    if path.suffix not in CREDENTIAL_EXTS or not path.is_file():
        raise Refused("no credential file {}".format(name))
    return path


def registry(kind: str) -> dict:
    """The field table for "cloud" (providers) or "service" (service kinds)."""
    if kind == "cloud":
        return setup_credentials.PROVIDER_FIELDS
    if kind == "service":
        return setup_credentials.SERVICE_CREDENTIAL_FIELDS
    raise Refused("kind must be cloud or service")


def credentials_list() -> dict:
    """Every credential file, plus the fields each provider and service kind takes."""
    d = credentials_dir()
    files = sorted(p for p in d.iterdir() if p.is_file() and p.suffix in CREDENTIAL_EXTS) if d.is_dir() else []
    table = lambda reg: {k: [{"name": n, "required": r, "secret": s} for n, r, s in v] for k, v in reg.items()}
    return {
        "dir": str(d),
        "credentials": [credential_entry(p) for p in files],
        "cloud": table(setup_credentials.PROVIDER_FIELDS),
        "service": table(setup_credentials.SERVICE_CREDENTIAL_FIELDS),
    }


def credentials_add(req: dict) -> dict:
    """Write <type>-<account>.yaml from request `req` (see the module docstring)."""
    kind = req.get("kind")
    table = registry(kind)
    ctype = req.get("type")
    if ctype not in table:
        raise Refused("unknown {} type {!r}".format(kind, ctype))
    account = safe_name(req.get("account", ""), "account")
    known = {n: r for n, r, _ in table[ctype]}
    given = {k: str(v) for k, v in (req.get("fields") or {}).items() if str(v) != ""}
    unknown = sorted(set(given) - set(known))
    if unknown:
        raise Refused("{} takes no field {}".format(ctype, ", ".join(unknown)))
    missing = [n for n, r in known.items() if r and n not in given]
    if missing:
        raise Refused("missing required field {}".format(", ".join(missing)))
    passphrase = req.get("passphrase")
    if passphrase is not None and len(passphrase) < 8:
        raise Refused("the passphrase must have at least 8 characters")
    d = credentials_dir()
    stem = "{}-{}".format(ctype, account)
    if any((d / (stem + ext)).exists() for ext in CREDENTIAL_EXTS):
        raise Refused("a credential file {} already exists".format(stem))
    write = setup_credentials.write_account_file if kind == "cloud" else setup_credentials.write_credential_file
    path = write(d / (stem + ".yaml"), ctype, given, passphrase is not None, passphrase=passphrase)
    return {"file": path.name}


def credentials_encrypt(name: str, req: dict) -> dict:
    """Write the ".encrypted.yaml" copy of credential file `name`."""
    path = credential_file(name)
    passphrase = req.get("passphrase") or ""
    if len(passphrase) < 8:
        raise Refused("the passphrase must have at least 8 characters")
    data = load_credential(path)
    keys = setup_credentials.sensitive_plaintext_fields(data)
    if not keys:
        raise Refused("{} has no plaintext secret to encrypt".format(name))
    out = path.parent / "{}.encrypted.yaml".format(path.stem)
    if out.exists():
        raise Refused("{} already exists".format(out.name))
    return {"file": setup_credentials.write_encrypted_copy(path, data, keys, passphrase).name, "encrypted": keys}


def credentials_delete(name: str) -> dict:
    """Delete credential file `name`."""
    path = credential_file(name)
    path.unlink()
    return {"deleted": path.name}


def job_dir(job: str) -> Path:
    """The directory of job `job`; Refused when there is none."""
    d = JOBS_DIR / safe_name(job, "job")
    if not (d / "job.json").is_file():
        raise Refused("no job {}".format(job))
    return d


def job_state(d: Path) -> dict:
    """Job directory `d` as {"job", "lab", "started", "state", "rc"}; state is running, done or failed."""
    meta = json.loads((d / "job.json").read_text())
    rc_file = d / "rc"
    rc = int(rc_file.read_text().strip()) if rc_file.is_file() and rc_file.read_text().strip() else None
    if rc is None:
        alive = Path("/proc", str(meta.get("pid", 0))).exists()
        state = "running" if alive else "failed"
    else:
        state = "done" if rc == 0 else "failed"
    return dict(meta, job=d.name, state=state, rc=rc)


def lab_create(name: str, keep: bool) -> dict:
    """Start setup_lab.py on saved lab `name` as a background job; refuse while one runs for that lab."""
    lab = LABS_DIR / safe_name(name, "lab file")
    if lab.suffix != ".json" or not lab.is_file():
        raise Refused("no saved lab {}".format(name))
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    for d in JOBS_DIR.iterdir():
        if (d / "job.json").is_file():
            st = job_state(d)
            if st["lab"] == lab.name and st["state"] == "running":
                raise Refused("lab {} is already being created (job {})".format(lab.name, d.name))
    job = "{}-{}".format(lab.stem, time.strftime("%Y%m%d-%H%M%S"))
    d = JOBS_DIR / job
    d.mkdir(mode=0o700)
    cmd = [SETUP_LAB] + (["--keep"] if keep else []) + [str(lab)]
    with open(d / "log", "wb") as log:
        proc = subprocess.Popen(
            ["/bin/sh", "-c", '"$@"; echo $? > "$0"', str(d / "rc")] + cmd,
            stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
            cwd=str(JOBS_DIR), start_new_session=True, close_fds=True)
    (d / "job.json").write_text(json.dumps({"lab": lab.name, "started": int(time.time()), "pid": proc.pid,
                                            "keep": keep}))
    return {"job": job}


def lab_status(job: str) -> dict:
    """Job `job`'s state and the last LOG_TAIL_LINES lines of its log."""
    d = job_dir(job)
    lines = (d / "log").read_text(errors="replace").splitlines() if (d / "log").is_file() else []
    return dict(job_state(d), log="\n".join(lines[-LOG_TAIL_LINES:]))


def lab_jobs() -> dict:
    """Every job, newest first."""
    if not JOBS_DIR.is_dir():
        return {"jobs": []}
    jobs = [job_state(d) for d in JOBS_DIR.iterdir() if (d / "job.json").is_file()]
    return {"jobs": sorted(jobs, key=lambda j: j.get("started", 0), reverse=True)}


def run(args: list) -> dict:
    """Carry out command line `args`; returns the JSON answer."""
    cmd, rest = (args[0], args[1:]) if args else ("", [])
    if cmd == "credentials-list" and not rest:
        return credentials_list()
    if cmd == "credentials-add" and not rest:
        return credentials_add(read_stdin())
    if cmd == "credentials-encrypt" and len(rest) == 1:
        return credentials_encrypt(rest[0], read_stdin())
    if cmd == "credentials-delete" and len(rest) == 1:
        return credentials_delete(rest[0])
    if cmd == "lab-create" and rest and rest[1:] in ([], ["--keep"]):
        return lab_create(rest[0], rest[1:] == ["--keep"])
    if cmd == "lab-status" and len(rest) == 1:
        return lab_status(rest[0])
    if cmd == "lab-jobs" and not rest:
        return lab_jobs()
    raise Refused("usage: lab-builder-helper {credentials-list|credentials-add|credentials-encrypt FILE|"
                  "credentials-delete FILE|lab-create FILE [--keep]|lab-status JOB|lab-jobs}")


def main() -> None:
    if sys.argv[1:] in (["--help"], ["-h"]):
        print(__doc__)
        return
    if sys.argv[1:] in (["--version"], ["-v"]):
        print("lab-builder-helper {}".format(__version__))
        return
    try:
        print(json.dumps(run(sys.argv[1:])))
    except (Refused, ValueError, OSError) as e:
        print(json.dumps({"error": str(e)}))
        sys.exit(1)


if __name__ == "__main__":
    main()
