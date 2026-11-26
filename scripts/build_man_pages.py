#!/usr/bin/env python3
"""
build_man_pages.py — generate the man pages of lab-in-a-box's own commands from their --help output.

Every command this project installs already prints complete help for --help or -h in any argument position, with no
configuration present (tests/checks/90_help_anywhere.sh holds it to that). That help text is therefore the one
description of a command that cannot go stale, so the man pages are generated from it rather than written a second
time beside it: there is nothing to keep in sync.

Usage:
    build_man_pages.py [--output-dir <dir>] [--source-dir <dir>|--installed] [--section <n>] [<command> ...]
        Write one page per command. Without <command>, every command this project installs.
    build_man_pages.py --list
        Print the commands that would get a page, one "<section> <command>" per line, and exit.
    build_man_pages.py --help
        Print this help and exit.

Options:
    --output-dir <dir>
            Where the man<n>/ directories are written (default /usr/local/share/man). Each page lands at
            <dir>/man<n>/<command>.<n>, the path `man` reads when MANPATH includes <dir>.
    --source-dir <dir>
            Where to find the commands to ask for help (default: this repository's own scripts/ directory).
    --installed
            Ask each command where it is installed rather than in one directory: /usr/local/bin for the section-1
            commands, /usr/local/sbin for the section-8 ones. The pages then describe the commands as actually
            deployed, version line included, which is how install_automation_node_scripts.sh calls this.
    --section <n>
            Write every page in section <n> instead of each command's own (1 for the commands on PATH, 8 for the
            two administrative ones under sbin). Mainly for inspecting the output somewhere harmless.
    --dry-run
            Say what would be written without writing it.

A command whose help cannot be read is reported on stderr and skipped, and the exit code is 1; the other pages are
still written, since one broken command must not leave a node with no documentation at all.
"""
__version__ = "__LABVERSION__"

import os
import re
import subprocess
import sys
import time
from pathlib import Path

for _candidate in ("/usr/local/lib/lab_creation", str(Path(__file__).resolve().parent.parent / "libs")):
    if Path(_candidate).is_dir() and _candidate not in sys.path:
        sys.path.insert(0, _candidate)

import primary  # noqa: E402

# The commands this project installs, and the manual section each belongs in: section 1 for the ones on PATH that an
# operator runs, section 8 for the two administrative ones installed under sbin and run as root. The list is held to
# install_automation_node_scripts.sh's own by tests/checks/127_man_pages.sh, so a command added there without a page
# fails the suite rather than shipping undocumented.
COMMANDS = {
    "setup_lab.py": 1,
    "setup_vm.py": 1,
    "destroy_lab.py": 1,
    "destroy_vm.py": 1,
    "vm_power.py": 1,
    "check_lab.py": 1,
    "check_addon.py": 1,
    "migrate_config.py": 1,
    "overlay_ctl.py": 1,
    "dns_sync.py": 1,
    "reconcile_lab.py": 1,
    "lab_watch.py": 1,
    "setup_credentials.py": 1,
    "build_lab_usb.py": 1,
    "refresh_hypervisor_status.py": 1,
    "setup_harvester_cluster.py": 1,
    "lab_schema": 1,
    "pushDockerImage.sh": 1,
    "build_man_pages.py": 1,
    "upgrade_lab_in_a_box": 1,
    "lab-builder-helper": 8,
    "lab-builder-passwd": 8,
}

# A command whose source file is named differently from the command itself (install_automation_node_scripts.sh
# renames the helper as it deploys it); the page is named after the command, built from this file. It applies to a
# repository checkout: in an installed tree the file already carries the command's own name, which _help_text()
# falls back to.
SOURCE_NAMES = {"lab-builder-helper": "lab_builder_helper.py"}

DEFAULT_OUTPUT_DIR = Path("/usr/local/share/man")
HELP_TIMEOUT_SECONDS = 60

# Where install_automation_node_scripts.sh puts a command of each section, which is what --installed reads so that
# one call covers both: the section-1 commands an operator runs, and the section-8 ones only root does.
INSTALL_DIRS = {1: Path("/usr/local/bin"), 8: Path("/usr/local/sbin")}

