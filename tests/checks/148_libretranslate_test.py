#!/usr/bin/env python3
# Unit tests for scripts/install_libretranslate — mocked ssh_run/ssh_output, no real SSH, no container and no
# model download. What is asserted is the language selection (individually, or "all"), the LT_* environment it
# produces, and that a code upstream has no model for is refused before anything is downloaded. Run from
# 148_libretranslate.sh, in its own container — see tests/run_tests.sh.
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "libs"))
sys.path.insert(0, str(_REPO / "scripts"))

import addon_common as ac  # noqa: E402
import apps  # noqa: E402
import podman_service as ps  # noqa: E402

lt = apps.load_addon_module("install_libretranslate", str(_REPO / "scripts"))  # no .py: loaded by path

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


for module in (ps, lt):
    module.ssh_run = _fake_ssh_run
    module.ssh_output = _fake_ssh_output
    module.log = lambda m: logs.append(m)
    module.warn = lambda m: logs.append(m)
ps.ensure_podman = lambda host: True


def reset():
    del calls[:]
    del logs[:]
    returncodes.clear()
    outputs.clear()


def sent():
    return "\n".join(c[0] for c in calls)


# ── which languages ──────────────────────────────────────────────────────
check("every language upstream publishes a model for is listed, so a lab's code can be checked",
      len(lt.LANGUAGES) == 49 and lt.LANGUAGES["zt"] == "Chinese (traditional)")
check("a lab that names none gets English and Spanish, which is a working pair and a small download",
      lt.languages_of({}) == ["en", "es"])
check("English is always loaded, because LibreTranslate pivots through it",
      lt.languages_of({"libretranslate_languages": ["de"]}) == ["de", "en"])
check("the codes come out in a fixed order, whatever order the lab wrote them in",
      lt.languages_of({"libretranslate_languages": ["fr", "de"]})
      == lt.languages_of({"libretranslate_languages": ["de", "fr"]}) == ["de", "en", "fr"])
check("\"all\" loads everything, which LibreTranslate does by needing no restriction at all",
      lt.wants_all({"libretranslate_languages": "all"}) and lt.languages_of(
          {"libretranslate_languages": "all"}) == [])
check("\"all\" is recognised inside a list too, and case does not matter",
      lt.wants_all({"libretranslate_languages": ["de", "All"]}))
check("a comma-separated string is read as the list it obviously is",
      lt.languages_of({"libretranslate_languages": "de, fr"}) == ["de", "en", "fr"])
check("a code upstream has no model for is reported rather than downloaded",
      lt.unknown_languages({"libretranslate_languages": ["de", "elvish"]}) == ["elvish"])


# ── the container's environment ──────────────────────────────────────────
env = lt.environment({})
check("it is told to listen on every address inside the container, or the published port answers nothing",
      env["LT_HOST"] == "0.0.0.0")
check("the port it serves and the port that is published are the same", env["LT_PORT"] == "5000")
check("a narrowed selection becomes LT_LOAD_ONLY, in one comma-separated value",
      lt.environment({"libretranslate_languages": ["de", "fr"]})["LT_LOAD_ONLY"] == "de,en,fr")
check("\"all\" passes no restriction at all, which is upstream's own default",
      "LT_LOAD_ONLY" not in lt.environment({"libretranslate_languages": "all"}))
check("nothing is limited and nothing is keyed unless the lab asked",
      "LT_API_KEYS" not in env and "LT_REQ_LIMIT" not in env and "LT_CHAR_LIMIT" not in env
      and "LT_UPDATE_MODELS" not in env)

env = lt.environment({"libretranslate_api_keys": "true", "libretranslate_req_limit": "60",
                      "libretranslate_char_limit": "5000", "libretranslate_update_models": "true",
                      "libretranslate_disable_web_ui": "true", "libretranslate_threads": "8"})
check("every limit the lab set is passed through under its own LT name",
      env["LT_API_KEYS"] == "true" and env["LT_REQ_LIMIT"] == "60" and env["LT_CHAR_LIMIT"] == "5000"
      and env["LT_UPDATE_MODELS"] == "true" and env["LT_DISABLE_WEB_UI"] == "true"
      and env["LT_THREADS"] == "8")
