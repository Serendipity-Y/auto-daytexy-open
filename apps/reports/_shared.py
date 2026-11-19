# -*- coding: utf-8 -*-
"""Shared utilities for the reports application (daily_report and weekly_report)."""
import json
import errno
import os
import sys
import tempfile
import threading
import time

from openai import APIConnectionError, APIStatusError
from pathlib import Path
from collections.abc import Callable
from types import ModuleType

import pyperclip

try:
    import readline as _readline
    readline: ModuleType | None = _readline
except ImportError:
    readline = None

try:
    from prompt_toolkit import prompt as _prompt_toolkit_prompt
    prompt_toolkit_prompt: Callable[..., str] | None = _prompt_toolkit_prompt
except ImportError:
    prompt_toolkit_prompt = None


# -- environment / config ---------------------------------------------------

def get_env_value(*names, default=""):
    for name in names:
        value = os.getenv(name, "").strip()
        if value:
            return value
    return default


BASE_URL = get_env_value(
    "DAILY_REPORT_BASE_URL",
    "SILICONFLOW_BASE_URL",
    default="https://api.siliconflow.cn/v1",
)
DEFAULT_MODEL_NAME = "Qwen/Qwen3.5-35B-A3B"
MODEL_NAME = get_env_value(
    "DAILY_REPORT_MODEL",
    default=DEFAULT_MODEL_NAME,
)
def _env_positive_float(name, default):
    try:
        value = float(get_env_value(name, default=str(default)))
    except ValueError:
        return float(default)
    return value if value > 0 else float(default)


# 实测（2026-11-05）：常规一次日报润色只需 2–3 秒（约 120 个输出 token），
# 但服务端偶发排队可达数分钟。总预算保证最坏等待有上限，单次上限保证
# 卡住的那次请求能被尽早放弃、换新连接重试，而不是一次吃满整个预算。
REQUEST_TIMEOUT_SECONDS = _env_positive_float("DAILY_REPORT_AI_TIMEOUT", 120)
AI_ATTEMPT_TIMEOUT_SECONDS = _env_positive_float("DAILY_REPORT_AI_ATTEMPT_TIMEOUT", 45)
AI_HEARTBEAT_SECONDS = 15.0
DEFAULT_API_CREDENTIALS_PATH = Path("~/.config/auto-daytexy/api_credentials.json").expanduser()
APPLE_TERMINAL_CLIPBOARD_COMMAND = "/clipboard"


# -- API helpers ------------------------------------------------------------

def build_chat_completion_options(temperature, model_name=None, max_tokens=None):
    selected_model = model_name or MODEL_NAME
    options = {
        "model": selected_model,
        "temperature": temperature,
    }
    if max_tokens is not None:
        options["max_tokens"] = max_tokens
    if selected_model == DEFAULT_MODEL_NAME:
        options["extra_body"] = {"enable_thinking": False}
    return options


def _create_with_heartbeat(client, options, attempt_timeout, attempt):
    """One blocking request, with an elapsed-time line so a slow wait looks alive."""
    started = time.monotonic()
    done = threading.Event()

    def report():
        while not done.wait(AI_HEARTBEAT_SECONDS):
            emit_terminal_message(
                f"[AI] 模型已等待 {time.monotonic() - started:.0f} 秒"
                f"（本次上限 {attempt_timeout:.0f} 秒，第 {attempt}/3 次）…"
            )

    watcher = threading.Thread(target=report, daemon=True)
    watcher.start()
    try:
        return client.chat.completions.create(**options, timeout=attempt_timeout)
    finally:
        done.set()


def request_chat_completion(client, options, *, timeout_seconds=REQUEST_TIMEOUT_SECONDS):
    """One retry owner and a shared waiting budget for a logical AI request."""
    deadline = time.monotonic() + timeout_seconds
    for attempt in range(1, 4):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeError("AI 请求等待预算已用完，请稍后重试。")
        attempt_timeout = min(remaining, AI_ATTEMPT_TIMEOUT_SECONDS)
        try:
            return _create_with_heartbeat(client, options, attempt_timeout, attempt)
        except (APIConnectionError, APIStatusError) as exc:
            status = getattr(exc, "status_code", None)
            transient = isinstance(exc, APIConnectionError) or status in {408, 409, 429} or (isinstance(status, int) and status >= 500)
            if not transient:
                raise RuntimeError(f"AI 服务拒绝请求（HTTP {status}），请检查模型、凭据或请求配置。") from exc
            if attempt == 3:
                raise RuntimeError("AI 服务暂时不可用（已尝试3次），请稍后重试。") from exc
            delay = 1.5 * attempt
            if deadline - time.monotonic() <= delay:
                raise RuntimeError("AI 请求等待预算已用完，请稍后重试。") from exc
            emit_terminal_message(
                f"[AI] 第 {attempt} 次 {attempt_timeout:.0f} 秒未返回，"
                f"已换新连接重试（{attempt + 1}/3）；预算剩余 {max(deadline - time.monotonic(), 0):.0f} 秒。"
            )
            time.sleep(delay)
    raise AssertionError("unreachable")


def load_api_key_from_credentials(*keys, credentials_path=None):
    path = Path(credentials_path).expanduser() if credentials_path else DEFAULT_API_CREDENTIALS_PATH
    if not path.exists():
        return ""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return ""
    if not isinstance(payload, dict):
        return ""
    for key in keys:
        value = str(payload.get(key, "") or "").strip()
        if value:
            return value
    return ""