# A line that starts a section of the help text: "Usage:", "Options:", "Examples:" and the like, alone on its line.
_HEADING = re.compile(r"^([A-Z][A-Za-z /-]{2,40}):\s*$")

# The heading each help section becomes in the page. Anything else keeps its own words, upper-cased, so a command
# with a section of its own is not silently dropped.
_HEADING_MAP = {
    "usage": "SYNOPSIS",
    "options": "OPTIONS",
    "option": "OPTIONS",
    "examples": "EXAMPLES",
    "example": "EXAMPLES",
    "actions": "ACTIONS",
    "arguments": "ARGUMENTS",
    "environment": "ENVIRONMENT",
    "files": "FILES",
    "notes": "NOTES",
}


def command_help(command: str, source_dir: Path) -> str:
    """
    `command`'s own --help output, read by running it.

    Runs the file in `source_dir`, so the page describes the command as deployed there. Raises RuntimeError when the
    command cannot be run or does not print help, which the caller reports and skips: a page built from an error
    message would be worse than no page.
    """
    path = source_dir / SOURCE_NAMES.get(command, command)
    if not path.is_file():
        # An installed tree holds it under the command's own name; only a checkout uses the source name.
        path = source_dir / command
    if not path.is_file():
        raise RuntimeError("{} does not exist".format(path))
    argv = [str(path), "--help"]
    if path.suffix == ".py" or _is_python(path):
        argv = [sys.executable, str(path), "--help"]
    elif path.suffix == ".sh" or _is_shell(path):
        argv = ["bash", str(path), "--help"]
    try:
        done = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              timeout=HELP_TIMEOUT_SECONDS)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise RuntimeError("running '{} --help' failed: {}".format(path, e))
    text = done.stdout.decode("utf-8", "replace").strip()
    if not text:
        text = done.stderr.decode("utf-8", "replace").strip()
    if not text:
        raise RuntimeError("'{} --help' printed nothing (exit {})".format(path, done.returncode))
    return text


def _shebang(path: Path) -> str:
    try:
        with path.open("rb") as fh:
            return fh.readline(256).decode("utf-8", "replace")
    except OSError:
        return ""


def _is_python(path: Path) -> bool:
    return "python" in _shebang(path)


def _is_shell(path: Path) -> bool:
    return "sh" in _shebang(path)


SUMMARY_MAX_CHARS = 100


def summary_of(help_text: str, command: str) -> str:
    """
    The one-line description for the page's NAME section.

    Built from the help text's whole opening paragraph, not its first line: a help text wraps its opening sentence
    over several lines, and taking one of them leaves NAME holding half a sentence. The "<command> — " the paragraph
    usually opens with is dropped, since NAME supplies the name itself, and the result is cut at the first sentence
    end, or at a word boundary within SUMMARY_MAX_CHARS, so it stays the single line man expects.
    """
    stem = command.split(".")[0]
    # Both spellings a help text introduces itself by: the file name it is invoked as, and that name without its
    # extension. "mycmd.py USER" opens with the first, "mycmd — does things" with either.
    names = tuple(sorted({command.lower() + " ", stem.lower() + " "}))
    text = ""
    for paragraph in _paragraphs(help_text):
        joined = " ".join(paragraph)
        # A paragraph that is the usage block, or a bare invocation line, describes the syntax rather than the
        # command: SYNOPSIS already carries it, and in NAME it would make `apropos` useless. Keep looking.
        # "<command> — what it does" opens with the command's name too, and is exactly what NAME wants, so a
        # summary separator in that first line tells the two apart.
        invocation = (joined.lower().startswith(names)
                      and not any(sep in paragraph[0] for sep in ("—", " -- ", " - ", ": ")))
        if _HEADING.match(paragraph[0]) or joined.lower().startswith("usage:") or invocation:
            continue
        text = joined
        break
    text = text or " ".join(next(iter(_paragraphs(help_text)), [command]))

    # "<command> — summary" and "<command>: summary", the two ways these help texts introduce themselves. NAME
    # supplies the name itself, so the prefix is dropped rather than printed twice.
    for separator in ("—", " -- ", " - ", ": "):
        head, _, tail = text.partition(separator)
        if tail and head.strip().rstrip(":").startswith(stem):
            text = tail.strip()
            break

    sentence, _, _ = text.partition(". ")
    text = (sentence.strip() or text).rstrip(".").strip()
    if len(text) > SUMMARY_MAX_CHARS:
        # Cut at the last clause boundary that fits, so the line ends on a complete thought rather than mid-phrase;
        # on a sentence with no comma in reach, at a word boundary. The marker is appended last: stripping the
        # trailing full stop first is what keeps it from being stripped off again.
        head = text[:SUMMARY_MAX_CHARS]
        text = (head.rsplit(",", 1)[0] if "," in head else head.rsplit(" ", 1)[0]).rstrip(" ,") + "..."
    return text or command


