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
import json
import os
import re
import shutil
import subprocess
import sys
import textwrap
import time
import urllib.error
import urllib.request

APP_NAME = "pia"
VERSION = "0.3.0"

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
    "max_history_messages": 40,  # trim oldest turns beyond this to bound RAM/context
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


def save_key(provider_name, key):
    """Persist an API key for a provider into the user config file (chmod 600)."""
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
        eprint(red("échec de la mise à jour : " + msg))
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


def term_width():
    try:
        w = shutil.get_terminal_size((60, 20)).columns
    except Exception:
        w = 60
    return max(20, w)


def eprint(*a):
    print(*a, file=sys.stderr)


def die(msg, code=1):
    eprint(red("error: ") + msg)
    sys.exit(code)


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


MAX_DIFF_LINES = 40  # keep previews short enough for a 480x272 screen


def make_diff(old, new, path):
    """Return a small colored unified diff string, or None if old == new."""
    diff = list(
        difflib.unified_diff(
            old.splitlines(keepends=True),
            new.splitlines(keepends=True),
            fromfile=path,
            tofile=path,
            lineterm="",
        )
    )
    if not diff:
        return None
    shown = diff[:MAX_DIFF_LINES]
    out = []
    for line in shown:
        line = line.rstrip("\n")
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
    if len(diff) > MAX_DIFF_LINES:
        out.append(dim(f"... ({len(diff) - MAX_DIFF_LINES} more lines)"))
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


def tool_read_file(args):
    path = args["path"]
    try:
        with open(path, "r", errors="replace") as f:
            data = f.read()
    except Exception as e:
        return f"ERROR reading {path}: {e}"
    return _truncate(data)


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


def tool_run_bash(args, timeout=90):
    cmd = args["command"]
    try:
        proc = subprocess.run(
            cmd,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
        )
        out = proc.stdout.decode("utf-8", errors="replace")
        return _truncate(f"[exit {proc.returncode}]\n{out}")
    except subprocess.TimeoutExpired:
        return f"ERROR: command timed out after {timeout}s"
    except Exception as e:
        return f"ERROR running command: {e}"


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


def build_system_prompt():
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
    headers = {
        "Content-Type": "application/json",
        "Accept": "text/event-stream" if stream else "application/json",
    }
    if provider.get("api_key"):
        headers["Authorization"] = "Bearer " + provider["api_key"]

    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        msg = f"HTTP {e.code} from {url}\n{detail[:800]}"
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
                sys.stdout.write(piece)
                sys.stdout.flush()
                printed_any = True
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


def trim_history(messages, max_messages):
    """Drop the oldest whole turns once history grows past max_messages.

    A "turn" starts at a user message, so tool_call/tool-result pairs are
    never split apart. Keeps RAM and API context bounded on long sessions.
    """
    if max_messages <= 0:
        return
    system = messages[:1] if messages and messages[0].get("role") == "system" else []
    rest = messages[len(system):]
    while len(rest) > max_messages and rest:
        rest.pop(0)
        while rest and rest[0].get("role") != "user":
            rest.pop(0)
    messages[:] = system + rest


