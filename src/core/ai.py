from __future__ import annotations

import ast
import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

from src.core.config import (
    AI_BACKEND,
    ALLOW_BACKEND_FALLBACK,
    CLAUDE_ARGS,
    CLAUDE_CMD,
    CODEX_ARGS,
    CODEX_CMD,
    DATA_DIR,
    CREDIT_LIMIT_KEYWORDS,
    RATE_LIMIT_KEYWORDS,
    CreditLimitError,
    AI_TIMEOUT_MULTIPLIER,
    AI_TIMEOUT_MAX_SECONDS,
    OPENROUTER_API_KEY,
    OPENROUTER_MODEL,
    OPENROUTER_BASE_URL,
    _is_unusable_cross_os_cli_path,
)


# ── Constants ────────────────────────────────────────────────
_RATE_LIMIT_BUFFER_SECONDS = 5
_MAX_AUTO_WAIT_CYCLES = 1
_ACTIVE_BACKEND = AI_BACKEND
_LAST_PROBE_RESULT: tuple[str, str] | None = None

# ── Logger ───────────────────────────────────────────────────
_LOG = logging.getLogger(__name__)

# ── Token usage tracking (char-based proxy) ──────────────────
# Guarded by _usage_lock: the daemon's PR monitor thread records usage while the
# main thread (or an MCP status tool) may read a snapshot concurrently. The lock
# keeps reads and the read-modify-write increments consistent.
_usage_lock = threading.Lock()
_usage: dict[str, int] = {"prompt_chars": 0, "response_chars": 0, "calls": 0}


# ── Exceptions ────────────────────────────────────────────────
class BackendRateLimitWaitError(RuntimeError):
    """Raised when the selected backend reports a reset time and should wait."""

    def __init__(self, message: str, wait_seconds: int, reset_at: datetime | None = None) -> None:
        super().__init__(message)
        self.wait_seconds = wait_seconds
        self.reset_at = reset_at


class BackendConfigurationError(RuntimeError):
    """Raised when the selected AI backend is not configured with a callable CLI."""


class BackendRuntimeError(RuntimeError):
    """Raised when the selected AI backend is reachable but blocked by local runtime or network issues."""


# ── Usage helpers ────────────────────────────────────────────
def reset_usage() -> None:
    global _ACTIVE_BACKEND, _LAST_PROBE_RESULT
    _ACTIVE_BACKEND = AI_BACKEND
    _LAST_PROBE_RESULT = None
    with _usage_lock:
        _usage["prompt_chars"] = 0
        _usage["response_chars"] = 0
        _usage["calls"] = 0


def get_usage() -> dict:
    with _usage_lock:
        calls = _usage["calls"]
        prompt_chars = _usage["prompt_chars"]
        response_chars = _usage["response_chars"]
    return {
        "backend": _ACTIVE_BACKEND,
        "calls": calls,
        "prompt_chars": prompt_chars,
        "response_chars": response_chars,
        "total_chars": prompt_chars + response_chars,
        "est_tokens": (prompt_chars + response_chars) // 4,
    }


def get_backend_name() -> str:
    return _ACTIVE_BACKEND


def get_backend_label() -> str:
    if _ACTIVE_BACKEND == "openrouter":
        return "OpenRouter"
    return "Codex" if _ACTIVE_BACKEND == "codex" else "Claude"


def _record_usage(prompt: str, response_text: str = "") -> None:
    with _usage_lock:
        _usage["calls"] += 1
        _usage["prompt_chars"] += len(prompt)
        _usage["response_chars"] += len(response_text)