def write_text_atomically(path, text, encoding="utf-8"):
    """Replace a local text file without exposing a partially written target."""
    target = Path(path).expanduser().resolve(strict=False)
    target.parent.mkdir(parents=True, exist_ok=True)
    existing_mode = target.stat().st_mode & 0o777 if target.exists() else 0o644
    descriptor, temp_name = tempfile.mkstemp(
        prefix=f".{target.name}.",
        suffix=".tmp",
        dir=str(target.parent),
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(descriptor, "w", encoding=encoding) as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_path, existing_mode)
        os.replace(temp_path, target)
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise


def extract_message_content(message_content):
    if isinstance(message_content, str):
        return message_content.strip()

    if isinstance(message_content, list):
        parts = []
        for item in message_content:
            if isinstance(item, str):
                parts.append(item)
                continue
            if isinstance(item, dict):
                text = item.get("text") or item.get("content") or item.get("value")
                if isinstance(text, str):
                    parts.append(text)
                continue
            text = getattr(item, "text", None) or getattr(item, "content", None)
            if isinstance(text, str):
                parts.append(text)
        return "\n".join(parts).strip()

    return str(message_content or "").strip()


# -- terminal / I/O ---------------------------------------------------------

def emit_terminal_message(message, file=None, flush=True):
    """Display human progress without failing work when its output sink closes."""
    stream = sys.stdout if file is None else file
    try:
        print(message, file=stream, flush=flush)
    except ValueError:
        if getattr(stream, "closed", False) is not True:
            raise
        return False
    except OSError as exc:
        if not isinstance(exc, BrokenPipeError) and exc.errno not in {errno.EPIPE, errno.EIO, errno.ENXIO, errno.EBADF}:
            raise
        if stream is sys.stdout or stream is sys.stderr:
            # Detach the lost descriptor so shutdown does not retry a broken
            # buffered flush. Machine-readable result files are not routed here.
            null_fd = None
            try:
                null_fd = os.open(os.devnull, os.O_WRONLY)
                os.dup2(null_fd, stream.fileno())
            except (OSError, ValueError):
                pass
            finally:
                if null_fd is not None:
                    os.close(null_fd)
        return False
    return True


def is_libedit_readline():
    if not readline:
        return False
    return "libedit" in (getattr(readline, "__doc__", "") or "").lower()


def configure_readline():
    if not readline:
        return

    if is_libedit_readline():
        return

    try:
        readline.parse_and_bind("set editing-mode emacs")
        readline.parse_and_bind("set enable-bracketed-paste on")
        readline.parse_and_bind(r'"\e[3~": delete-char')
    except Exception:
        pass


def configure_stdio():
    for stream_name in ("stdin", "stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if stream is not None and hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass

    configure_readline()


def is_apple_terminal():
    """Whether input is running in the macOS built-in Terminal application."""
    return (
        sys.platform == "darwin"
        and get_env_value("TERM_PROGRAM").lower() == "apple_terminal"
    )


def should_use_prompt_toolkit():
    if prompt_toolkit_prompt is None:
        return False
    if get_env_value("DAILY_REPORT_DISABLE_PROMPT_TOOLKIT"):
        return False
    return sys.stdin.isatty() and sys.stdout.isatty()


def thread_has_running_event_loop():
    """Whether asyncio already reports a running loop for the current thread."""
    try:
        import asyncio
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


def read_brief_input(prompt_text):
    """Read one answer, preserving EOF and interruption instead of accepting defaults."""
    if "\n" in prompt_text:
        preview, _, prompt_text = prompt_text.rpartition("\n")
        sys.stdout.write(preview + "\n")
        sys.stdout.flush()
    if sys.stdin.isatty():
        # prompt_toolkit renders every prompt through asyncio.run(); it cannot run
        # while the current thread already reports a loop (Playwright's sync API
        # keeps one alive for the whole browser session). Choose the renderer up
        # front instead of failing mid-answer: those prompts are short answers,
        # and the builtin input keeps Ctrl-C/EOF behaviour identical.
        if (
            should_use_prompt_toolkit()
            and prompt_toolkit_prompt is not None
            and not thread_has_running_event_loop()
        ):
            # A failed renderer must not silently switch to libedit mid-answer.
            value = prompt_toolkit_prompt(prompt_text, multiline=False, wrap_lines=True).strip()
        else:
            value = input(prompt_text).strip()
        if is_apple_terminal() and value == APPLE_TERMINAL_CLIPBOARD_COMMAND:
            clipboard_text = read_clipboard_text()
            if clipboard_text:
                return clipboard_text
            print("[提示] 剪贴板没有可用文本。")
            return ""
        return value

    sys.stdout.write(prompt_text)
    sys.stdout.flush()
    value = sys.stdin.readline()
    if value == "":
        raise EOFError("输入已结束。")
    return value.strip()


# -- clipboard --------------------------------------------------------------

def copy_to_clipboard(text):
    try:
        pyperclip.copy(text)
        return True
    except pyperclip.PyperclipException:
        return False


def read_clipboard_text():
    try:
        return str(pyperclip.paste() or "").strip()
    except pyperclip.PyperclipException:
        return ""


# -- CLI helpers ------------------------------------------------------------

def should_auto_confirm(args, stdin_isatty=None):
    if args.yes:
        return True

    if stdin_isatty is None:
        stdin_isatty = sys.stdin.isatty()

    return not stdin_isatty
