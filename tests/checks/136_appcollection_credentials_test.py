#!/usr/bin/env python3.11
# Every add-on that installs from the SUSE Application Collection resolves its account the same way, and that way
# actually works against a credential file — see 136_appcollection_credentials.sh.
import importlib.util
import sys
import tempfile
from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest import mock

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "libs"))

import addon_common as ac  # noqa: E402
import appco_common as appco  # noqa: E402
import primary  # noqa: E402

failures = []


def check(desc, cond):
    if not cond:
        failures.append(desc)
        print("FAIL:", desc)


def _load(path):
    loader = SourceFileLoader(path.name, str(path))
    module = importlib.util.module_from_spec(importlib.util.spec_from_loader(path.name, loader))
    loader.exec_module(module)
    return module


_schema = _load(_REPO / "scripts" / "lab_schema")


def credentials_in(directory, cfg, prefix):
    """appco.credentials() with `directory` as the only credential store there is."""
    with mock.patch.object(primary, "_DEFAULT_CREDENTIALS_DIRS", [str(directory)]):
        return appco.credentials(cfg, prefix)


# ── the contract resolve_credential() is called under ────────────────────────
# This is the root of the bug these checks exist for: the field map's key is looked up in the credential file and
# its value in the lab definition, so a map written the other way round resolves to "" from both.

store = Path(tempfile.mkdtemp())
(store / "appcollection-lab.yaml").write_text(
    "credential_kind: appcollection\nappcollection_user: file-user\nappcollection_password: file-token\n")

with mock.patch.object(primary, "_DEFAULT_CREDENTIALS_DIRS", [str(store)]):
    right = ac.resolve_credential({}, "appcollection", {"appcollection_user": "x_appcollection_user"})
    wrong = ac.resolve_credential({"x_appcollection_user": "inline"}, "appcollection",
                                  {"x_appcollection_user": "appcollection_user"})
check("resolve_credential reads the credential file by the file's own field name",
      right["appcollection_user"] == "file-user")
check("a field map written the other way round resolves to nothing, from the file and from the lab definition "
      "alike — which is why no add-on builds one by hand any more",
      wrong["x_appcollection_user"] == "")

# ── every add-on that offers the Collection ──────────────────────────────────

addons = sorted(p for p in (_REPO / "scripts").glob("install_*")
                if "appco.credentials(" in p.read_text(errors="replace"))
check("the add-ons that install from the Collection were found", len(addons) >= 10)

for path in addons:
    name = path.name
    module = _load(path)
    prefix = getattr(module, "APPCOLLECTION_PREFIX", None)
    check("{}: names the prefix of its own Collection fields as APPCOLLECTION_PREFIX".format(name),
          isinstance(prefix, str) and prefix != "")
    if not prefix:
        continue

    documented = set(f["name"] for f in _schema.parse_script(str(path))["fields"])
    for suffix in ("user", "password", "account"):
        field = "{}_appcollection_{}".format(prefix, suffix)
        check("{}: documents {}, which is the field its prefix resolves".format(name, field),
              field in documented)

    user, password = credentials_in(store, {}, prefix)
    check("{}: a single credential file in the store is used on its own".format(name),
          (user, password) == ("file-user", "file-token"))

    named = {"{}_appcollection_account".format(prefix): "appcollection-lab"}
    check("{}: the account the lab names is read".format(name),
          credentials_in(store, named, prefix) == ("file-user", "file-token"))

    empty = Path(tempfile.mkdtemp())
    inline = {"{}_appcollection_user".format(prefix): "inline-user",
              "{}_appcollection_password".format(prefix): "inline-token"}
    check("{}: its own fields are used when there is no credential file".format(name),
          credentials_in(empty, inline, prefix) == ("inline-user", "inline-token"))
    check("{}: nothing set anywhere gives two empty strings, not an exception".format(name),
          credentials_in(empty, {}, prefix) == ("", ""))

    file_wins = dict(inline)
    check("{}: the credential file wins over a field left in the lab definition".format(name),
          credentials_in(store, file_wins, prefix) == ("file-user", "file-token"))

check("no add-on builds an \"appcollection\" field map of its own any more",
      not [p.name for p in (_REPO / "scripts").glob("install_*")
           if '"appcollection", {' in p.read_text(errors="replace")
           and "appco.credentials(" not in p.read_text(errors="replace")
           and p.name not in ("install_rancher", "install_appcollection", "install_suse_ai")])

if failures:
    print("{} check(s) failed".format(len(failures)))
    sys.exit(1)
print("all Application Collection credential checks passed")