check("a request limit of zero is passed, not read as unset: with api_keys it is what makes a key mandatory",
      lt.environment({"libretranslate_req_limit": "0"})["LT_REQ_LIMIT"] == "0")


# ── the container ────────────────────────────────────────────────────────
reset()
lt.setup_libretranslate("n1.mydemo.lab", {})
check("the image is upstream's own", "podman pull libretranslate/libretranslate:latest" in sent())
check("the port is published on every address", "-p 0.0.0.0:5000:5000" in sent())
check("the models live in a directory on the node, writable by the image's own user",
      "-v /var/lib/libretranslate:/home/libretranslate/.local" in sent()
      and "mkdir -p /var/lib/libretranslate && chmod 0777 /var/lib/libretranslate" in sent())
check("it waits on /languages, which only answers once the models are loaded",
      "/languages" in sent() and "seq 1 900" in sent())
check("the log names the languages it is loading", any("English" in m and "Spanish" in m for m in logs))

reset()
lt.setup_libretranslate("n1.mydemo.lab", {"libretranslate_languages": "all"})
check("a lab asking for every language is told it is a large download",
      any("gigabytes" in m for m in logs))

reset()
returncodes["curl -fsS"] = 1
lt.setup_libretranslate("n1.mydemo.lab", {})
check("a service still downloading is reported with the command to watch it, and does not fail the lab",
      any("podman logs" in m for m in logs))


# ── --validate ───────────────────────────────────────────────────────────
def errors_for(section):
    v = ac.Validator({"libretranslate": section})
    lt._validate(v)
    return "\n".join(v.errors)


check("a plain lab passes", errors_for({}) == "")
check("\"all\" is accepted", errors_for({"libretranslate_languages": "all"}) == "")
check("a language it has no model for is refused, listing the ones it has",
      "is not a language" in errors_for({"libretranslate_languages": ["elvish"]}))
check("the languages written as something else are refused",
      "must be a list" in errors_for({"libretranslate_languages": {"a": 1}}))
check("a port that is not one is refused", "must be a port" in errors_for({"libretranslate_port": "http"}))
check("a limit that is not a number is refused",
      "must be a number" in errors_for({"libretranslate_req_limit": "many"}))
check("the booleans are checked", "must be 'true' or 'false'"
      in errors_for({"libretranslate_api_keys": "yes"}))


# ── --check ──────────────────────────────────────────────────────────────
reset()
chk = ac.Checker({"nodes": {}}, vm_name="n1.mydemo.lab")
lt._check(chk)
check("a lab with no libretranslate section is not a failure", not chk.problems and not calls)

reset()
outputs["podman inspect"] = "libretranslate/libretranslate:latest"
outputs["/languages"] = '[{"code":"en","name":"English"},{"code":"es","name":"Spanish"}]'
chk = ac.Checker({"libretranslate": {}}, vm_name="n1.mydemo.lab")
lt._check(chk)
check("a running container whose /languages holds what the lab asked for passes", not chk.problems)

reset()
outputs["podman inspect"] = "libretranslate/libretranslate:latest"
outputs["/languages"] = '[{"code":"en","name":"English"}]'
chk = ac.Checker({"libretranslate": {"libretranslate_languages": ["de"]}}, vm_name="n1.mydemo.lab")
lt._check(chk)
check("a language the lab asked for that is not loaded is a difference, naming it",
      chk.problems and "de" in chk.problems[0])

reset()
returncodes["podman ps"] = 1
chk = ac.Checker({"libretranslate": {}}, vm_name="n1.mydemo.lab")
lt._check(chk)
check("a container that is not running is reported, and nothing else is asked",
      chk.problems and "/languages" not in sent())


# ── the plugin the registry reads ───────────────────────────────────────
check("it is a container on a node", lt.PLUGIN["layers"] == ["standalone-container"])
check("it implements --check", lt.PLUGIN["check"] is True)
check("it is classified as AI tooling", apps.tags_for("libretranslate") == ["ai", "apps"])

if failures:
    print("{} check(s) failed".format(len(failures)))
    sys.exit(1)
print("all libretranslate checks passed")