def agent_turn(cfg, provider, messages):
    max_steps = int(cfg.get("max_steps", 25))
    stream = bool(cfg.get("stream", True))
    timeout = int(cfg.get("request_timeout", 180))
    max_history = int(cfg.get("max_history_messages", 40))
    usage_total = cfg.setdefault("_usage", {"prompt": 0, "completion": 0, "total": 0})

    trim_history(messages, max_history)

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
                eprint(
                    dim(
                        f"  [{usage.get('prompt_tokens', 0)}+"
                        f"{usage.get('completion_tokens', 0)} tok this turn, "
                        f"{usage_total['total']} total this session]"
                    )
                )
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
    headers = {"Accept": "application/json"}
    if provider.get("api_key"):
        headers["Authorization"] = "Bearer " + provider["api_key"]
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            obj = json.loads(resp.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HTTP {e.code} depuis {url}")
    except Exception as e:
        raise RuntimeError(f"échec de la requête vers {url}: {e}")
    items = obj.get("data") if isinstance(obj, dict) else obj
    ids = []
    for it in items or []:
        if isinstance(it, dict) and it.get("id"):
            ids.append(it["id"])
        elif isinstance(it, str):
            ids.append(it)
    return sorted(ids)


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
HELP = """\
commands:
  /help            show this help
  /reset           clear the conversation
  /model [name]    show or switch model
  /provider [name] show or switch provider
  /yolo            toggle auto-approve for write/edit/run
  /cwd [dir]       show or change working directory
  /update          check the git repo for updates and offer to install
  /save [name]     save the conversation (default: "manual")
  /load [name]     load a saved conversation (default: "manual")
  /sessions        list saved conversations
  /usage           show tokens used this session
  /tools           list available tools
  /models          list the models this provider offers
  /compact         replace history with a short summary (frees RAM)
  /undo            revert the last file pia changed
  /diff            show `git diff`
  /commit          write a commit message for the staged diff, then commit
  /init            generate a PIA.md describing this project
  /commands        list your custom commands
  /exit, /quit     leave (Ctrl-D also works)
Also:
  !cmd             run a shell command directly (no tokens spent)
  @path/to/file    attach a file's contents to your message
  line ending in \\ continues on the next line
Type anything else to talk to the model."""


def print_banner(provider, cfg):
    print(bold(cyan(f"{APP_NAME}")) + dim(f" v{VERSION}"))
    key_state = green("key set") if provider.get("api_key") else red("no api key")
    print(
        dim(
            f"provider={provider['name']} model={provider['model']} "
            f"({key_state})  auto_approve={cfg.get('auto_approve')}"
        )
    )
    try:
        batt = battery_status()
    except Exception:
        batt = None
    if batt:
        cap, status = batt
        label = f"batt={cap}%"
        if status:
            label += f" ({status})"
        print(dim(label))
    print(dim("type /help for commands, Ctrl-D to quit"))


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
                if arg:
                    provider["model"] = arg
                print(dim(f"model = {provider['model']}"))
            elif cmd == "/provider":
                if arg:
                    provider = resolve_provider(cfg, arg)
                    if not provider.get("api_key"):
                        eprint(red(f"warning: no api key for provider {arg}"))
                print(dim(f"provider = {provider['name']} model = {provider['model']}"))
            elif cmd == "/yolo":
                cfg["auto_approve"] = not cfg.get("auto_approve", False)
                print(dim(f"auto_approve = {cfg['auto_approve']}"))
            elif cmd == "/cwd":
                if arg:
                    try:
                        os.chdir(os.path.expanduser(arg))
                        # the project context file may differ in the new dir
                        if messages and messages[0].get("role") == "system":
                            messages[0]["content"] = build_system_prompt()
                    except Exception as e:
                        eprint(red(str(e)))
                print(dim(os.getcwd()))
            elif cmd == "/update":
                if not offer_update(cfg, restart_argv=[a for a in sys.argv[1:]]):
                    print(dim("déjà à jour (ou vérification impossible)."))
            elif cmd == "/save":
                try:
                    path = save_session(messages, arg or "manual")
                    print(dim(f"session sauvegardée : {path}"))
                except Exception as e:
                    eprint(red(str(e)))
            elif cmd == "/load":
                try:
                    messages = load_session(arg or "manual")
                    print(dim(f"session chargée ({len(messages)} messages)."))
                except Exception as e:
                    eprint(red(str(e)))
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
            elif cmd == "/tools":
                for name in TOOLS:
                    print(dim("  " + name))
            elif cmd == "/models":
                try:
                    ids = fetch_models(provider)
                    if ids:
                        for i in ids:
                            marker = " *" if i == provider["model"] else ""
                            print(dim("  " + i + marker))
                    else:
                        print(dim("(aucun modèle renvoyé)"))
                except RuntimeError as e:
                    eprint(red(str(e)))
            elif cmd == "/compact":
                ok, info = compact_history(cfg, provider, messages)
                if ok:
                    print(dim("historique compacté :"))
                    print(wrap(info))
                else:
                    eprint(red(info))
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
                    eprint(red(str(e)))
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
                    eprint(red(str(e)))
                except KeyboardInterrupt:
                    eprint(dim("\n(interrupted)"))
            else:
                eprint(red(f"unknown command {cmd} (try /help)"))
            continue

        messages.append({"role": "user", "content": expand_mentions(line)})
        try:
            agent_turn(cfg, provider, messages)
        except RuntimeError as e:
            eprint(red(str(e)))
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
        metavar="KEY",
        help="save an API key for the selected provider into the config, then exit",
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
        path = save_key(prov_name, args.set_key)
        print(green(f"clé enregistrée pour '{prov_name}' dans {path}"))
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
