#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
chip - a tiny AI coding agent for the Pocket C.H.I.P (and other small ARM boxes).

Design goals:
  * Pure Python 3 standard library. No pip, no build step, no dependencies.
  * Single self-contained file: copy it to the device and run.
  * Works with any OpenAI-compatible chat API (OpenCode Zen, Kimi/Moonshot,
    OpenAI, local llama.cpp servers, ...).
  * Frugal with RAM and screen space (built for 512 MB / 480x272 displays).

Requires Python 3.6+ (uses f-strings). Check with:  python3 --version
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import textwrap
import urllib.error
import urllib.request

APP_NAME = "chip"
VERSION = "0.1.0"

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
}


def config_path():
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, "chip-ai", "config.json")


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
    if os.environ.get("CHIP_PROVIDER"):
        cfg["provider"] = os.environ["CHIP_PROVIDER"]
    if os.environ.get("CHIP_MODEL"):
        prov = cfg["providers"].setdefault(cfg["provider"], {})
        prov["model"] = os.environ["CHIP_MODEL"]
    if os.environ.get("CHIP_BASE_URL"):
        prov = cfg["providers"].setdefault(cfg["provider"], {})
        prov["base_url"] = os.environ["CHIP_BASE_URL"]
    return cfg


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
        key = os.environ.get(env_name) or os.environ.get("CHIP_API_KEY")
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
    "You are chip, a concise AI coding assistant running in a terminal on a small "
    "ARM computer (a Pocket C.H.I.P). Keep answers short and to the point; the "
    "screen is tiny. You can inspect and modify the local project using the "
    "provided tools: read_file, write_file, str_replace, list_dir, run_bash. "
    "Prefer str_replace for small edits and write_file for new files. Before "
    "editing a file, read it. Explain what you did in one or two sentences. Do not "
    "invent file contents — read them first."
)


# ---------------------------------------------------------------------------
# HTTP / model call
# ---------------------------------------------------------------------------
def http_chat(provider, messages, tools, stream, timeout):
    url = provider["base_url"].rstrip("/") + "/chat/completions"
    payload = {
        "model": provider["model"],
        "messages": messages,
        "stream": bool(stream),
    }
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
        raise RuntimeError(f"HTTP {e.code} from {url}\n{detail[:800]}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"connection failed to {url}: {e.reason}")

    if stream:
        return _read_stream(resp)
    return _read_full(resp)


def _read_full(resp):
    obj = json.loads(resp.read().decode("utf-8", errors="replace"))
    msg = obj["choices"][0]["message"]
    content = msg.get("content") or ""
    if content:
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
    return content, tool_calls


def _read_stream(resp):
    content_parts = []
    slots = {}  # index -> {id, name, arguments}
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
        choices = obj.get("choices") or []
        if not choices:
            continue
        delta = choices[0].get("delta") or {}
        piece = delta.get("content")
        if piece:
            sys.stdout.write(piece)
            sys.stdout.flush()
            content_parts.append(piece)
            printed_any = True
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
    return content, tool_calls


# ---------------------------------------------------------------------------
# Agent loop
# ---------------------------------------------------------------------------
def confirm(prompt):
    try:
        ans = input(yellow(prompt + " [y/N] ")).strip().lower()
    except EOFError:
        return False
    return ans in ("y", "yes", "o", "oui")


def run_tool(name, arguments_str, auto_approve):
    if name not in TOOLS:
        return f"ERROR: unknown tool '{name}'"
    try:
        args = json.loads(arguments_str) if arguments_str.strip() else {}
    except Exception as e:
        return f"ERROR: could not parse arguments: {e}"

    fn, needs_confirm, verb = TOOLS[name]

    # show what is about to happen
    target = args.get("path") or args.get("command") or ""
    eprint(dim(f"  → {verb} {target}"[: term_width() - 1]))

    if needs_confirm and not auto_approve:
        if not confirm(f"    allow {name}?"):
            return "User declined this action."
    return fn(args)


def agent_turn(cfg, provider, messages):
    max_steps = int(cfg.get("max_steps", 25))
    stream = bool(cfg.get("stream", True))
    timeout = int(cfg.get("request_timeout", 180))
    auto = bool(cfg.get("auto_approve", False))

    for _ in range(max_steps):
        content, tool_calls = http_chat(
            provider, messages, TOOL_SCHEMA, stream, timeout
        )

        if not tool_calls:
            messages.append({"role": "assistant", "content": content})
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
            result = run_tool(tc["name"], tc["arguments"], auto)
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": result,
                }
            )
    eprint(red(f"stopped after {max_steps} steps (max_steps reached)."))


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
  /tools           list available tools
  /exit, /quit     leave (Ctrl-D also works)
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
    print(dim("type /help for commands, Ctrl-D to quit"))


def repl(cfg, provider):
    try:
        import readline  # noqa: F401  (enables line editing/history if available)
    except Exception:
        pass

    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    print_banner(provider, cfg)

    while True:
        try:
            line = input(bold(green("\nchip> ")))
        except EOFError:
            print()
            break
        except KeyboardInterrupt:
            print()
            continue

        line = line.strip()
        if not line:
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
                messages = [{"role": "system", "content": SYSTEM_PROMPT}]
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
                    except Exception as e:
                        eprint(red(str(e)))
                print(dim(os.getcwd()))
            elif cmd == "/tools":
                for name in TOOLS:
                    print(dim("  " + name))
            else:
                eprint(red(f"unknown command {cmd} (try /help)"))
            continue

        messages.append({"role": "user", "content": line})
        try:
            agent_turn(cfg, provider, messages)
        except RuntimeError as e:
            eprint(red(str(e)))
        except KeyboardInterrupt:
            eprint(dim("\n(interrupted)"))


# ---------------------------------------------------------------------------
# One-shot mode
# ---------------------------------------------------------------------------
def one_shot(cfg, provider, prompt):
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": prompt},
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
    parser.add_argument("--version", action="store_true")
    args = parser.parse_args(argv)

    if args.version:
        print(f"{APP_NAME} {VERSION}")
        return
    if args.config:
        print(config_path())
        return

    cfg = load_config()
    if args.no_stream:
        cfg["stream"] = False
    if args.yolo:
        cfg["auto_approve"] = True

    provider = resolve_provider(cfg, args.provider)
    if args.model:
        provider["model"] = args.model
    if args.base_url:
        provider["base_url"] = args.base_url

    if not provider.get("api_key"):
        env_name = provider.get("api_key_env", "CHIP_API_KEY")
        eprint(
            yellow(
                f"note: no API key found. Set {env_name} (or CHIP_API_KEY), e.g.\n"
                f"  export {env_name}=sk-..."
            )
        )

    if args.prompt:
        one_shot(cfg, provider, " ".join(args.prompt))
    else:
        repl(cfg, provider)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        eprint()
        sys.exit(130)