def _paragraphs(help_text: str) -> list:
    """`help_text`'s blank-line-separated paragraphs, each a list of stripped lines."""
    out, current = [], []
    for line in help_text.splitlines():
        if line.strip():
            current.append(line.strip())
        elif current:
            out.append(current)
            current = []
    if current:
        out.append(current)
    return out


def roff_escape(line: str) -> str:
    r"""
    `line` with the characters roff reads as markup made literal: a backslash, and a leading "." or "'", which
    would otherwise start a request and silently swallow the line.
    """
    out = line.replace("\\", "\\e")
    return "\\&" + out if out[:1] in (".", "'") else out


def sections_of(help_text: str, command: str) -> list:
    """
    [(heading, [line, ...])] for `help_text`: its own "Usage:"/"Options:" blocks as man sections, with everything
    before the first one as DESCRIPTION.

    The body lines are kept exactly as the command prints them, because a command's help aligns its options in
    columns that carry meaning; the caller renders them unfilled so that alignment survives.
    """
    sections, heading, body = [], "DESCRIPTION", []
    for line in help_text.splitlines():
        match = _HEADING.match(line)
        if match:
            if any(ln.strip() for ln in body):
                sections.append((heading, _trim(body)))
            name = match.group(1).strip()
            heading, body = _HEADING_MAP.get(name.lower(), name.upper()), []
            continue
        body.append(line)
    if any(ln.strip() for ln in body):
        sections.append((heading, _trim(body)))
    if not sections:
        sections = [("DESCRIPTION", [command])]
    # The opening paragraph stays in DESCRIPTION even though NAME is built from it: a man page repeats its summary
    # there, and removing the lines NAME drew from would leave DESCRIPTION starting mid-sentence.
    return sections


def _trim(lines: list) -> list:
    """`lines` without its leading and trailing blank lines."""
    out = list(lines)
    while out and not out[0].strip():
        out.pop(0)
    while out and not out[-1].strip():
        out.pop()
    return out


def render(command: str, section: int, help_text: str, date: str, version: str) -> str:
    """The complete roff source of `command`'s page, in manual `section`."""
    out = [".\\\" Generated by build_man_pages.py from \"{} --help\" — do not edit.".format(command),
           ".TH {} {} \"{}\" \"lab-in-a-box {}\" \"lab-in-a-box\"".format(
               command.upper().replace("-", "\\-"), section, date, version),
           ".SH NAME",
           "{} \\- {}".format(command.replace("-", "\\-"), roff_escape(summary_of(help_text, command)))]
    for heading, body in sections_of(help_text, command):
        out.append(".SH {}".format(heading))
        out.append(".nf")
        out.extend(roff_escape(line) for line in body)
        out.append(".fi")
    out.append(".SH SEE ALSO")
    siblings = [c for c in sorted(COMMANDS) if c != command]
    out.append(".nf")
    out.append(", ".join("{}({})".format(c.replace("-", "\\-"), COMMANDS[c]) for c in siblings))
    out.append("")
    out.append("The lab-in-a-box documentation: https://suse-technical-marketing.github.io/lab-in-a-box/")
    out.append(".fi")
    return "\n".join(out) + "\n"