# ── Parsing helpers ──────────────────────────────────────────
def _extract_reset_wait_seconds(text: str) -> tuple[int, datetime | None] | None:
    patterns = [
        r"resets?\s+(?P<hour>\d{1,2}):(?P<minute>\d{2})(?P<ampm>am|pm)\s*\((?P<tz>[^)]+)\)",
        r"resets?\s+(?P<hour24>\d{1,2}):(?P<minute24>\d{2})\s*\((?P<tz24>[^)]+)\)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if not match:
            continue
        tz_name = match.groupdict().get("tz") or match.groupdict().get("tz24")
        if not tz_name:
            continue
        try:
            zone = ZoneInfo(tz_name)
        except Exception:
            continue

        now = datetime.now(zone)
        minute = int(match.groupdict().get("minute") or match.groupdict().get("minute24") or 0)
        if match.groupdict().get("hour24"):
            hour = int(match.group("hour24"))
        else:
            hour = int(match.group("hour"))
            ampm = match.group("ampm")
            if ampm == "am":
                hour = 0 if hour == 12 else hour
            else:
                hour = 12 if hour == 12 else hour + 12

        reset_at = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if reset_at <= now:
            reset_at += timedelta(days=1)

        wait_seconds = max(int((reset_at - now).total_seconds()) + _RATE_LIMIT_BUFFER_SECONDS, 1)
        return wait_seconds, reset_at
    return None


def _extract_error_text(stderr_text: str, stdout_text: str) -> str:
    return stderr_text.strip() or stdout_text.strip() or "unknown AI CLI error"


def _detect_backend_runtime_issue(stderr_text: str, stdout_text: str) -> RuntimeError | None:
    combined = f"{stderr_text}\n{stdout_text}".lower()

    cert_patterns = (
        "no native root ca certificates found",
        "failed to open current user certificate store",
    )
    network_patterns = (
        "stream disconnected before completion",
        "error sending request for url",
        "failed to connect to websocket",
        "unable to connect to proxy",
        "proxyerror",
        "failed to establish a new connection",
    )
    filesystem_patterns = (
        "permission denied",
        "access is denied",
        "attempt to write a readonly database",
        "cannot access session files",
        "failed to create temporary plugin cache directory",
        "thread/start failed",
        "uv_spawn 'c:\\windows\\system32\\reg.exe'",
        "eperm: operation not permitted",
    )

    if any(pattern in combined for pattern in cert_patterns):
        return BackendRuntimeError(
            "Codex runtime cannot load Windows root certificates, so TLS connections to the API fail. "
            "This machine needs certificate-store access or a backend that can reach api.openai.com with a valid CA bundle."
        )
    if any(pattern in combined for pattern in network_patterns):
        return BackendRuntimeError(
            "Codex runtime cannot maintain a stable network connection to the API. "
            "Check proxy, firewall, and outbound HTTPS access for api.openai.com/chatgpt.com."
        )
    if any(pattern in combined for pattern in filesystem_patterns):
        return BackendRuntimeError(
            "Codex runtime is blocked by local filesystem permissions. "
            "Check write access for the configured CODEX_HOME/session/temp directories."
        )
    return None


def _extract_first_json_object(text: str) -> str | None:
    """Return the first balanced top-level {...} object, ignoring surrounding prose.

    Brace-aware and string-aware so nested objects and braces inside string
    literals do not break the match. Reasoning models (and OpenRouter responses)
    often wrap the JSON in explanation text; this recovers it.
    """
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    in_str = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


def _parse_json(raw: str) -> dict:
    text = raw.strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fenced:
        text = fenced.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # Reasoning models may prepend/append prose around the JSON — recover the object.
    obj = _extract_first_json_object(text)
    if obj is not None:
        return json.loads(obj)
    return json.loads(text)  # re-raise the original decode error


def _strip_fences(text: str) -> str:
    text = text.strip()
    text = re.sub(r"^```(?:python|py)?\s*\n?", "", text)
    text = re.sub(r"\n?```\s*$", "", text)
    return text.strip()


def _rescue_code_from_prose(text: str) -> str:
    """Extract the largest Python code block from a prose/apology response."""
    # Prefer explicitly-labelled python blocks
    fenced = re.findall(r"```(?:python|py)\s*\n(.*?)```", text, re.DOTALL)
    if not fenced:
        fenced = re.findall(r"```\s*\n(.*?)```", text, re.DOTALL)
    if fenced:
        return max(fenced, key=len).strip()
    # Fallback: strip prose lines — keep only lines that look like Python
    _PROSE_RE = re.compile(
        r"^(?:here|note|this|the|i |we |you |below|above|sure|of course|certainly|"
        r"please|feel free|let me|as requested|as you|explanation|output|result)",
        re.IGNORECASE,
    )
    lines = text.splitlines()
    code_lines = [l for l in lines if not _PROSE_RE.match(l.strip()) or l.startswith(" ") or l.startswith("\t")]
    candidate = "\n".join(code_lines).strip()
    if candidate and _syntax_ok(candidate):
        return candidate
    return ""


def _syntax_ok(content: str) -> bool:
    try:
        ast.parse(content)
        return True
    except SyntaxError:
        return False
    except Exception:
        return False


def _syntax_ok_ts(content: str) -> bool:
    """TypeScript syntax check — always fail-open.

    tsc exits 1 for both type errors AND syntax errors, making them
    indistinguishable without parsing the error output. Failing open avoids
    false rejects on valid TS with missing type context (imports, globals).
    The diff check and maintainer CI are the real safety nets for TS files.
    """
    return True


def get_scaled_timeout(base_timeout: int, attempt: int) -> int:
    """Return a stable timeout budget for AI CLI calls.

    First attempts get a small global multiplier to avoid wasting credits on
    partial outputs that time out. Retries grow moderately and are capped so a
    stuck CLI cannot hang the builder forever.
    """
    scale_factors = [1.0, 1.6, 2.2]
    factor = scale_factors[min(max(attempt, 1) - 1, len(scale_factors) - 1)]
    timeout = int(base_timeout * factor * AI_TIMEOUT_MULTIPLIER)
    return min(max(timeout, 45), AI_TIMEOUT_MAX_SECONDS)


# ── Streaming helpers ────────────────────────────────────────
def _read_stdout(proc_stdout, chunks: list[str], stream_path: Path | None) -> None:
    """Background thread: read stdout line by line, stream to file if given."""
    f = None
    if stream_path:
        try:
            stream_path.parent.mkdir(parents=True, exist_ok=True)
            f = open(stream_path, "w", encoding="utf-8")
        except Exception:
            pass
    try:
        for line in proc_stdout:
            decoded = line.decode("utf-8", errors="replace")
            chunks.append(decoded)
            if f:
                f.write(decoded)
                f.flush()
    finally:
        if f:
            try:
                f.close()
            except Exception:
                pass


def _read_stderr(proc_stderr, chunks: list[str]) -> None:
    """Background thread: collect stderr."""
    for line in proc_stderr:
        chunks.append(line.decode("utf-8", errors="replace"))


# ── Backend execution ────────────────────────────────────────
def _get_backend_command() -> tuple[str, list[str]]:
    if _ACTIVE_BACKEND == "codex":
        return CODEX_CMD, CODEX_ARGS
    return CLAUDE_CMD, CLAUDE_ARGS


def _quote_pwsh_arg(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _build_command(argv: list[str]) -> list[str]:
    if _ACTIVE_BACKEND not in {"codex", "claude"}:
        return argv

    if _ACTIVE_BACKEND == "codex" and "exec" in argv and "-C" not in argv and "--cd" not in argv:
        exec_index = argv.index("exec")
        argv = argv[: exec_index + 1] + ["-C", str(Path.cwd())] + argv[exec_index + 1 :]

    if os.name != "nt":
        return argv

    pwsh_exe = shutil.which("pwsh") or "pwsh"
    script = " ".join(_quote_pwsh_arg(part) for part in argv)
    return [pwsh_exe, "-Command", f"& {script}"]


def _prepare_invocation(prompt: str) -> tuple[list[str], bytes | None]:
    backend_cmd, backend_args = _get_backend_command()
    if _ACTIVE_BACKEND == "codex":
        spawn_argv = _build_command([backend_cmd, *backend_args])
        return spawn_argv, prompt.encode("utf-8")

    claude_args = [arg for arg in backend_args if arg != "-"]
    claude_prompt = prompt if len(prompt) <= 8000 else _materialize_claude_prompt(prompt)
    spawn_argv = _build_command([backend_cmd, *claude_args, claude_prompt])
    return spawn_argv, None


def _build_backend_env() -> dict[str, str]:
    env = os.environ.copy()
    if _ACTIVE_BACKEND != "codex":
        return env

    # Preserve the user's logged-in Codex home by default. Forcing a repo-local
    # CODEX_HOME breaks auth because the login state typically lives in the
    # default user profile directory.
    codex_home_override = os.getenv("CODEX_HOME_OVERRIDE", "").strip()
    if codex_home_override:
        codex_home = Path(codex_home_override)
        for subdir in ("sessions", "plugins", "tmp", "logs"):
            (codex_home / subdir).mkdir(parents=True, exist_ok=True)
        env["CODEX_HOME"] = str(codex_home)
        env.setdefault("TMP", str(codex_home / "tmp"))
        env.setdefault("TEMP", str(codex_home / "tmp"))

    for key in ("ALL_PROXY", "HTTP_PROXY", "HTTPS_PROXY", "GIT_HTTP_PROXY", "GIT_HTTPS_PROXY"):
        value = env.get(key, "").strip().lower()
        if value == "http://127.0.0.1:9":
            env[key] = ""

    return env


def _has_usable_backend(backend: str) -> bool:
    if backend == "openrouter":
        return bool(OPENROUTER_API_KEY)
    if backend == "codex":
        if not CODEX_CMD:
            return False
        if _is_unusable_cross_os_cli_path(CODEX_CMD):
            return False
        found = shutil.which(CODEX_CMD) if CODEX_CMD else None
        if found and _is_unusable_cross_os_cli_path(found):
            found = None
        fallback = shutil.which("codex")
        if fallback and _is_unusable_cross_os_cli_path(fallback):
            fallback = None
        return Path(CODEX_CMD).exists() or found is not None or fallback is not None
    if backend == "claude":
        if not CLAUDE_CMD:
            return False
        if _is_unusable_cross_os_cli_path(CLAUDE_CMD):
            return False
        found = shutil.which(CLAUDE_CMD) if CLAUDE_CMD else None
        if found and _is_unusable_cross_os_cli_path(found):
            found = None
        fallback = shutil.which("claude")
        if fallback and _is_unusable_cross_os_cli_path(fallback):
            fallback = None
        return Path(CLAUDE_CMD).exists() or found is not None or fallback is not None
    return False


def _fallback_backend_candidates() -> list[str]:
    return [backend for backend in ("codex", "claude") if backend != _ACTIVE_BACKEND]


def _switch_backend(new_backend: str) -> None:
    global _ACTIVE_BACKEND
    _ACTIVE_BACKEND = new_backend


def _materialize_claude_prompt(prompt: str) -> str:
    prompt_dir = DATA_DIR / ".ai_prompts"
    prompt_dir.mkdir(parents=True, exist_ok=True)
    prompt_path = prompt_dir / "claude_prompt.txt"
    prompt_path.write_text(prompt, encoding="utf-8")
    return (
        "Read the full instructions from this file and follow them exactly: "
        f"{prompt_path}. Reply exactly in the format requested by that file."
    )


def _call_openrouter(prompt: str, timeout: int, stream_path: Path | None = None) -> str:
    """Call an OpenAI-compatible chat-completions API (OpenRouter by default).

    Returns the assistant message text. Raises the same exception types as the CLI
    backends so the rest of the engine handles errors uniformly.
    """
    if not OPENROUTER_API_KEY:
        raise BackendConfigurationError(
            "OPENROUTER_API_KEY is not set. Add it to .env to use AI_BACKEND=openrouter."
        )
    if not OPENROUTER_MODEL:
        raise BackendConfigurationError(
            "OPENROUTER_MODEL is not set. Set it to an OpenRouter model id "
            "(e.g. OPENROUTER_MODEL=anthropic/claude-3.5-sonnet) in .env."
        )

    url = f"{OPENROUTER_BASE_URL}/chat/completions"
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {"model": OPENROUTER_MODEL, "messages": [{"role": "user", "content": prompt}]}

    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=timeout)
    except requests.Timeout as exc:
        raise BackendRuntimeError(f"OpenRouter request timed out after {timeout}s") from exc
    except requests.RequestException as exc:
        raise BackendRuntimeError(f"OpenRouter request failed: {str(exc)[:200]}") from exc

    if resp.status_code != 200:
        body = (resp.text or "").strip()
        low = body.lower()
        if resp.status_code in (401, 403):
            raise BackendConfigurationError(
                f"OpenRouter auth failed (HTTP {resp.status_code}); check OPENROUTER_API_KEY."
            )
        if resp.status_code == 402 or any(kw in low for kw in CREDIT_LIMIT_KEYWORDS):
            _record_usage(prompt, "")
            raise CreditLimitError(f"OpenRouter credit/quota limit: {body[:200]}")
        if resp.status_code == 429 or any(kw in low for kw in RATE_LIMIT_KEYWORDS):
            _record_usage(prompt, "")
            raise RuntimeError(f"OpenRouter rate limited (temporary): {body[:200]}")
        raise BackendRuntimeError(f"OpenRouter HTTP {resp.status_code}: {body[:200]}")

    try:
        data = resp.json()
        text = (data["choices"][0]["message"]["content"] or "").strip()
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise BackendRuntimeError(
            f"OpenRouter returned an unexpected response shape: {str(exc)[:200]}"
        ) from exc

    _record_usage(prompt, text)
    if stream_path:
        try:
            stream_path.parent.mkdir(parents=True, exist_ok=True)
            stream_path.write_text(text, encoding="utf-8")
        except Exception:
            pass
    return text


def _call_backend(
    prompt: str,
    timeout: int = 600,
    auto_wait_cycles: int = 0,
    stream_path: Path | None = None,
) -> str:
    """Call the configured AI backend. Streams stdout to stream_path while generating."""
    if _ACTIVE_BACKEND == "openrouter":
        return _call_openrouter(prompt, timeout, stream_path)
    backend_label = get_backend_label()
    backend_cmd, _backend_args = _get_backend_command()
    spawn_argv, stdin_payload = _prepare_invocation(prompt)
    spawn_env = _build_backend_env()
    try:
        proc = subprocess.Popen(
            spawn_argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=spawn_env,
        )
    except FileNotFoundError as exc:
        if _ACTIVE_BACKEND == "codex":
            if _is_unusable_cross_os_cli_path(backend_cmd):
                raise BackendConfigurationError(
                    "The configured Codex command points at a Windows-mounted binary that is not usable from Linux/WSL. "
                    "Install Codex inside WSL and set CODEX_CMD=codex, or remove the Windows path override."
                ) from exc
            raise BackendConfigurationError(
                "Codex CLI command was not found. Set CODEX_CMD in .env to a working Codex CLI binary "
                "or wrapper script, then retry with --codex."
            ) from exc
        raise BackendConfigurationError(
            "Claude CLI command was not found. Set CLAUDE_CMD in .env to a working Claude CLI binary."
        ) from exc
    except PermissionError as exc:
        if _ACTIVE_BACKEND == "codex":
            if _is_unusable_cross_os_cli_path(backend_cmd):
                raise BackendConfigurationError(
                    "The configured Codex command points at a Windows-mounted binary that is not usable from Linux/WSL. "
                    "Install Codex inside WSL and set CODEX_CMD=codex, or remove the Windows path override."
                ) from exc
            raise BackendConfigurationError(
                "The detected Codex command is not callable from Python subprocesses on this machine "
                f"({backend_cmd}). This usually happens with the WindowsApps desktop alias. "
                "Set CODEX_CMD in .env to a real Codex CLI executable or wrapper script, then retry with --codex."
            ) from exc
        raise

    # Write stdin and close only for stdin-driven backends.
    if stdin_payload is not None:
        try:
            proc.stdin.write(stdin_payload)
        except BrokenPipeError as exc:
            proc.wait(timeout=5)
            stderr_text = proc.stderr.read().decode("utf-8", errors="replace") if proc.stderr else ""
            stdout_text = proc.stdout.read().decode("utf-8", errors="replace") if proc.stdout else ""
            if fatal_runtime_issue := _detect_backend_runtime_issue(stderr_text, stdout_text):
                raise fatal_runtime_issue from exc
            error_text = _extract_error_text(stderr_text, stdout_text)
            raise BackendRuntimeError(f"{backend_label} backend closed stdin early: {error_text[:300]}") from exc
    proc.stdin.close()

    # Read stdout/stderr in background threads so output streams to disk while generating
    stdout_chunks: list[str] = []
    stderr_chunks: list[str] = []
    t_out = threading.Thread(
        target=_read_stdout, args=(proc.stdout, stdout_chunks, stream_path), daemon=True
    )
    t_err = threading.Thread(
        target=_read_stderr, args=(proc.stderr, stderr_chunks), daemon=True
    )
    t_out.start()
    t_err.start()

    t_out.join(timeout)
    timed_out = t_out.is_alive()
    if timed_out:
        proc.kill()
        t_out.join(10)   # let thread flush & close stream_path file

    t_err.join(10)
    if not timed_out:
        proc.wait()

    if timed_out:
        raise BackendRuntimeError(f"{get_backend_label()} backend timed out after {timeout}s")

    stdout_text = "".join(stdout_chunks).strip()
    stderr_text = "".join(stderr_chunks)

    if fatal_runtime_issue := _detect_backend_runtime_issue(stderr_text, stdout_text):
        raise fatal_runtime_issue

    # Only inspect stderr (+ stdout when stderr empty) for CLI errors.
    # Never scan prompt-bearing stdout unless stderr is empty; otherwise prompt
    # text can create false positives for "rate limit" or similar keywords.
    if proc.returncode != 0:
        error_text = _extract_error_text(stderr_text, stdout_text)
        err_lower = (stderr_text if stderr_text.strip() else stdout_text).lower()

        if "you've hit your limit" in err_lower and (reset_wait := _extract_reset_wait_seconds(err_lower)):
            wait_seconds, reset_at = reset_wait
            if auto_wait_cycles >= _MAX_AUTO_WAIT_CYCLES:
                _record_usage(prompt, stdout_text)
                raise CreditLimitError(error_text[:300])
            reset_text = reset_at.strftime("%Y-%m-%d %H:%M:%S %Z") if reset_at else "the reported reset time"
            _LOG.warning(
                "%s rate limit hit: %s. Sleeping for %ss until %s before retrying.",
                backend_label, error_text, wait_seconds, reset_text,
            )
            _record_usage(prompt, stdout_text)
            time.sleep(wait_seconds)
            return _call_backend(prompt, timeout=timeout, auto_wait_cycles=auto_wait_cycles + 1)

        if any(kw in err_lower for kw in CREDIT_LIMIT_KEYWORDS):
            _record_usage(prompt, stdout_text)
            raise CreditLimitError(f"{backend_label} credit/quota limit detected: {error_text[:300]}")
        if any(kw in err_lower for kw in RATE_LIMIT_KEYWORDS):
            _record_usage(prompt, stdout_text)
            raise RuntimeError(f"{backend_label} rate limited (temporary): {error_text[:200]}")
        _record_usage(prompt, stdout_text)
        raise RuntimeError(f"{_ACTIVE_BACKEND} CLI failed:\n{error_text}")

    _record_usage(prompt, stdout_text)
    return stdout_text


def call_ai(prompt: str, timeout: int = 600, stream_path: Path | None = None) -> str:
    original_backend = _ACTIVE_BACKEND
    try:
        return _call_backend(prompt, timeout=timeout, stream_path=stream_path)
    except (BackendConfigurationError, BackendRuntimeError) as exc:
        primary_backend = _ACTIVE_BACKEND
        if ALLOW_BACKEND_FALLBACK:
            fallback_errors: list[str] = []
            for fallback_backend in _fallback_backend_candidates():
                if not _has_usable_backend(fallback_backend):
                    continue
                _LOG.warning(
                    "%s backend unavailable (%s). Falling back to %s for this run.",
                    get_backend_label(),
                    exc,
                    "Codex" if fallback_backend == "codex" else "Claude",
                )
                _switch_backend(fallback_backend)
                try:
                    return _call_backend(prompt, timeout=timeout, stream_path=stream_path)
                except (BackendConfigurationError, BackendRuntimeError) as fallback_exc:
                    fallback_errors.append(f"{fallback_backend}: {fallback_exc}")
            if fallback_errors:
                raise BackendRuntimeError(
                    f"{primary_backend} backend unavailable: {exc} | fallback(s) unavailable: {' | '.join(fallback_errors)}"
                ) from exc
        raise
    finally:
        _switch_backend(original_backend)


def probe_backend(timeout: int = 20) -> str:
    global _LAST_PROBE_RESULT
    cache_key = f"{AI_BACKEND}|{_ACTIVE_BACKEND}"
    if _LAST_PROBE_RESULT and _LAST_PROBE_RESULT[0] == cache_key:
        return _LAST_PROBE_RESULT[1]

    response = call_ai("Reply with exactly OK", timeout=timeout).strip()
    _LAST_PROBE_RESULT = (cache_key, response)
    return response
