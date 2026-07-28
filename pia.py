#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pia - a tiny AI coding agent for the Pocket C.H.I.P (and other small ARM boxes).

Design goals:
  * Pure Python 3 standard library. No pip, no build step, no dependencies.
  * Single self-contained file: copy it to the device and run.
  * Works with any OpenAI-compatible chat API (OpenCode Zen, Kimi/Moonshot,
    OpenAI, local llama.cpp servers, ...).
  * Frugal with RAM and screen space (built for 512 MB / 480x272 displays).

Requires Python 3.6+ (uses f-strings). Check with:  python3 --version
"""

import argparse
import difflib
import fnmatch
import glob
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import textwrap
import threading
import time
import urllib.error
import urllib.request

APP_NAME = "pia"
VERSION = "0.6.4"

# path of the currently running script (the installed binary, or pia.py in-repo)
try:
    SELF_PATH = os.path.realpath(__file__)
except NameError:  # pragma: no cover - frozen/interactive
    SELF_PATH = os.path.realpath(sys.argv[0])

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
# Config is merged from, in increasing priority:
#   1. built-in defaults (below)
#   2. ~/.config/chip-ai/config.json
#   3. environment variables (CHIP_*, plus provider keys)
#   4. command-line flags
#
# A config file looks like:
# {
#   "provider": "opencode",
#   "providers": {
#     "opencode": {
#       "base_url": "https://opencode.ai/zen/v1",
#       "api_key_env": "OPENCODE_ZEN_API_KEY",
#       "model": "big-pickle"
#     },
#     "kimi": {
#       "base_url": "https://api.moonshot.ai/v1",
#       "api_key_env": "MOONSHOT_API_KEY",
#       "model": "kimi-k2.5"
#     }
#   },
#   "auto_approve": false,
#   "stream": true
# }

BUILTIN_PROVIDERS = {
    "opencode": {
        "base_url": "https://opencode.ai/zen/v1",
        "api_key_env": "OPENCODE_ZEN_API_KEY",
        "model": "big-pickle",
    },
    # The Go plan is a different tier on a different path, with its own key.
    # Model left blank on purpose: run /model rather than trust a guess.
    "opencode-go": {
        "base_url": "https://opencode.ai/zen/go/v1",
        "api_key_env": "OPENCODE_GO_API_KEY",
        "model": "",
    },
    "kimi": {
        "base_url": "https://api.moonshot.ai/v1",
        "api_key_env": "MOONSHOT_API_KEY",
        "model": "kimi-k2.5",
    },
    "openai": {
        "base_url": "https://api.openai.com/v1",
        "api_key_env": "OPENAI_API_KEY",
        "model": "gpt-4o-mini",
    },
    "local": {
        "base_url": "http://127.0.0.1:8080/v1",
        "api_key_env": "LOCAL_API_KEY",
        "model": "local-model",
    },
}

DEFAULT_CONFIG = {
    "provider": "opencode",
    "providers": BUILTIN_PROVIDERS,
    "auto_approve": False,
    "stream": True,
    "max_steps": 25,
    "request_timeout": 180,
    "max_history_messages": 40,  # backstop; the token budget below does the real work
    # tokens this model accepts. null = guess from the model name, falling back
    # to a conservative default. Set it if you know the real figure.
    "context_limit": None,
    "auto_compact": False,  # summarise automatically instead of just warning
    # generous by default: builds and installs are slow on a 1 GHz ARM core
    "bash_timeout": 300,
    "update": {
        # repo_dir is filled in by install.sh (path of your local git clone).
        "enabled": True,
        "repo_dir": "",
        "timeout": 12,
    },
}


def config_path():
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, APP_NAME, "config.json")


def load_config():
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))  # deep copy
    path = config_path()
    if os.path.exists(path):
        try:
            with open(path, "r") as f:
                user = json.load(f)
            # merge providers map instead of replacing it wholesale
            providers = dict(cfg["providers"])
            providers.update(user.get("providers", {}))
            cfg.update(user)
            cfg["providers"] = providers
        except Exception as e:
            eprint(f"warning: could not read {path}: {e}")

    # environment overrides
    if os.environ.get("PIA_PROVIDER"):
        cfg["provider"] = os.environ["PIA_PROVIDER"]
    if os.environ.get("PIA_MODEL"):
        prov = cfg["providers"].setdefault(cfg["provider"], {})
        prov["model"] = os.environ["PIA_MODEL"]
    if os.environ.get("PIA_BASE_URL"):
        prov = cfg["providers"].setdefault(cfg["provider"], {})
        prov["base_url"] = os.environ["PIA_BASE_URL"]
    return cfg


def save_key(provider_name, key, make_default=True):
    """Persist an API key for a provider into the user config file (chmod 600).

    make_default: also switch to this provider. On by default: setting a key
    for a provider is a strong signal you mean to use it, and forgetting this
    is exactly how you end up silently billed on the wrong one (opencode's
    Zen and Go are separate balances on separate endpoints).
    """
    path = config_path()
    d = os.path.dirname(path)
    if d and not os.path.isdir(d):
        os.makedirs(d)
    data = {}
    if os.path.exists(path):
        try:
            with open(path, "r") as f:
                data = json.load(f)
        except Exception:
            data = {}
    data.setdefault("providers", {})
    # start from the built-in provider defaults if this one is new
    if provider_name not in data["providers"]:
        data["providers"][provider_name] = dict(
            BUILTIN_PROVIDERS.get(provider_name, {})
        )
    data["providers"][provider_name]["api_key"] = key
    if make_default:
        data["provider"] = provider_name
    else:
        data.setdefault("provider", provider_name)
    with open(path, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")
    try:
        os.chmod(path, 0o600)  # keep the key readable only by the owner
    except Exception:
        pass
    return path


# ---------------------------------------------------------------------------
# Session persistence (survives crashes, SSH drops, low battery shutdowns...)
# ---------------------------------------------------------------------------
def sessions_dir():
    base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    d = os.path.join(base, APP_NAME, "sessions")
    os.makedirs(d, exist_ok=True)
    return d


def save_session(messages, name="last"):
    path = os.path.join(sessions_dir(), name + ".json")
    with open(path, "w") as f:
        json.dump(messages, f, ensure_ascii=False)
    return path


def load_session(name="last"):
    path = os.path.join(sessions_dir(), name + ".json")
    with open(path, "r") as f:
        return json.load(f)


def list_sessions():
    d = sessions_dir()
    return sorted(f[:-5] for f in os.listdir(d) if f.endswith(".json"))


# ---------------------------------------------------------------------------
# Battery (Pocket C.H.I.P specific touch; silently absent on other machines)
# ---------------------------------------------------------------------------
def battery_status():
    base = "/sys/class/power_supply"
    try:
        names = os.listdir(base)
    except Exception:
        return None
    for name in names:
        cap_path = os.path.join(base, name, "capacity")
        if not os.path.exists(cap_path):
            continue
        try:
            with open(cap_path) as f:
                cap = f.read().strip()
            status = ""
            status_path = os.path.join(base, name, "status")
            if os.path.exists(status_path):
                with open(status_path) as f:
                    status = f.read().strip().lower()
            return cap, status
        except Exception:
            return None
    return None


# ---------------------------------------------------------------------------
# Self-update (git-based, works with private repos via your git credentials)
# ---------------------------------------------------------------------------
def _run_git(repo_dir, git_args, timeout=12):
    try:
        p = subprocess.run(
            ["git", "-C", repo_dir] + git_args,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )
        return (
            p.returncode,
            p.stdout.decode("utf-8", "replace").strip(),
            p.stderr.decode("utf-8", "replace").strip(),
        )
    except Exception as e:
        return 1, "", str(e)


def update_info(cfg):
    """Return (n_commits_behind, repo_dir, branch) if updates exist, else None.

    Stays silent (returns None) on any problem: disabled, no clone, no git,
    offline, detached head, etc. — startup must never break because of this.
    """
    upd = cfg.get("update") or {}
    if not upd.get("enabled", True):
        return None
    repo_dir = upd.get("repo_dir") or ""
    if not repo_dir or not os.path.isdir(os.path.join(repo_dir, ".git")):
        return None
    if not shutil.which("git"):
        return None
    timeout = int(upd.get("timeout", 12))
    rc, _, _ = _run_git(repo_dir, ["fetch", "--quiet"], timeout=timeout)
    if rc != 0:
        return None
    rc, branch, _ = _run_git(repo_dir, ["rev-parse", "--abbrev-ref", "HEAD"])
    if rc != 0 or not branch or branch == "HEAD":
        return None
    rc, count, _ = _run_git(repo_dir, ["rev-list", "--count", "HEAD..@{u}"])
    if rc != 0 or not count.isdigit() or int(count) <= 0:
        return None
    return int(count), repo_dir, branch


def apply_update(repo_dir):
    """git pull --ff-only, then copy the fresh pia.py over the running binary.

    Returns (ok, message).
    """
    rc, out, err = _run_git(repo_dir, ["pull", "--ff-only"], timeout=90)
    if rc != 0:
        return False, (err or out or "git pull a échoué")
    src = os.path.join(repo_dir, "pia.py")
    try:
        if os.path.exists(src) and os.path.realpath(src) != SELF_PATH:
            shutil.copyfile(src, SELF_PATH)
            os.chmod(SELF_PATH, 0o755)
    except Exception as e:
        return False, f"maj récupérée mais copie vers {SELF_PATH} impossible : {e}"
    return True, (out or "à jour")


def offer_update(cfg, restart_argv=None):
    """Check for updates and, if the user agrees, apply and (optionally) restart.

    restart_argv: if given, re-exec with these args after a successful update.
    Returns True if an update was applied.
    """
    info = update_info(cfg)
    if not info:
        return False
    n, repo_dir, branch = info
    plural = "s" if n > 1 else ""
    print(yellow(f"{n} mise{plural} à jour disponible{plural} sur « {branch} »."))
    if not cfg.get("auto_approve") and confirm_action("Mettre à jour maintenant ?") == "n":
        return False
    ok, msg = apply_update(repo_dir)
    if not ok:
        eprint_err("échec de la mise à jour : " + msg)
        return False
    print(green("mis à jour."))
    if restart_argv is not None:
        print(dim("redémarrage…"))
        os.execv(sys.executable, [sys.executable, SELF_PATH] + restart_argv)
    return True


def resolve_provider(cfg, provider_name=None):
    name = provider_name or cfg.get("provider", "opencode")
    prov = cfg.get("providers", {}).get(name)
    if not prov:
        die(f"unknown provider '{name}'. Known: {', '.join(cfg.get('providers', {}))}")
    prov = dict(prov)
    prov["name"] = name
    # resolve the API key
    key = None
    if prov.get("api_key"):
        key = prov["api_key"]
    else:
        env_name = prov.get("api_key_env", "")
        key = os.environ.get(env_name) or os.environ.get("PIA_API_KEY")
    prov["api_key"] = key
    return prov


# ---------------------------------------------------------------------------
# Terminal helpers (small-screen friendly)
# ---------------------------------------------------------------------------
USE_COLOR = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None


def c(code, s):
    if not USE_COLOR:
        return s
    return f"\033[{code}m{s}\033[0m"


def dim(s):
    return c("2", s)


def bold(s):
    return c("1", s)


def cyan(s):
    return c("36", s)


def green(s):
    return c("32", s)


def yellow(s):
    return c("33", s)


def red(s):
    return c("31", s)


# Fallback size used only when detection fails. Deliberately small: the stock
# Pocket C.H.I.P screen is 480x272, which is roughly 50x16 characters. Guessing
# too wide garbles that screen; guessing too narrow is merely a bit cramped on
# a big one.
FALLBACK_SIZE = (50, 16)


def term_width():
    try:
        w = shutil.get_terminal_size(FALLBACK_SIZE).columns
    except Exception:
        w = FALLBACK_SIZE[0]
    return max(20, w)


def term_height():
    try:
        h = shutil.get_terminal_size(FALLBACK_SIZE).lines
    except Exception:
        h = FALLBACK_SIZE[1]
    return max(8, h)


def eprint(*a):
    print(*a, file=sys.stderr)


def die(msg, code=1):
    eprint(red("error: ") + msg)
    sys.exit(code)


def eprint_err(msg):
    """Print an error wrapped to the screen: server messages can be long."""
    eprint(red(wrap(str(msg))))


class StreamWrap:
    """Word-wraps text arriving token by token.

    textwrap needs the whole string up front, which streaming does not have.
    This tracks the current column and holds back the word being typed until a
    space or newline arrives, so words never get split across the right edge of
    the Pocket C.H.I.P's narrow screen.
    """

    def __init__(self, width=None):
        self.width = max(20, width or term_width())
        self.col = 0
        self.word = ""

    def _emit_word(self):
        if not self.word:
            return ""
        out = []
        w, self.word = self.word, ""
        # a single token longer than the line (URL, long path) gets hard-split
        while len(w) > self.width:
            if self.col > 0:
                out.append("\n")
                self.col = 0
            out.append(w[: self.width])
            w = w[self.width :]
            out.append("\n")
        if self.col + len(w) > self.width:
            out.append("\n")
            self.col = 0
        out.append(w)
        self.col += len(w)
        return "".join(out)

    def feed(self, text):
        out = []
        for ch in text:
            if ch == "\n":
                out.append(self._emit_word())
                out.append("\n")
                self.col = 0
            elif ch in " \t":
                out.append(self._emit_word())
                if 0 < self.col < self.width:
                    out.append(" ")
                    self.col += 1
            else:
                self.word += ch
        return "".join(out)

    def finish(self):
        return self._emit_word()


def wrap(text, indent=""):
    width = term_width()
    out = []
    for line in text.split("\n"):
        if not line:
            out.append("")
            continue
        out.extend(
            textwrap.wrap(
                line, width=width, initial_indent=indent, subsequent_indent=indent
            )
            or [indent]
        )
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Tools the model can call
# ---------------------------------------------------------------------------
MAX_TOOL_OUTPUT = 12000  # chars fed back to the model; keeps context + RAM small


def _truncate(s, limit=MAX_TOOL_OUTPUT):
    if len(s) <= limit:
        return s
    head = s[: limit - 200]
    return head + f"\n... [truncated, {len(s) - len(head)} more chars]"


def diff_budget():
    """How many diff lines fit while leaving the confirm prompt on screen.

    On the Pocket C.H.I.P (~16 rows) this is about 9 lines: enough to judge a
    small edit, never enough to scroll the "allow?" question out of sight.
    """
    return max(5, term_height() - 7)


def make_diff(old, new, path):
    """Return a small colored unified diff string, or None if old == new."""
    diff = list(
        difflib.unified_diff(
            old.splitlines(keepends=True),
            new.splitlines(keepends=True),
            fromfile=path,
            tofile=path,
            lineterm="",
            n=2,  # less context than the default 3: rows are scarce here
        )
    )
    if not diff:
        return None
    # drop the ---/+++ header pair: the path is already shown on the trace line
    # just above, and two rows matter on a 16-row screen
    if len(diff) >= 2 and diff[0].startswith("---") and diff[1].startswith("+++"):
        diff = diff[2:]
    budget = diff_budget()
    width = term_width()
    shown = diff[:budget]
    out = []
    for line in shown:
        line = line.rstrip("\n")
        # truncate rather than let one long line wrap over several rows
        if len(line) > width:
            line = line[: width - 1] + "…"
        if line.startswith("+++") or line.startswith("---"):
            out.append(dim(line))
        elif line.startswith("+"):
            out.append(green(line))
        elif line.startswith("-"):
            out.append(red(line))
        elif line.startswith("@@"):
            out.append(cyan(line))
        else:
            out.append(dim(line))
    if len(diff) > budget:
        out.append(dim(f"… (+{len(diff) - budget} lignes)"))
    return "\n".join(out)


# files modified this session, most recent last: [(path, backup_or_None), ...]
EDIT_LOG = []


def _backup(path):
    """Keep a single-level .bak copy before overwriting, and log it for /undo."""
    bak = None
    try:
        if os.path.isfile(path):
            bak = path + ".pia.bak"
            shutil.copyfile(path, bak)
    except Exception:
        bak = None
    EDIT_LOG.append((path, bak))


def undo_last_edit():
    """Restore the most recent file this session modified. Returns a message."""
    while EDIT_LOG:
        path, bak = EDIT_LOG.pop()
        if bak and os.path.isfile(bak):
            try:
                shutil.copyfile(bak, path)
                os.remove(bak)
                return f"restauré : {path}"
            except Exception as e:
                return f"ERREUR restauration {path}: {e}"
        # the file was newly created (no backup) -> removing it is the undo
        if bak is None and os.path.isfile(path):
            try:
                os.remove(path)
                return f"supprimé (fichier créé par pia) : {path}"
            except Exception as e:
                return f"ERREUR suppression {path}: {e}"
    return "rien à annuler."


# Whole files are held in RAM here, and this machine only has 512 MB.
MAX_FILE_BYTES = 2_000_000


def tool_read_file(args):
    path = args["path"]
    try:
        size = os.path.getsize(path)
        with open(path, "r", errors="replace") as f:
            # only ever pull in what we would send anyway: loading a 2 MB file
            # just to trim it to 12 kB would waste this machine's RAM
            data = f.read(MAX_TOOL_OUTPUT + 1)
    except Exception as e:
        return f"ERROR reading {path}: {e}"
    if len(data) > MAX_TOOL_OUTPUT:
        data = data[:MAX_TOOL_OUTPUT]
        data += f"\n... [tronque ; le fichier fait {size} octets]"
    return data


def tool_write_file(args):
    path = args["path"]
    content = args.get("content", "")
    d = os.path.dirname(os.path.abspath(path))
    try:
        _backup(path)
        if d and not os.path.isdir(d):
            os.makedirs(d)
        with open(path, "w") as f:
            f.write(content)
    except Exception as e:
        return f"ERROR writing {path}: {e}"
    return f"wrote {len(content)} chars to {path}"


def tool_str_replace(args):
    path = args["path"]
    old = args["old"]
    new = args.get("new", "")
    try:
        # this rewrites the whole file, so refuse rather than blow up the RAM
        if os.path.getsize(path) > MAX_FILE_BYTES:
            return (
                f"ERROR: {path} depasse {MAX_FILE_BYTES} octets ; "
                "trop gros pour une edition en memoire sur cette machine"
            )
        with open(path, "r", errors="replace") as f:
            data = f.read()
    except Exception as e:
        return f"ERROR reading {path}: {e}"
    n = data.count(old)
    if n == 0:
        return f"ERROR: pattern not found in {path}"
    if n > 1:
        return f"ERROR: pattern is not unique ({n} matches) in {path}; add more context"
    data = data.replace(old, new, 1)
    try:
        _backup(path)
        with open(path, "w") as f:
            f.write(data)
    except Exception as e:
        return f"ERROR writing {path}: {e}"
    return f"replaced 1 occurrence in {path}"


def tool_list_dir(args):
    path = args.get("path", ".")
    try:
        entries = sorted(os.listdir(path))
    except Exception as e:
        return f"ERROR listing {path}: {e}"
    lines = []
    for name in entries:
        full = os.path.join(path, name)
        marker = "/" if os.path.isdir(full) else ""
        lines.append(name + marker)
    return _truncate("\n".join(lines) or "(empty)")


SEARCH_SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv", "venv", ".cache"}
MAX_SEARCH_MATCHES = 200
MAX_SEARCH_FILE_SIZE = 2_000_000  # skip huge files to protect 512 MB of RAM


def tool_glob_search(args):
    pattern = args["pattern"]
    root = args.get("path", ".")
    matches = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SEARCH_SKIP_DIRS]
        for name in filenames:
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, root)
            if fnmatch.fnmatch(rel, pattern) or fnmatch.fnmatch(name, pattern):
                matches.append(rel)
                if len(matches) >= MAX_SEARCH_MATCHES:
                    break
        if len(matches) >= MAX_SEARCH_MATCHES:
            break
    return _truncate("\n".join(sorted(matches)) or "(no matches)")


def tool_grep_search(args):
    pattern = args["pattern"]
    root = args.get("path", ".")
    file_glob = args.get("glob")
    try:
        rx = re.compile(pattern)
    except Exception as e:
        return f"ERROR: invalid regex: {e}"
    results = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SEARCH_SKIP_DIRS]
        for name in filenames:
            if file_glob and not fnmatch.fnmatch(name, file_glob):
                continue
            full = os.path.join(dirpath, name)
            try:
                if os.path.getsize(full) > MAX_SEARCH_FILE_SIZE:
                    continue
                with open(full, "r", errors="ignore") as f:
                    for i, line in enumerate(f, 1):
                        if rx.search(line):
                            rel = os.path.relpath(full, root)
                            results.append(f"{rel}:{i}: {line.strip()}")
                            if len(results) >= MAX_SEARCH_MATCHES:
                                break
            except Exception:
                continue
            if len(results) >= MAX_SEARCH_MATCHES:
                break
        if len(results) >= MAX_SEARCH_MATCHES:
            break
    return _truncate("\n".join(results) or "(no matches)")


# Settings that tools need but that are not passed through the tool schema.
# Populated from the config at the start of every turn.
RUNTIME = {"bash_timeout": 300}

MAX_ECHO_LINES = 200  # printed live; the model still receives the full capture


def tool_run_bash(args):
    """Run a shell command, streaming its output as it appears.

    Two things matter on a 1 GHz single-core board: you must SEE that a slow
    command is progressing, and it must not be killed just for being slow.
    Output is echoed line by line, and the timeout is generous and
    configurable ("bash_timeout"). Ctrl-C kills the command, not pia.
    """
    cmd = args["command"]
    timeout = int(RUNTIME.get("bash_timeout", 300))
    width = term_width()
    captured = []
    echoed = 0

    try:
        proc = subprocess.Popen(
            cmd,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=1,
            universal_newlines=True,
            errors="replace",
            # own process group: killing the shell alone leaves its children
            # holding the pipe open, and we would block reading it forever
            start_new_session=True,
        )
    except Exception as e:
        return f"ERROR running command: {e}"

    def _kill_tree():
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    # a watchdog, so a command that hangs without printing anything still dies
    killed = {"by_timeout": False}

    def _kill():
        killed["by_timeout"] = True
        _kill_tree()

    watchdog = threading.Timer(timeout, _kill)
    watchdog.start()
    try:
        for line in proc.stdout:
            captured.append(line)
            if echoed < MAX_ECHO_LINES:
                eprint(dim("  | " + line.rstrip("\n")[: width - 4]))
                echoed += 1
            elif echoed == MAX_ECHO_LINES:
                eprint(dim("  | ... (suite masquee a l'ecran)"))
                echoed += 1
        proc.wait()
    except KeyboardInterrupt:
        _kill_tree()
        eprint(dim("  (commande interrompue)"))
        captured.append("\n[interrompu par l'utilisateur]")
    except Exception as e:
        captured.append(f"\n[erreur de lecture: {e}]")
    finally:
        watchdog.cancel()
        try:
            proc.stdout.close()
        except Exception:
            pass

    out = "".join(captured)
    if killed["by_timeout"]:
        return _truncate(
            f"ERROR: commande tuee apres {timeout}s "
            f'(augmente "bash_timeout" dans la config)\n{out}'
        )
    return _truncate(f"[exit {proc.returncode}]\n{out}")


# name -> (function, needs_confirmation, description-for-user)
TOOLS = {
    "read_file": (tool_read_file, False, "read"),
    "write_file": (tool_write_file, True, "write"),
    "str_replace": (tool_str_replace, True, "edit"),
    "list_dir": (tool_list_dir, False, "list"),
    "glob_search": (tool_glob_search, False, "find"),
    "grep_search": (tool_grep_search, False, "grep"),
    "run_bash": (tool_run_bash, True, "run"),
}

TOOL_SCHEMA = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read the full text contents of a file.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Create or overwrite a file with the given content.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "str_replace",
            "description": (
                "Replace exactly one unique occurrence of 'old' with 'new' in a "
                "file. 'old' must match the existing text exactly and be unique."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "old": {"type": "string"},
                    "new": {"type": "string"},
                },
                "required": ["path", "old", "new"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_dir",
            "description": "List the entries of a directory (directories end with /).",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "glob_search",
            "description": "Find files by name pattern (e.g. '*.py', 'src/**/*.ts').",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string"},
                    "path": {"type": "string"},
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "grep_search",
            "description": (
                "Search file contents with a regular expression across a "
                "directory tree. Returns 'path:line: text' per match."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string"},
                    "path": {"type": "string"},
                    "glob": {
                        "type": "string",
                        "description": "only search files matching this name pattern, e.g. '*.py'",
                    },
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_bash",
            "description": (
                "Run a shell command in the current working directory and return "
                "its combined stdout/stderr and exit code."
            ),
            "parameters": {
                "type": "object",
                "properties": {"command": {"type": "string"}},
                "required": ["command"],
            },
        },
    },
]

SYSTEM_PROMPT = (
    "You are pia, a concise AI coding assistant running in a terminal on a small "
    "ARM computer (a Pocket C.H.I.P). Keep answers short and to the point; the "
    "screen is tiny. You can inspect and modify the local project using the "
    "provided tools: read_file, write_file, str_replace, list_dir, glob_search, "
    "grep_search, run_bash. Prefer str_replace for small edits and write_file "
    "for new files. Use glob_search to find files by name and grep_search to "
    "find text/code across the project instead of shelling out to find/grep. "
    "Before editing a file, read it. Explain what you did in one or two "
    "sentences. Do not invent file contents — read them first."
)

# ---------------------------------------------------------------------------
# Project context file (PIA.md / AGENTS.md / CLAUDE.md), like other agent CLIs
# ---------------------------------------------------------------------------
CONTEXT_FILENAMES = ("PIA.md", "AGENTS.md", "CLAUDE.md")
MAX_CONTEXT_CHARS = 8000  # bounded: this rides along in every request


def find_context_file(start=None):
    """Look for a project context file in cwd, then walk up to the git root."""
    d = os.path.abspath(start or os.getcwd())
    while True:
        for name in CONTEXT_FILENAMES:
            p = os.path.join(d, name)
            if os.path.isfile(p):
                return p
        if os.path.isdir(os.path.join(d, ".git")):
            break  # don't escape the project
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    return None


# ---------------------------------------------------------------------------
# Environment block — tell the model what machine it is actually driving
# ---------------------------------------------------------------------------
def _os_name():
    try:
        with open("/etc/os-release") as f:
            for line in f:
                if line.startswith("PRETTY_NAME="):
                    return line.split("=", 1)[1].strip().strip('"')
    except Exception:
        pass
    try:
        return os.uname().sysname
    except Exception:
        return "unknown"


def _machine():
    try:
        return os.uname().machine
    except Exception:
        return "unknown"


def total_ram_mb():
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    return int(line.split()[1]) // 1024
    except Exception:
        pass
    return None


def _git_context():
    """Branch and dirtiness, or None when this is not a git checkout."""
    if not shutil.which("git"):
        return None
    rc, out, _ = _run_git(".", ["rev-parse", "--abbrev-ref", "HEAD"], timeout=5)
    if rc != 0:
        return None
    branch = out or "HEAD"
    rc2, status, _ = _run_git(".", ["status", "--porcelain"], timeout=5)
    if rc2 != 0:
        return f"branch {branch}"
    n = len([l for l in status.split("\n") if l.strip()])
    return f"branch {branch}, {n} fichier(s) modifie(s)" if n else f"branch {branch}, propre"


def environment_block():
    """Compact facts about this machine, so the model stops guessing.

    Ordered stable-first, volatile-last: providers cache on a shared prefix,
    and putting the date or git status up top would bust that cache every
    turn (opencode has this exact bug, their issue #5224).

    Deliberately no file tree: opencode ships up to 200 entries, which would
    cost more tokens per turn than this machine's whole session budget.
    """
    ram = total_ram_mb()
    lines = [
        f"os: {_os_name()} ({_machine()})",
        f"python: {sys.version.split()[0]}",
        f"shell: {os.environ.get('SHELL', 'sh')}",
    ]
    if ram:
        lines.append(f"ram: {ram} MB total")
    lines.append(f"terminal: {term_width()}x{term_height()} caracteres")
    lines.append(f"cwd: {os.getcwd()}")
    git = _git_context()
    lines.append(f"git: {git}" if git else "git: pas un depot git")
    lines.append(f"date: {time.strftime('%Y-%m-%d')}")

    block = "<environment>\n" + "\n".join(lines) + "\n</environment>"

    notes = []
    if ram and ram < 1024:
        notes.append(
            "This machine has very little RAM: never run full builds, test "
            "suites or installs unless asked; prefer targeted commands."
        )
    if term_width() <= 60:
        notes.append(
            "The screen is tiny: keep every reply to a few short lines, and "
            "avoid printing long files or command output."
        )
    if notes:
        block += "\n" + " ".join(notes)
    return block


def build_system_prompt():
    """Assemble: static instructions, then project rules, then environment.

    The environment goes last on purpose — it is the part that changes
    between sessions, so everything above it stays cacheable.
    """
    prompt = SYSTEM_PROMPT
    path = find_context_file()
    if path:
        try:
            with open(path, "r", errors="replace") as f:
                extra = f.read(MAX_CONTEXT_CHARS)
            if extra.strip():
                prompt += (
                    f"\n\nProject instructions from {os.path.basename(path)} "
                    f"(follow them):\n{extra}"
                )
        except Exception:
            pass
    try:
        prompt += "\n\n" + environment_block()
    except Exception:
        pass  # never let environment detection break startup
    return prompt


# ---------------------------------------------------------------------------
# User-defined slash commands: ~/.config/pia/commands/NAME.md
# ---------------------------------------------------------------------------
def commands_dir():
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, APP_NAME, "commands")


def load_custom_commands():
    """Return {name: template}. '$ARGUMENTS' in a template is replaced at call time."""
    d = commands_dir()
    out = {}
    try:
        names = os.listdir(d)
    except Exception:
        return out
    for fn in names:
        if not fn.endswith(".md"):
            continue
        try:
            with open(os.path.join(d, fn), "r", errors="replace") as f:
                out[fn[:-3]] = f.read()
        except Exception:
            continue
    return out


# ---------------------------------------------------------------------------
# @file mentions — inline a file's contents into the prompt
# ---------------------------------------------------------------------------
FILE_MENTION_RE = re.compile(r"@([A-Za-z0-9_./~-]+)")
MAX_MENTION_CHARS = 20000


def expand_mentions(text):
    """Inline the contents of @-mentioned files that actually exist."""
    seen = []
    for m in FILE_MENTION_RE.finditer(text):
        raw = m.group(1)
        p = os.path.expanduser(raw)
        if raw in [s[0] for s in seen]:
            continue
        if os.path.isfile(p):
            try:
                with open(p, "r", errors="replace") as f:
                    seen.append((raw, f.read(MAX_MENTION_CHARS)))
            except Exception:
                continue
    if not seen:
        return text
    parts = [text]
    for name, content in seen:
        parts.append(f"\n\n--- {name} ---\n{content}")
        eprint(dim(f"  (joint : {name})"))
    return "".join(parts)


# ---------------------------------------------------------------------------
# HTTP / model call
# ---------------------------------------------------------------------------
RETRY_STATUSES = {408, 429, 500, 502, 503, 504}
MAX_RETRIES = 4

# urllib announces itself as "Python-urllib/3.x", which several API front-ends
# (Cloudflare in particular) reject outright. Identify the client properly.
USER_AGENT = f"{APP_NAME}/{VERSION}"


def request_headers(provider, stream=False, json_accept=False):
    """Standard headers, plus any per-provider overrides from the config."""
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json" if json_accept or not stream
        else "text/event-stream",
        "User-Agent": USER_AGENT,
    }
    if provider.get("api_key"):
        headers["Authorization"] = "Bearer " + provider["api_key"]
    # "headers" in the provider config wins, so a blocked setup can be fixed
    # without touching the code
    extra = provider.get("headers")
    if isinstance(extra, dict):
        headers.update({str(k): str(v) for k, v in extra.items()})
    return headers


def describe_http_error(code, body, url):
    """Turn an error body into something readable on a 16-row screen.

    Gateways answer with a full HTML page; dumping that raw filled the CHIP's
    screen with markup and hid the actual problem.
    """
    head = body[:400].lower()
    if "<html" in head or "<!doctype" in head:
        m = re.search(r"<title>(.*?)</title>", body, re.S | re.I)
        title = re.sub(r"\s+", " ", m.group(1)).strip() if m else "reponse HTML"
        msg = f"HTTP {code} depuis {url}\n{title[:120]}"
        if "cloudflare" in body.lower():
            msg += (
                "\nBloque par le pare-feu du fournisseur, pas par l'API elle-meme."
                "\nVerifie : l'URL de base correspond bien a ton abonnement,"
                " la cle est valide, et le service est ouvert dans ta region."
            )
        return msg
    return f"HTTP {code} depuis {url}\n{body[:400]}"


def http_chat(provider, messages, tools, stream, timeout, echo=True):
    """POST /chat/completions, retrying transient failures.

    The CHIP's wifi drops often, so network errors and 5xx/429 responses are
    retried with exponential backoff. Auth/config errors (401/403/404) are not
    retried — they will never succeed on their own.
    """
    last_err = None
    for attempt in range(MAX_RETRIES):
        try:
            return _http_chat_once(provider, messages, tools, stream, timeout, echo)
        except RetryableError as e:
            last_err = e
            if attempt == MAX_RETRIES - 1:
                break
            delay = 2 ** attempt  # 1s, 2s, 4s
            brief = str(e).split("\n")[0][:60]  # keep it to one short line
            eprint(dim(f"  ({brief} — nouvel essai dans {delay}s…)"))
            time.sleep(delay)
    raise RuntimeError(str(last_err))


class RetryableError(Exception):
    pass


def _http_chat_once(provider, messages, tools, stream, timeout, echo=True):
    url = provider["base_url"].rstrip("/") + "/chat/completions"
    payload = {
        "model": provider["model"],
        "messages": messages,
        "stream": bool(stream),
    }
    if stream:
        # ask OpenAI-compatible servers to send a final usage chunk; servers
        # that don't support it simply ignore the field.
        payload["stream_options"] = {"include_usage": True}
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"

    body = json.dumps(payload).encode("utf-8")
    headers = request_headers(provider, stream)

    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        msg = describe_http_error(e.code, detail, url)
        if e.code in RETRY_STATUSES:
            raise RetryableError(msg)
        raise RuntimeError(msg)
    except urllib.error.URLError as e:
        raise RetryableError(f"connexion échouée vers {url}: {e.reason}")
    except OSError as e:  # socket timeouts, connection reset, ...
        raise RetryableError(f"erreur réseau vers {url}: {e}")

    if stream:
        return _read_stream(resp, echo)
    return _read_full(resp, echo)


def _read_full(resp, echo=True):
    obj = json.loads(resp.read().decode("utf-8", errors="replace"))
    msg = obj["choices"][0]["message"]
    content = msg.get("content") or ""
    if content and echo:
        sys.stdout.write(wrap(content))
        sys.stdout.write("\n")
        sys.stdout.flush()
    tool_calls = []
    for tc in msg.get("tool_calls") or []:
        tool_calls.append(
            {
                "id": tc.get("id", ""),
                "name": tc["function"]["name"],
                "arguments": tc["function"].get("arguments", "") or "",
            }
        )
    usage = obj.get("usage") or {}
    return content, tool_calls, usage


def _read_stream(resp, echo=True):
    content_parts = []
    slots = {}  # index -> {id, name, arguments}
    usage = {}
    printed_any = False
    sw = StreamWrap()
    for raw in resp:
        line = raw.decode("utf-8", errors="replace").strip()
        if not line or not line.startswith("data:"):
            continue
        data = line[len("data:"):].strip()
        if data == "[DONE]":
            break
        try:
            obj = json.loads(data)
        except Exception:
            continue
        if obj.get("usage"):
            usage = obj["usage"]
        choices = obj.get("choices") or []
        if not choices:
            continue
        delta = choices[0].get("delta") or {}
        piece = delta.get("content")
        if piece:
            if echo:
                shown = sw.feed(piece)
                if shown:
                    sys.stdout.write(shown)
                    sys.stdout.flush()
                printed_any = True
            # history keeps the raw text, never the wrapped version
            content_parts.append(piece)
        for tc in delta.get("tool_calls") or []:
            idx = tc.get("index", 0)
            slot = slots.setdefault(idx, {"id": "", "name": "", "arguments": ""})
            if tc.get("id"):
                slot["id"] = tc["id"]
            fn = tc.get("function") or {}
            if fn.get("name"):
                slot["name"] = fn["name"]
            if fn.get("arguments"):
                slot["arguments"] += fn["arguments"]
    if printed_any:
        sys.stdout.write(sw.finish())  # flush the last partial word
        sys.stdout.write("\n")
        sys.stdout.flush()
    content = "".join(content_parts)
    tool_calls = [slots[i] for i in sorted(slots)]
    return content, tool_calls, usage


# ---------------------------------------------------------------------------
# Agent loop
# ---------------------------------------------------------------------------
def confirm_action(prompt):
    """Ask y/N/a. Returns 'y', 'n', or 'a' (always-approve this tool type).

    stdin may be non-interactive (piped, redirected, cron, ...) — e.g. when
    one-shot mode also reads stdin for context. There is then no way to ask,
    so fail closed and say why, instead of silently hanging or declining.
    """
    if not sys.stdin.isatty():
        eprint(red("    non-interactif : action refusée (utilise --yolo)"))
        return "n"
    try:
        ans = input(yellow(prompt + " [y/N/a] ")).strip().lower()
    except EOFError:
        return "n"
    if ans in ("a", "always", "toujours"):
        return "a"
    if ans in ("y", "yes", "o", "oui"):
        return "y"
    return "n"


def _preview_diff(name, args):
    """Show a small diff for write_file/str_replace before asking to confirm."""
    path = args.get("path")
    if not path:
        return
    try:
        old = ""
        if os.path.isfile(path):
            with open(path, "r", errors="replace") as f:
                old = f.read()
        if name == "write_file":
            new = args.get("content", "")
        elif name == "str_replace":
            old_pat, new_pat = args.get("old", ""), args.get("new", "")
            if old_pat not in old:
                return  # tool_str_replace will report the real error
            new = old.replace(old_pat, new_pat, 1)
        else:
            return
        diff = make_diff(old, new, path)
        if diff:
            eprint(diff)
        else:
            eprint(dim("    (no changes)"))
    except Exception:
        pass  # preview is best-effort; never block the actual tool call


def run_tool(name, arguments_str, cfg):
    if name not in TOOLS:
        return f"ERROR: unknown tool '{name}'"
    try:
        args = json.loads(arguments_str) if arguments_str.strip() else {}
    except Exception as e:
        return f"ERROR: could not parse arguments: {e}"

    fn, needs_confirm, verb = TOOLS[name]
    auto_approve = bool(cfg.get("auto_approve", False))
    always = cfg.setdefault("_always_approve", set())

    # show what is about to happen
    target = args.get("path") or args.get("command") or ""
    eprint(dim(f"  → {verb} {target}"[: term_width() - 1]))

    if needs_confirm and not auto_approve and name not in always:
        if name in ("write_file", "str_replace"):
            _preview_diff(name, args)
        ans = confirm_action(f"    allow {name}?")
        if ans == "n":
            return "User declined this action."
        if ans == "a":
            always.add(name)
    return fn(args)


# ---------------------------------------------------------------------------
# Context budget — conditioned on the model, not on a fixed message count
# ---------------------------------------------------------------------------
# Hints only, and deliberately conservative: guessing low just trims early,
# guessing high overflows the request and the API rejects it outright. A
# `context_limit` in the config (global or per provider) always wins, and
# /context shows which value is in force.
KNOWN_CONTEXT_LIMITS = (
    ("claude", 200000),
    ("gpt-4o", 128000),
    ("gpt-5", 200000),
    ("gemini", 200000),
    ("kimi", 128000),
    ("qwen", 128000),
    ("deepseek", 128000),
    ("grok", 128000),
    ("minimax", 128000),
    ("llama", 128000),
    ("mistral", 128000),
)
DEFAULT_CONTEXT_LIMIT = 32000
COMPACT_THRESHOLD = 0.75  # warn/compact once the prompt passes this share


def context_limit(cfg, provider):
    """Tokens this model can take, and where that number came from."""
    for src, holder in (("config", provider), ("config", cfg)):
        v = holder.get("context_limit")
        if v:
            return int(v), src
    model = (provider.get("model") or "").lower()
    for key, lim in KNOWN_CONTEXT_LIMITS:
        if key in model:
            return lim, "table"
    return DEFAULT_CONTEXT_LIMIT, "defaut"


def estimate_tokens(messages):
    """Rough token count (~4 chars/token). Used before the API tells us."""
    total = 0
    for m in messages:
        c = m.get("content")
        if isinstance(c, str):
            total += len(c)
        for tc in m.get("tool_calls") or []:
            total += len((tc.get("function") or {}).get("arguments") or "")
        total += 16  # per-message envelope
    return total // 4


def trim_history(messages, max_messages, token_budget=None):
    """Drop the oldest whole turns to stay inside both budgets.

    A "turn" starts at a user message, so tool_call/tool-result pairs are
    never split apart. The token budget is what actually protects the request
    from being rejected; max_messages is just a backstop. Returns how many
    messages were dropped.
    """
    system = messages[:1] if messages and messages[0].get("role") == "system" else []
    rest = messages[len(system):]
    dropped = 0

    def too_big():
        if max_messages > 0 and len(rest) > max_messages:
            return True
        if token_budget and estimate_tokens(system + rest) > token_budget:
            return True
        return False

    while rest and too_big():
        rest.pop(0)
        dropped += 1
        while rest and rest[0].get("role") != "user":
            rest.pop(0)
            dropped += 1
    messages[:] = system + rest
    return dropped


def agent_turn(cfg, provider, messages):
    max_steps = int(cfg.get("max_steps", 25))
    stream = bool(cfg.get("stream", True))
    timeout = int(cfg.get("request_timeout", 180))
    max_history = int(cfg.get("max_history_messages", 40))
    usage_total = cfg.setdefault("_usage", {"prompt": 0, "completion": 0, "total": 0})
    limit, _src = context_limit(cfg, provider)
    budget = int(limit * COMPACT_THRESHOLD)
    RUNTIME["bash_timeout"] = int(cfg.get("bash_timeout", 300))

    dropped = trim_history(messages, max_history, token_budget=budget)
    if dropped:
        eprint(dim(f"  (contexte plein : {dropped} anciens messages retires)"))

    for _ in range(max_steps):
        content, tool_calls, usage = http_chat(
            provider, messages, TOOL_SCHEMA, stream, timeout
        )
        if usage:
            usage_total["prompt"] += usage.get("prompt_tokens", 0) or 0
            usage_total["completion"] += usage.get("completion_tokens", 0) or 0
            usage_total["total"] += usage.get("total_tokens", 0) or 0

        if not tool_calls:
            messages.append({"role": "assistant", "content": content})
            if usage:
                prompt_tok = usage.get("prompt_tokens", 0) or 0
                cfg["_last_prompt_tokens"] = prompt_tok
                pct = round(100 * prompt_tok / limit) if limit else 0
                eprint(
                    dim(
                        f"  [{prompt_tok}+{usage.get('completion_tokens', 0)} tok · "
                        f"contexte {pct}% · session {usage_total['total']}]"
                    )
                )
                if pct >= COMPACT_THRESHOLD * 100:
                    eprint(
                        yellow(
                            f"  contexte a {pct}% — /compact pour resumer"
                            + (" (auto)" if cfg.get("auto_compact") else "")
                        )
                    )
                    if cfg.get("auto_compact"):
                        ok, info = compact_history(cfg, provider, messages)
                        eprint(dim("  compacte." if ok else f"  {info}"))
            return

        # record the assistant's tool-call turn
        messages.append(
            {
                "role": "assistant",
                "content": content or None,
                "tool_calls": [
                    {
                        "id": tc["id"],
                        "type": "function",
                        "function": {
                            "name": tc["name"],
                            "arguments": tc["arguments"],
                        },
                    }
                    for tc in tool_calls
                ],
            }
        )

        for tc in tool_calls:
            result = run_tool(tc["name"], tc["arguments"], cfg)
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": result,
                }
            )
    eprint(red(f"stopped after {max_steps} steps (max_steps reached)."))


# ---------------------------------------------------------------------------
# Extra REPL features: model listing, context compaction, git helpers
# ---------------------------------------------------------------------------
def fetch_models(provider, timeout=30):
    """GET {base_url}/models — returns a list of model ids."""
    url = provider["base_url"].rstrip("/") + "/models"
    req = urllib.request.Request(
        url, headers=request_headers(provider, json_accept=True)
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            obj = json.loads(resp.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        raise RuntimeError(describe_http_error(e.code, detail, url))
    except Exception as e:
        raise RuntimeError(f"echec de la requete vers {url}: {e}")
    items = obj.get("data") if isinstance(obj, dict) else obj
    ids = []
    for it in items or []:
        if isinstance(it, dict) and it.get("id"):
            ids.append(it["id"])
        elif isinstance(it, str):
            ids.append(it)
    return sorted(ids)


def save_model(provider_name, model):
    """Remember the chosen model as this provider's default."""
    path = config_path()
    d = os.path.dirname(path)
    if d and not os.path.isdir(d):
        os.makedirs(d)
    data = {}
    if os.path.exists(path):
        try:
            with open(path, "r") as f:
                data = json.load(f)
        except Exception:
            data = {}
    provs = data.setdefault("providers", {})
    if provider_name not in provs:
        provs[provider_name] = dict(BUILTIN_PROVIDERS.get(provider_name, {}))
    provs[provider_name]["model"] = model
    with open(path, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")
    try:
        os.chmod(path, 0o600)  # the file may also hold the api key
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Arrow-key menu (stdlib only: termios/tty, no curses, no dependencies)
# ---------------------------------------------------------------------------
def _read_key(fd):
    """Read one keypress in raw mode. Returns (kind, value).

    kind is one of: up, down, enter, quit, back, home, end, char, other.
    Printable characters come back as ("char", c) so callers can filter.
    """
    import select

    ch = os.read(fd, 1)
    if not ch or ch in (b"\x03", b"\x04"):  # EOF, Ctrl-C, Ctrl-D
        return ("quit", "")
    if ch in (b"\r", b"\n"):
        return ("enter", "")
    if ch in (b"\x7f", b"\x08"):  # Backspace
        return ("back", "")
    if ch == b"\x1b":
        # bare Esc cancels; Esc [ X is an arrow/navigation sequence
        ready, _, _ = select.select([fd], [], [], 0.05)
        if not ready:
            return ("quit", "")
        seq = os.read(fd, 2)
        return (
            {b"[A": "up", b"[B": "down", b"[H": "home", b"[F": "end"}.get(
                seq, "other"
            ),
            "",
        )
    try:
        c = ch.decode("utf-8")
    except UnicodeDecodeError:
        return ("other", "")
    return ("char", c) if c.isprintable() else ("other", "")


def _menu_frame(items, sel, top, rows, width, current, query, total):
    """Render one frame of the menu; returns (text, lines_drawn)."""
    out = []
    if not items:
        out.append("\033[2K" + dim("  (aucun resultat)") + "\r\n")
        n = 1
    else:
        shown = list(range(top, min(top + rows, len(items))))
        for i in shown:
            mark = "  (actuel)" if items[i] == current else ""
            label = (("> " if i == sel else "  ") + items[i] + mark)[: width - 1]
            if i == sel and USE_COLOR:
                label = "\033[7m" + label + "\033[0m"  # reverse video
            out.append("\033[2K" + label + "\r\n")
        n = len(shown)
    if query:
        hint = f"filtre: {query}_ · Entree · Esc annule"
    else:
        hint = "tapez pour filtrer · fleches · Entree · Esc"
    if len(items) != total:
        hint += f" [{len(items)}/{total}]"
    elif items and len(items) > rows:
        hint += f" [{sel + 1}/{len(items)}]"
    out.append("\033[2K" + dim(hint[: width - 1]) + "\r\n")
    return "".join(out), n + 1


def pick_interactive(items, current=None):
    """Arrow-key picker with type-to-filter. Returns (handled, chosen_item).

    handled=False means this terminal cannot do raw mode (not a tty, dumb
    TERM, no termios) and the caller should fall back to a numbered prompt.
    Filtering matters here: OpenCode Zen alone offers dozens of models, which
    is a lot to scroll through on a 16-row screen.
    """
    if not items:
        return True, None
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return False, None
    if os.environ.get("TERM", "") in ("", "dumb"):
        return False, None
    try:
        import termios
        import tty

        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
    except Exception:
        return False, None

    total = len(items)
    query = ""
    view = list(items)
    sel = view.index(current) if current in view else 0
    top = 0
    drawn = 0
    rows = max(3, term_height() - 3)  # leave room for the hint and the prompt
    width = term_width()
    result = None
    sys.stdout.write("\033[?25l")  # hide cursor while navigating
    try:
        tty.setraw(fd)
        while True:
            if sel < top:
                top = sel
            elif sel >= top + rows:
                top = sel - rows + 1
            frame, n = _menu_frame(view, sel, top, rows, width, current, query, total)
            if drawn:
                sys.stdout.write(f"\033[{drawn}A")  # rewind to redraw in place
            sys.stdout.write(frame)
            sys.stdout.flush()
            drawn = n

            kind, val = _read_key(fd)
            if kind == "up" and view:
                sel = (sel - 1) % len(view)
            elif kind == "down" and view:
                sel = (sel + 1) % len(view)
            elif kind == "home":
                sel = 0
            elif kind == "end" and view:
                sel = len(view) - 1
            elif kind == "enter":
                if view:
                    result = view[sel]
                break
            elif kind == "quit":
                break
            elif kind in ("char", "back"):
                query = query[:-1] if kind == "back" else query + val
                q = query.lower()
                view = [it for it in items if q in it.lower()] if q else list(items)
                sel, top = 0, 0
    finally:
        try:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)
        except Exception:
            pass
        # erase the menu so it does not clutter the tiny screen
        if drawn:
            sys.stdout.write(f"\033[{drawn}A" + "\033[J")
        sys.stdout.write("\033[?25h")  # show cursor again
        sys.stdout.flush()
    return True, result


def choose_model(cfg, provider):
    """Numbered model picker: type a number instead of a long model name.

    Deliberately plain stdin/stdout — no curses, no cursor keys — so it works
    over a flaky SSH link and on the CHIP's own terminal.
    """
    try:
        ids = fetch_models(provider)
    except RuntimeError as e:
        eprint_err(e)
        eprint(dim("ce fournisseur n'expose peut-etre pas /v1/models."))
        eprint(dim("entre le nom a la main : /model <nom>"))
        return None
    if not ids:
        eprint(dim("(aucun modele renvoye)"))
        return None

    current = provider.get("model")

    # preferred path: arrow keys + type-to-filter
    handled, picked = pick_interactive(ids, current=current)
    if handled:
        return picked

    # fallback for terminals without raw mode: numbered list
    width = term_width()
    for i, mid in enumerate(ids, 1):
        mark = " *" if mid == current else ""
        print(dim(f"{i:>2}) {mid}{mark}"[: width - 1]))
    if not sys.stdin.isatty():
        return None
    try:
        ans = input(yellow("numero (Entree = garder) : ")).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return None
    if not ans:
        return None
    if not ans.isdigit() or not (1 <= int(ans) <= len(ids)):
        eprint(red("numero invalide."))
        return None
    return ids[int(ans) - 1]


def set_model(cfg, provider, model):
    """Apply a model to the running session and persist it as the default."""
    provider["model"] = model
    try:
        save_model(provider["name"], model)
        print(dim(f"modele = {model} (enregistre par defaut)"))
    except Exception as e:
        print(dim(f"modele = {model} (non enregistre : {e})"))


def compact_history(cfg, provider, messages):
    """Replace the conversation with a short summary to free RAM and context."""
    body = [m for m in messages if m.get("role") in ("user", "assistant")]
    if len(body) < 2:
        return False, "conversation trop courte pour être compactée."
    convo = []
    for m in body:
        content = m.get("content") or ""
        if content:
            convo.append(f"{m['role']}: {content}")
    ask = [
        {
            "role": "system",
            "content": "Summarize the conversation so far in at most 10 bullet "
            "points: what the user wants, decisions made, files changed, and "
            "what is left to do. Be terse and factual.",
        },
        {"role": "user", "content": "\n\n".join(convo)[:30000]},
    ]
    try:
        content, _, _ = http_chat(
            provider, ask, None, False, int(cfg.get("request_timeout", 180)), echo=False
        )
    except RuntimeError as e:
        return False, str(e)
    if not content.strip():
        return False, "le modèle n'a rien renvoyé."
    messages[:] = [
        {"role": "system", "content": build_system_prompt()},
        {"role": "assistant", "content": "Résumé de la session précédente :\n" + content},
    ]
    return True, content


def git_here(args, timeout=30):
    """Run a git command in the current directory. Returns (rc, out)."""
    try:
        p = subprocess.run(
            ["git"] + args,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
        )
        return p.returncode, p.stdout.decode("utf-8", "replace")
    except Exception as e:
        return 1, str(e)


def git_commit_with_model(cfg, provider, messages_unused=None):
    """Write a commit message from the staged diff, confirm, then commit."""
    rc, diff = git_here(["diff", "--staged"])
    if rc != 0:
        return "pas un dépôt git (ou git indisponible)."
    if not diff.strip():
        return "rien dans l'index : utilise d'abord `git add`."
    ask = [
        {
            "role": "system",
            "content": "Write a concise git commit message for this staged diff. "
            "First line: imperative summary under 72 chars. Then a blank line and "
            "2-4 bullet points if the change is non-trivial. Output only the "
            "message, no code fences, no preamble.",
        },
        {"role": "user", "content": diff[:30000]},
    ]
    try:
        msg, _, _ = http_chat(
            provider, ask, None, False, int(cfg.get("request_timeout", 180)), echo=False
        )
    except RuntimeError as e:
        return str(e)
    msg = msg.strip()
    if not msg:
        return "le modèle n'a pas proposé de message."
    print(cyan("\n--- message proposé ---"))
    print(wrap(msg))
    print(cyan("-----------------------"))
    if not cfg.get("auto_approve") and confirm_action("Committer ?") == "n":
        return "annulé."
    rc, out = git_here(["commit", "-m", msg])
    return out.strip() or ("commit OK" if rc == 0 else "échec du commit")


INIT_PROMPT = (
    "Explore this project (list_dir, glob_search, grep_search, read_file) and "
    "write a PIA.md file at the project root. It must be short (under 40 lines) "
    "and contain: what the project does, how to build/run/test it, the layout of "
    "the main directories, and any conventions a new contributor must follow. "
    "Write it with write_file."
)


# ---------------------------------------------------------------------------
# REPL
# ---------------------------------------------------------------------------
# Every line kept under 40 columns: the stock Pocket C.H.I.P screen is the
# narrowest target, and a help screen that wraps is worse than a terse one.
HELP = """\
session
  /reset        efface la conversation
  /save [nom]   sauvegarder
  /load [nom]   recharger
  /sessions     liste des sessions
  /usage        tokens consommes
  /context      place restante
  /env          ce que le modele sait
  /compact      resume (libere la RAM)
modele
  /model        choisir (fleches+filtre)
  /model <nom>  changer directement
  /provider [n] voir/changer fournisseur
fichiers & git
  /undo         annule la derniere modif
  /diff         git diff
  /commit       message auto + commit
  /init         genere un PIA.md
divers
  /cwd [dir]    dossier de travail
  /yolo         auto-approbation
  /stream       affichage mot a mot
  /tools /commands /update /help
  /exit         quitter (Ctrl-D aussi)
raccourcis
  !cmd          shell direct, 0 token
  @fichier      joint un fichier
  \\ en fin de ligne = suite dessous
  y/N/a : a = ne plus demander"""


def print_banner(provider, cfg):
    """Two compact lines: on a 16-row screen every line counts."""
    warn = "" if provider.get("api_key") else " (pas de cle)"
    model = provider.get("model") or "(aucun modele)"
    bits = [f"{provider['name']}/{model}"]
    if cfg.get("auto_approve"):
        bits.append("yolo")
    try:
        batt = battery_status()
    except Exception:
        batt = None
    if batt:
        bits.append(f"batt {batt[0]}%")
    head = f"{APP_NAME} v{VERSION} "
    line = " · ".join(bits)
    room = term_width() - len(head) - len(warn)
    if len(line) > room:
        line = line[: max(0, room - 1)] + "…" if room > 1 else ""
    print(bold(cyan(APP_NAME)) + dim(f" v{VERSION} ") + dim(line) + red(warn))
    print(dim("/help pour l'aide, Ctrl-D pour quitter"))


# ---------------------------------------------------------------------------
# Tab completion — every keystroke saved matters on the CHIP's little keyboard
# ---------------------------------------------------------------------------
BUILTIN_COMMANDS = [
    "/help", "/reset", "/model", "/provider", "/models", "/yolo", "/cwd",
    "/update", "/save", "/load", "/sessions", "/usage", "/context", "/env",
    "/compact", "/undo", "/diff", "/commit", "/init", "/commands", "/tools",
    "/exit", "/quit", "/stream",
]


def _path_options(prefix):
    """Filesystem completions, with a trailing / on directories."""
    out = []
    try:
        for m in glob.glob(os.path.expanduser(prefix) + "*"):
            out.append(m + "/" if os.path.isdir(m) else m)
    except Exception:
        return []
    return sorted(out)


def make_completer(commands):
    """Complete /commands at the start of a line, and @paths anywhere.

    Bare words are deliberately left alone: completing ordinary prose against
    the filesystem would fight the user on every sentence.
    """
    def completer(text, state):
        try:
            if text.startswith("@"):
                opts = ["@" + p for p in _path_options(text[1:])]
            elif text.startswith("/"):
                if readline.get_begidx() == 0:
                    opts = sorted(c for c in commands if c.startswith(text))
                else:
                    opts = _path_options(text)  # an absolute path mid-sentence
            else:
                return None
            return opts[state] if state < len(opts) else None
        except Exception:
            return None

    return completer


def setup_completion(custom_commands):
    """Install the completer. Silently does nothing without readline."""
    try:
        import readline as _rl
        globals()["readline"] = _rl
        names = BUILTIN_COMMANDS + ["/" + c for c in custom_commands]
        _rl.set_completer(make_completer(names))
        # keep "@path/to/file" as a single token so it completes as a whole
        _rl.set_completer_delims(" \t\n")
        _rl.parse_and_bind("tab: complete")
    except Exception:
        pass


def _read_input(prompt):
    """Read one logical line, supporting a trailing backslash to keep typing
    on the next line (handy for pasting short multi-line snippets)."""
    line = input(prompt)
    while line.endswith("\\"):
        line = line[:-1] + "\n"
        try:
            line += input(dim("... "))
        except EOFError:
            break
    return line


def repl(cfg, provider, check_update=True, resume=False):
    try:
        import readline  # noqa: F401  (enables line editing/history if available)
    except Exception:
        pass
    setup_completion(load_custom_commands())

    # propose an update before starting the session (silent if none / offline)
    if check_update:
        try:
            offer_update(cfg, restart_argv=[a for a in sys.argv[1:]])
        except Exception:
            pass

    messages = [{"role": "system", "content": build_system_prompt()}]
    if resume:
        try:
            loaded = load_session("last")
            if loaded:
                messages = loaded
                print(dim(f"session reprise ({len(messages)} messages)."))
        except Exception:
            pass

    print_banner(provider, cfg)
    ctx = find_context_file()
    if ctx:
        print(dim(f"contexte projet : {os.path.basename(ctx)}"))
    custom = load_custom_commands()

    while True:
        try:
            line = _read_input(bold(green(f"\n{APP_NAME}> ")))
        except EOFError:
            print()
            break
        except KeyboardInterrupt:
            print()
            continue

        line = line.strip()
        if not line:
            continue

        # shell escape: run it directly, no model call, no tokens spent
        if line.startswith("!"):
            cmd = line[1:].strip()
            if cmd:
                out = tool_run_bash({"command": cmd})
                print(wrap(out))
                messages.append(
                    {"role": "user", "content": f"(J'ai lancé `{cmd}`)\n{out}"}
                )
            continue

        if line.startswith("/"):
            parts = line.split()
            cmd = parts[0]
            arg = " ".join(parts[1:]).strip()
            if cmd in ("/exit", "/quit"):
                break
            elif cmd == "/help":
                print(HELP)
            elif cmd == "/reset":
                messages = [{"role": "system", "content": build_system_prompt()}]
                print(dim("conversation cleared."))
            elif cmd == "/model":
                if arg.isdigit():
                    # allow "/model 3" using the numbering shown by /models
                    try:
                        ids = fetch_models(provider)
                    except RuntimeError as e:
                        ids = []
                        eprint_err(e)
                    if ids and 1 <= int(arg) <= len(ids):
                        set_model(cfg, provider, ids[int(arg) - 1])
                    elif ids:
                        eprint(red(f"numero hors liste (1-{len(ids)})."))
                elif arg:
                    set_model(cfg, provider, arg)
                else:
                    picked = choose_model(cfg, provider)
                    if picked:
                        set_model(cfg, provider, picked)
                    else:
                        print(dim(f"modele = {provider['model']}"))
            elif cmd == "/provider":
                if arg:
                    provider = resolve_provider(cfg, arg)
                    if not provider.get("api_key"):
                        eprint(red(f"warning: no api key for provider {arg}"))
                print(dim(f"provider = {provider['name']} model = {provider['model']}"))
            elif cmd == "/yolo":
                cfg["auto_approve"] = not cfg.get("auto_approve", False)
                print(dim(f"auto_approve = {cfg['auto_approve']}"))
            elif cmd == "/stream":
                cfg["stream"] = not cfg.get("stream", True)
                état = "mot a mot" if cfg["stream"] else "d'un bloc a la fin"
                print(dim(f"streaming = {cfg['stream']} ({état})"))
            elif cmd == "/cwd":
                if arg:
                    try:
                        os.chdir(os.path.expanduser(arg))
                        # the project context file may differ in the new dir
                        if messages and messages[0].get("role") == "system":
                            messages[0]["content"] = build_system_prompt()
                    except Exception as e:
                        eprint_err(e)
                print(dim(os.getcwd()))
            elif cmd == "/update":
                if not offer_update(cfg, restart_argv=[a for a in sys.argv[1:]]):
                    print(dim("déjà à jour (ou vérification impossible)."))
            elif cmd == "/save":
                try:
                    path = save_session(messages, arg or "manual")
                    print(dim(f"session sauvegardée : {path}"))
                except Exception as e:
                    eprint_err(e)
            elif cmd == "/load":
                try:
                    messages = load_session(arg or "manual")
                    print(dim(f"session chargée ({len(messages)} messages)."))
                except Exception as e:
                    eprint_err(e)
            elif cmd == "/sessions":
                names = list_sessions()
                if names:
                    for n in names:
                        print(dim("  " + n))
                else:
                    print(dim("(aucune session sauvegardée)"))
            elif cmd == "/usage":
                u = cfg.get("_usage", {"prompt": 0, "completion": 0, "total": 0})
                print(
                    dim(
                        f"prompt={u['prompt']} completion={u['completion']} "
                        f"total={u['total']} tokens"
                    )
                )
            elif cmd == "/context":
                lim, src = context_limit(cfg, provider)
                est = estimate_tokens(messages)
                real = cfg.get("_last_prompt_tokens")
                print(dim(f"limite  : {lim} tok ({src})"))
                print(dim(f"estime  : {est} tok ({round(100 * est / lim)}%)"))
                if real:
                    print(dim(f"reel API: {real} tok ({round(100 * real / lim)}%)"))
                print(dim(f"messages: {len(messages)}"))
                if src != "config":
                    print(dim('regle "context_limit" dans la config si c\'est faux'))
            elif cmd == "/env":
                print(wrap(environment_block()))
            elif cmd == "/tools":
                for name in TOOLS:
                    print(dim("  " + name))
            elif cmd == "/models":
                picked = choose_model(cfg, provider)
                if picked:
                    set_model(cfg, provider, picked)
            elif cmd == "/compact":
                ok, info = compact_history(cfg, provider, messages)
                if ok:
                    print(dim("historique compacté :"))
                    print(wrap(info))
                else:
                    eprint_err(info)
            elif cmd == "/undo":
                print(dim(undo_last_edit()))
            elif cmd == "/diff":
                rc, out = git_here(["diff"] + (arg.split() if arg else []))
                print(wrap(out.strip() or "(aucune modification)"))
            elif cmd == "/commit":
                print(dim(git_commit_with_model(cfg, provider)))
            elif cmd == "/init":
                messages.append({"role": "user", "content": INIT_PROMPT})
                try:
                    agent_turn(cfg, provider, messages)
                except RuntimeError as e:
                    eprint_err(e)
            elif cmd == "/commands":
                custom = load_custom_commands()
                if custom:
                    for name in sorted(custom):
                        print(dim("  /" + name))
                else:
                    print(dim(f"(aucune ; crée des .md dans {commands_dir()})"))
            elif cmd[1:] in custom:
                template = custom[cmd[1:]]
                text = (
                    template.replace("$ARGUMENTS", arg)
                    if "$ARGUMENTS" in template
                    else (template + ("\n\n" + arg if arg else ""))
                )
                messages.append({"role": "user", "content": expand_mentions(text)})
                try:
                    agent_turn(cfg, provider, messages)
                except RuntimeError as e:
                    eprint_err(e)
                except KeyboardInterrupt:
                    eprint(dim("\n(interrupted)"))
            else:
                eprint(red(f"unknown command {cmd} (try /help)"))
            continue

        messages.append({"role": "user", "content": expand_mentions(line)})
        try:
            agent_turn(cfg, provider, messages)
        except RuntimeError as e:
            eprint_err(e)
        except KeyboardInterrupt:
            eprint(dim("\n(interrupted)"))
        try:
            save_session(messages, "last")  # auto-save: survives crashes / low battery
        except Exception:
            pass


# ---------------------------------------------------------------------------
# One-shot mode
# ---------------------------------------------------------------------------
def one_shot(cfg, provider, prompt):
    messages = [
        {"role": "system", "content": build_system_prompt()},
        {"role": "user", "content": expand_mentions(prompt)},
    ]
    agent_turn(cfg, provider, messages)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main(argv=None):
    parser = argparse.ArgumentParser(
        prog=APP_NAME,
        description="Tiny AI coding agent for small ARM machines (OpenAI-compatible).",
    )
    parser.add_argument("prompt", nargs="*", help="run once with this prompt then exit")
    parser.add_argument("-p", "--provider", help="provider name from config")
    parser.add_argument("-m", "--model", help="override the model")
    parser.add_argument("--base-url", help="override the API base url")
    parser.add_argument("--no-stream", action="store_true", help="disable streaming")
    parser.add_argument("--yolo", action="store_true", help="auto-approve all actions")
    parser.add_argument("--config", action="store_true", help="print config path & exit")
    parser.add_argument(
        "--set-key",
        nargs="?",
        const="-",  # bare --set-key prompts instead, keeping it out of history
        metavar="CLE",
        help="save an API key for this provider (omit the value to type it hidden)",
    )
    parser.add_argument(
        "--update", action="store_true", help="check for updates, install, then exit"
    )
    parser.add_argument(
        "--no-update", action="store_true", help="skip the startup update check"
    )
    parser.add_argument(
        "-c",
        "--continue",
        dest="cont",
        action="store_true",
        help="resume the last interactive session",
    )
    parser.add_argument("--version", action="store_true")
    args = parser.parse_args(argv)

    if args.version:
        print(f"{APP_NAME} {VERSION}")
        return
    if args.config:
        print(config_path())
        return

    cfg = load_config()

    if args.set_key:
        prov_name = args.provider or cfg.get("provider", "opencode")
        key = args.set_key
        if key == "-":
            # typed hidden, so the secret never reaches the shell history
            import getpass

            try:
                key = getpass.getpass(f"cle API pour '{prov_name}' (masquee) : ")
            except (EOFError, KeyboardInterrupt):
                print()
                return
            key = key.strip()
            if not key:
                die("aucune cle saisie.")
        else:
            eprint(
                yellow(
                    "attention : une cle passee en argument reste dans "
                    "l'historique du shell.\nprefere `pia --set-key` sans "
                    "valeur, la saisie sera masquee."
                )
            )
        path = save_key(prov_name, key)
        print(green(f"clé enregistrée pour '{prov_name}' dans {path}"))
        print(dim(f"'{prov_name}' est maintenant le fournisseur par défaut"))
        return

    if args.no_stream:
        cfg["stream"] = False
    if args.yolo:
        cfg["auto_approve"] = True

    if args.update:
        if not offer_update(cfg):
            print(dim("déjà à jour (ou vérification impossible)."))
        return

    provider = resolve_provider(cfg, args.provider)
    if args.model:
        provider["model"] = args.model
    if args.base_url:
        provider["base_url"] = args.base_url

    if not provider.get("api_key"):
        env_name = provider.get("api_key_env", "PIA_API_KEY")
        eprint(
            yellow(
                f"note: pas de clé API trouvée. Enregistre-la une fois pour toutes :\n"
                f"  {APP_NAME} -p {provider['name']} --set-key TA_CLE\n"
                f"ou exporte {env_name} (ou PIA_API_KEY) dans ton shell."
            )
        )
    if not provider.get("model"):
        eprint(
            yellow(
                f"note: aucun modele defini pour '{provider['name']}'.\n"
                f"  {APP_NAME} -m <nom>   ou, dans pia, /model pour choisir."
            )
        )

    if args.prompt:
        prompt_text = " ".join(args.prompt)
        if not sys.stdin.isatty():
            try:
                piped = sys.stdin.read(50_000)  # capped: keep RAM/context small
            except Exception:
                piped = ""
            if piped.strip():
                prompt_text += "\n\n--- stdin ---\n" + piped
                if not cfg.get("auto_approve"):
                    eprint(
                        yellow(
                            "note : stdin non interactif (utilisé comme contexte) — "
                            "les actions nécessitant confirmation seront refusées ; "
                            "ajoute --yolo si le prompt doit écrire/modifier/exécuter."
                        )
                    )
        one_shot(cfg, provider, prompt_text)
    else:
        repl(cfg, provider, check_update=not args.no_update, resume=args.cont)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        eprint()
        sys.exit(130)