def lab_version() -> str:
    """
    The version the pages are stamped with.

    This file's own __version__ once installed, since install_automation_node_scripts.sh rewrites __LABVERSION__ in
    every deployed script. Running from a checkout it is still the placeholder, so fall back to the checkout's own
    git description, and to "unknown" where there is no git either.
    """
    if "LABVERSION" not in __version__:
        return __version__
    try:
        done = subprocess.run(["git", "-C", str(Path(__file__).resolve().parent.parent),
                               "describe", "--tags", "--always", "--dirty"],
                              stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=10)
        described = done.stdout.decode("utf-8", "replace").strip()
        if done.returncode == 0 and described:
            return described
    except (OSError, subprocess.TimeoutExpired):
        pass
    return os.environ.get("LAB_VERSION", "") or "unknown"


def build(commands, source_dir, output_dir: Path, section=None, dry_run=False) -> list:
    """
    Write a page per command. Returns the errors, one string each: an empty list means every page was written.

    `source_dir` is one directory for every command, or None to take each command's own installed directory from
    INSTALL_DIRS (--installed), which is what the installer passes: the two administrative commands live under sbin
    rather than beside the others.

    A command that cannot be asked for help is reported and skipped rather than ending the run, so one broken
    command does not leave the node with no pages at all.
    """
    errors = []
    date = time.strftime("%Y-%m-%d")
    version = lab_version()
    for command in commands:
        where = section or COMMANDS[command]
        try:
            help_text = command_help(command, source_dir or INSTALL_DIRS[COMMANDS[command]])
        except RuntimeError as e:
            errors.append("{}: {}".format(command, e))
            continue
        page = output_dir / "man{}".format(where) / "{}.{}".format(command, where)
        if dry_run:
            print("would write {} ({} lines of help)".format(page, len(help_text.splitlines())))
            continue
        try:
            page.parent.mkdir(parents=True, exist_ok=True)
            page.write_text(render(command, where, help_text, date, version), encoding="utf-8")
        except OSError as e:
            errors.append("{}: cannot write {}: {}".format(command, page, e))
            continue
        print("wrote {}".format(page))
    return errors


def main():
    args = sys.argv[1:]
    if primary.wants_help(args):
        print(__doc__.strip())
        sys.exit(0)
    if args and args[0] in ("--version", "-v"):
        print("{} {}".format(Path(sys.argv[0]).name, __version__))
        sys.exit(0)
    if "--list" in args:
        for command in sorted(COMMANDS):
            print("{} {}".format(COMMANDS[command], command))
        sys.exit(0)

    output_dir, source_dir, section = DEFAULT_OUTPUT_DIR, Path(__file__).resolve().parent, None
    dry_run = "--dry-run" in args
    installed = "--installed" in args
    args = [a for a in args if a not in ("--dry-run", "--installed")]
    rest = []
    i = 0
    while i < len(args):
        arg = args[i]
        if arg in ("--output-dir", "--source-dir", "--section"):
            if i + 1 >= len(args):
                print("{} needs a value".format(arg), file=sys.stderr)
                sys.exit(2)
            value = args[i + 1]
            i += 2
            if arg == "--output-dir":
                output_dir = Path(value)
            elif arg == "--source-dir":
                source_dir = Path(value)
            else:
                if not value.isdigit():
                    print("--section takes a number, got '{}'".format(value), file=sys.stderr)
                    sys.exit(2)
                section = int(value)
            continue
        if arg.startswith("-"):
            print("unknown option '{}' — see --help".format(arg), file=sys.stderr)
            sys.exit(2)
        rest.append(arg)
        i += 1

    unknown = [c for c in rest if c not in COMMANDS]
    if unknown:
        print("not a lab-in-a-box command: {} — --list names them".format(", ".join(unknown)), file=sys.stderr)
        sys.exit(2)

    errors = build(rest or sorted(COMMANDS), None if installed else source_dir, output_dir,
                   section=section, dry_run=dry_run)
    for error in errors:
        print("man page not written — {}".format(error), file=sys.stderr)
    sys.exit(1 if errors else 0)


if __name__ == "__main__":
    main()
