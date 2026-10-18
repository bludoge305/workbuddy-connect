"""Local OpenAI-compatible bridge for WorkBuddy models.

ZCode's model picker only understands providers declared in its personal
provider config (``~/.zcode/v2/provider_config.json``). This module exposes the
signed-in WorkBuddy session as a loopback-only OpenAI-compatible endpoint so a
normal ``openai-chat-completions`` provider can point at it:

    GET  /v1/models
    POST /v1/chat/completions   (streaming and non-streaming)

The bridge forwards the whole request body (tools, tool_choice, images,
reasoning_effort, ...) to the upstream and re-emits its SSE stream. It binds to
127.0.0.1 only and requires the bearer token stored in ``bridge.json``; the
token is generated locally and is not a WorkBuddy credential.
"""

from __future__ import annotations

import json
import os
import secrets
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterator

from . import catalog as catalog_module
from . import upstream as upstream_module
from .variants import CN, GLOBAL, VARIANTS, Variant, state_home, variant_for
DEFAULT_PORT = 38271
BRIDGE_VERSION = "0.2.0"
STATE_FILENAME = "bridge.json"
LOG_FILENAME = "bridge.log"
MAX_BODY_BYTES = 64 * 1024 * 1024
UPSTREAM_TIMEOUT_S = 600

# Fields that are meaningful to the WorkBuddy upstream. Everything else that an
# OpenAI client sends (store, metadata, stream_options, ...) is dropped rather
# than risking a rejection from the private endpoint.
FORWARD_FIELDS = (
    "model",
    "messages",
    "tools",
    "tool_choice",
    "temperature",
    "top_p",
    "top_k",
    "max_tokens",
    "stop",
    "presence_penalty",
    "frequency_penalty",
    "seed",
    "response_format",
    "reasoning_effort",
    "parallel_tool_calls",
)

EFFORT_ORDER = ("low", "medium", "high", "xhigh", "max")

# OpenAI delta keys a chat client understands. Anything else the WorkBuddy
# upstream adds (function_call, refusal, extra_fields, ...) is dropped.
DELTA_KEYS = frozenset(
    {
        "role",
        "content",
        "reasoning_content",
        "reasoning",
        "tool_calls",
        "function_call",
    }
)


def _clean_delta(delta: dict[str, Any]) -> dict[str, Any]:
    """Strip fields the upstream always attaches but OpenAI clients misread.

    The WorkBuddy stream repeats ``content: ""`` on every reasoning fragment and
    adds private keys (``function_call: null``, ``refusal: ""``, ``tool_calls:
    []``, ``extra_fields``). A client that treats an empty content as a new
    content segment ends up splitting the thinking block into dozens of pieces,
    so empty values are dropped and only OpenAI delta keys survive.
    """
    cleaned: dict[str, Any] = {}
    for key, value in delta.items():
        if key not in DELTA_KEYS:
            continue
        if value is None:
            continue
        if isinstance(value, str) and value == "":
            continue
        if isinstance(value, list) and not value:
            continue
        cleaned[key] = value
    return cleaned


def _chunk_has_payload(chunk: dict[str, Any]) -> bool:
    """True when a chunk still carries something a client can render."""
    if isinstance(chunk.get("usage"), dict):
        return True
    for choice in chunk.get("choices") or []:
        if not isinstance(choice, dict):
            continue
        if choice.get("finish_reason"):
            return True
        if choice.get("delta"):
            return True
    return False


# ---------------------------------------------------------------------------
# State file


def state_path() -> Path:
    return state_home() / STATE_FILENAME


def log_path() -> Path:
    return state_home() / LOG_FILENAME


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Unique temp name: concurrent start/stop calls would otherwise write the
    # same file and one of them fails with a sharing violation on Windows.
    tmp = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    for attempt in range(20):
        try:
            tmp.replace(path)
            return
        except PermissionError:
            # A reader may hold the target open for a few milliseconds.
            if attempt == 19:
                raise
            time.sleep(0.05)


def load_state() -> dict[str, Any] | None:
    try:
        document = json.loads(state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return document if isinstance(document, dict) else None


def _pid_alive(pid: Any) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        kernel32 = ctypes.windll.kernel32
        kernel32.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
        kernel32.GetExitCodeProcess.restype = ctypes.c_int
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel32.CloseHandle.restype = ctypes.c_int
        # PROCESS_QUERY_LIMITED_INFORMATION; works without elevating and avoids
        # parsing localized tasklist output (GBK decoding broke on zh-CN).
        handle = kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        try:
            exit_code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return False
            return exit_code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def health(state: dict[str, Any] | None = None, timeout: float = 1.5) -> dict[str, Any] | None:
    state = state or load_state()
    if not state:
        return None
    port = state.get("port")
    if not isinstance(port, int):
        return None
    request = urllib.request.Request(f"http://127.0.0.1:{port}/health")
    token = state.get("token")
    if isinstance(token, str) and token:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, ValueError, urllib.error.URLError):
        return None
    return payload if isinstance(payload, dict) else None


# ---------------------------------------------------------------------------
# Process lifecycle


def _spawn_environment() -> dict[str, str]:
    environment = dict(os.environ)
    plugin_mcp_dir = str(Path(__file__).resolve().parent.parent)
    existing = environment.get("PYTHONPATH", "")
    environment["PYTHONPATH"] = plugin_mcp_dir + (os.pathsep + existing if existing else "")
    environment.setdefault("PYTHONUTF8", "1")
    environment.setdefault("PYTHONIOENCODING", "utf-8")
    return environment


def _interpreter() -> str:
    """Pick an interpreter that never allocates a console window.

    A venv's ``python.exe`` is a launcher that re-executes the real
    ``python3.13.exe`` as a *new* process. The child inherits the launcher's
    creation flags inconsistently, so on Windows the grandchild gets a console
    of its own even when the parent was started with CREATE_NO_WINDOW — the
    symptom is a terminal window popping up whenever the bridge starts.
    ``pythonw.exe`` is the same interpreter linked against the GUI subsystem:
    it is console-free by construction, so the re-exec cannot create a window.
    """
    if os.name != "nt":
        return sys.executable
    executable = Path(sys.executable)
    if executable.name.lower() == "pythonw.exe":
        return str(executable)
    candidates = [executable.with_name("pythonw.exe")]
    resolved = Path(sys._base_executable) if getattr(sys, "_base_executable", None) else None
    if resolved is not None and resolved.name.lower() == "python.exe":
        candidates.append(resolved.with_name("pythonw.exe"))
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return str(executable)


def _hidden_process_kwargs() -> dict[str, Any]:
    """Detached, window-less spawn settings (empty on non-Windows).

    ``DETACHED_PROCESS`` is deliberately omitted: it conflicts with
    ``STARTF_USESHOWWINDOW`` and a detached child cannot inherit a console at
    all, which breaks the venv launcher's re-exec. ``pythonw.exe`` plus
    ``CREATE_NO_WINDOW`` and ``SW_HIDE`` is what actually keeps the bridge
    invisible.
    """
    if os.name != "nt":
        return {"close_fds": True}
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = subprocess.SW_HIDE
    return {
        "creationflags": (
            getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            | getattr(subprocess, "CREATE_NO_WINDOW", 0)
        ),
        "startupinfo": startupinfo,
        # Close every inherited handle except the three standard streams. The
        # bridge outlives its launcher, so leaving the MCP server's stdout pipe
        # open here would make the client wait for EOF long after we returned.
        "close_fds": True,
    }


def _wait_for_exit(pid: Any, timeout_s: float = 5.0) -> bool:
    """Block until ``pid`` is gone (or the timeout elapses)."""
    if not _pid_alive(pid):
        return True
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if not _pid_alive(pid):
            return True
        time.sleep(0.1)
    return False


def start(port: int | None = None, wait_s: float = 20.0) -> dict[str, Any]:
    """Start the bridge if it is not already healthy; returns its state."""
    current = load_state()
    if current and _pid_alive(current.get("pid")) and health(current):
        return current

    # The SessionStart hook, the MCP server and a manual /workbuddy-provider can
    # all call this at once. Without serialization each caller spawns its own
    # server, and on Windows the last bind wins while the others keep running
    # invisibly. The lock makes concurrent callers observe one instance.
    lock = _acquire_start_lock()
    try:
        current = load_state()
        if current and _pid_alive(current.get("pid")) and health(current):
            return current

        # A stopped bridge can keep accepting connections for a moment. Wait for
        # it to exit first: otherwise the wait loop below sees the dying
        # instance's state file, reports success, and the caller gets a dead pid.
        stale_pid = current.get("pid") if isinstance(current, dict) else None
        if _pid_alive(stale_pid):
            _wait_for_exit(stale_pid)
            current = load_state()

        # Reuse the previous local token when possible so a provider entry
        # written before a restart keeps working without being rewritten.
        previous_token = current.get("token") if isinstance(current, dict) else None
        token = previous_token if isinstance(previous_token, str) and previous_token else secrets.token_urlsafe(32)
        requested_port = port if isinstance(port, int) and port > 0 else DEFAULT_PORT
        wrapper = Path(__file__).resolve().parent.parent / "wb_bridge.py"
        state_home().mkdir(parents=True, exist_ok=True)
        log = open(log_path(), "ab")
        spawn_kwargs = _hidden_process_kwargs()
        try:
            process = subprocess.Popen(
                [
                    _interpreter(),
                    str(wrapper),
                    "serve",
                    "--port",
                    str(requested_port),
                    "--token",
                    token,
                ],
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=log,
                cwd=str(state_home()),
                env=_spawn_environment(),
                **spawn_kwargs,
            )
        finally:
            log.close()

        deadline = time.time() + wait_s
        while time.time() < deadline:
            latest = load_state()
            if (
                latest
                and latest.get("token") == token
                and latest.get("pid") != stale_pid
                and _pid_alive(latest.get("pid"))
                and health(latest)
            ):
                return latest
            if process.poll() is not None:
                break
            time.sleep(0.25)
        raise RuntimeError(
            "WorkBuddy bridge did not become healthy; see " + str(log_path())
        )
    finally:
        if lock is not None:
            _release_start_lock(*lock)


def stop() -> dict[str, Any]:
    current = load_state()
    if not current:
        return {"stopped": False, "reason": "no bridge state"}
    pid = current.get("pid")
    if _pid_alive(pid):
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
        if os.name == "nt":
            try:
                subprocess.run(
                    ["taskkill", "/PID", str(pid), "/T", "/F"],
                    capture_output=True,
                    timeout=10,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            except (OSError, subprocess.SubprocessError):
                pass
    # Keep the state file (and its local bearer token) so a later start does
    # not invalidate the provider entry already written into ZCode.
    current["pid"] = None
    current["stoppedAtMs"] = int(time.time() * 1000)
    _write_json_atomic(state_path(), current)
    return {"stopped": True, "pid": pid}


def status() -> dict[str, Any]:
    current = load_state()
    if not current:
        return {"running": False, "statePath": str(state_path())}
    payload = health(current)
    return {
        "running": payload is not None,
        "pid": current.get("pid"),
        "port": current.get("port"),
        "baseUrl": current.get("baseUrl"),
        "startedAtMs": current.get("startedAtMs"),
        "statePath": str(state_path()),
        "logPath": str(log_path()),
        "health": payload,
    }


# ---------------------------------------------------------------------------
# Variant/model helpers


def _variant_by_provider_hint(hint: str | None) -> Variant | None:
    if not hint:
        return None
    normalized = hint.strip().lower()
    if normalized in ("cn", "workbuddy"):
        return CN
    if normalized in ("global", "workbuddy-ai", "workbuddy_ai", "ai"):
        return GLOBAL
    return variant_for(normalized)


def _signed_in_variants() -> list[tuple[Variant, dict[str, Any]]]:
    found: list[tuple[Variant, dict[str, Any]]] = []
    for variant in VARIANTS:
        try:
            found.append((variant, upstream_module.resolve_credential(variant)))
        except Exception:
            continue
    return found


def _catalog(variant: Variant, credential: dict[str, Any], force: bool = False) -> list[dict[str, Any]]:
    try:
        result = upstream_module.fetch_models(variant, credential, force=force)
        models = result.get("models") if isinstance(result.get("models"), list) else []
        return [catalog_module.model_with_current_promotion(model) for model in models]
    except Exception:
        return catalog_module.builtin_models(variant)


def _models_for_variants(
    selected: list[tuple[Variant, dict[str, Any]]], force: bool = False
) -> list[dict[str, Any]]:
    models: list[dict[str, Any]] = []
    for variant, credential in selected:
        for model in _catalog(variant, credential, force=force):
            models.append({**model, "variant": variant.id, "variantName": variant.display_name})
    return models


def _clamp_effort(model_id: str, effort: Any, models: list[dict[str, Any]]) -> str | None:
    """Map a ZCode effort level onto one the model advertises."""
    if not isinstance(effort, str) or not effort.strip():
        return None
    value = effort.strip().lower()
    if value in ("off", "none", "minimal", "disabled"):
        return None
    info = next((m for m in models if m.get("id") == model_id), None)
    supported = info.get("supportedEfforts") if isinstance(info, dict) else None
    if isinstance(supported, list) and supported:
        allowed = [item for item in supported if isinstance(item, str)]
        if value in allowed:
            return value
        default = info.get("defaultEffort")
        if isinstance(default, str) and default in allowed:
            return default
        index = EFFORT_ORDER.index(value) if value in EFFORT_ORDER else len(EFFORT_ORDER) - 1
        for distance in range(1, len(EFFORT_ORDER)):
            for candidate_index in (index - distance, index + distance):
                if 0 <= candidate_index < len(EFFORT_ORDER):
                    candidate = EFFORT_ORDER[candidate_index]
                    if candidate in allowed:
                        return candidate
        return allowed[0]
    return value if value in EFFORT_ORDER else None


def _completion_id() -> str:
    return "chatcmpl-" + uuid.uuid4().hex


def _usage_estimate(messages: list[Any]) -> dict[str, int]:
    characters = 0
    for message in messages:
        if isinstance(message, dict):
            content = message.get("content")
            if isinstance(content, str):
                characters += len(content)
            elif isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and isinstance(part.get("text"), str):
                        characters += len(part["text"])
    prompt = max(1, characters // 4)
    return {"prompt_tokens": prompt, "completion_tokens": 0, "total_tokens": prompt}


# ---------------------------------------------------------------------------
# Stream aggregation


class _Accumulator:
    """Aggregate OpenAI SSE chunks into one ``chat.completion`` message."""

    def __init__(self) -> None:
        self.content: list[str] = []
        self.reasoning: list[str] = []
        self.tool_calls: dict[int, dict[str, Any]] = {}
        self.finish_reason: str | None = None
        self.usage: dict[str, Any] | None = None
        self.role: str | None = None

    def feed(self, event: dict[str, Any]) -> None:
        if isinstance(event.get("usage"), dict):
            self.usage = event["usage"]
        choices = event.get("choices") if isinstance(event.get("choices"), list) else []
        for choice in choices:
            if not isinstance(choice, dict):
                continue
            if choice.get("finish_reason"):
                self.finish_reason = choice["finish_reason"]
            delta = choice.get("delta")
            if not isinstance(delta, dict):
                delta = choice.get("message") if isinstance(choice.get("message"), dict) else {}
            if isinstance(delta.get("role"), str):
                self.role = delta["role"]
            for key in ("content", "text"):
                piece = delta.get(key)
                if isinstance(piece, str):
                    self.content.append(piece)
            for key in ("reasoning_content", "reasoning"):
                piece = delta.get(key)
                if isinstance(piece, str):
                    self.reasoning.append(piece)
            raw_calls = delta.get("tool_calls")
            if isinstance(raw_calls, list):
                for position, call in enumerate(raw_calls):
                    if not isinstance(call, dict):
                        continue
                    index = call.get("index")
                    if not isinstance(index, int):
                        index = position
                    slot = self.tool_calls.setdefault(
                        index,
                        {"id": "", "type": "function", "function": {"name": "", "arguments": ""}},
                    )
                    if isinstance(call.get("id"), str) and call["id"]:
                        slot["id"] = call["id"]
                    if isinstance(call.get("type"), str) and call["type"]:
                        slot["type"] = call["type"]
                    function = call.get("function") if isinstance(call.get("function"), dict) else {}
                    name = function.get("name")
                    if isinstance(name, str) and name:
                        # Some gateways repeat the full name on every fragment.
                        if not slot["function"]["name"]:
                            slot["function"]["name"] = name
                        elif slot["function"]["name"] != name:
                            slot["function"]["name"] += name
                    arguments = function.get("arguments")
                    if isinstance(arguments, str):
                        slot["function"]["arguments"] += arguments

    def message(self) -> dict[str, Any]:
        message: dict[str, Any] = {"role": self.role or "assistant"}
        content = "".join(self.content)
        message["content"] = content if content or not self.tool_calls else None
        if self.reasoning:
            message["reasoning_content"] = "".join(self.reasoning)
        if self.tool_calls:
            calls = []
            for index in sorted(self.tool_calls):
                call = self.tool_calls[index]
                if not call["id"]:
                    call["id"] = "call_" + uuid.uuid4().hex[:24]
                calls.append(call)
            message["tool_calls"] = calls
        return message

    def finish(self) -> str:
        if self.finish_reason:
            return self.finish_reason
        return "tool_calls" if self.tool_calls else "stop"


# ---------------------------------------------------------------------------
# HTTP handler


class _BridgeHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "workbuddy-connect/" + BRIDGE_VERSION
    token = ""
    verbose = False

    # -- helpers ---------------------------------------------------------

    def log_message(self, fmt: str, *args: Any) -> None:
        if self.verbose:
            sys.stderr.write("[bridge] " + (fmt % args) + "\n")
            sys.stderr.flush()

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _error(self, status: int, message: str, kind: str = "invalid_request_error") -> None:
        self._send_json(status, {"error": {"message": message, "type": kind, "code": None}})

    def _authorized(self) -> bool:
        if not self.token:
            return True
        header = self.headers.get("Authorization", "")
        candidate = header[7:].strip() if header.lower().startswith("bearer ") else ""
        if secrets.compare_digest(candidate, self.token):
            return True
        self._error(401, "invalid or missing bridge token", "authentication_error")
        return False

    def _read_body(self) -> dict[str, Any] | None:
        length_header = self.headers.get("Content-Length")
        try:
            length = int(length_header) if length_header else 0
        except ValueError:
            length = 0
        if length <= 0:
            self._error(400, "empty request body")
            return None
        if length > MAX_BODY_BYTES:
            self._error(413, "request body too large")
            return None
        raw = self.rfile.read(length)
        try:
            document = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            self._error(400, "request body is not valid JSON")
            return None
        if not isinstance(document, dict):
            self._error(400, "request body must be a JSON object")
            return None
        return document

    def _selected_variants(self) -> list[tuple[Variant, dict[str, Any]]]:
        hint = self.headers.get("X-WorkBuddy-Variant")
        if hint:
            pinned = _variant_by_provider_hint(hint)
            if pinned is None:
                return []
            try:
                return [(pinned, upstream_module.resolve_credential(pinned))]
            except Exception:
                return []
        return _signed_in_variants()

    # -- routes ----------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        path = self.path.split("?", 1)[0].rstrip("/")
        if path == "/health":
            self._send_json(
                200,
                {
                    "ok": True,
                    "service": "workbuddy-connect",
                    "version": BRIDGE_VERSION,
                    "variants": [variant.id for variant, _ in _signed_in_variants()],
                },
            )
            return
        if path in ("/v1/models", "/models"):
            if not self._authorized():
                return
            selected = self._selected_variants()
            if not selected:
                self._error(503, "no signed-in WorkBuddy account found", "service_unavailable")
                return
            data = []
            for model in _models_for_variants(selected):
                data.append(
                    {
                        "id": model["id"],
                        "object": "model",
                        "created": 0,
                        "owned_by": model.get("variantName") or "WorkBuddy",
                        "name": model.get("name") or model["id"],
                        "context_window": model.get("contextWindow"),
                        "max_tokens": model.get("maxTokens"),
                        "capabilities": {"vision": bool(model.get("supportsImages"))},
                        "x_workbuddy_variant": model.get("variant"),
                        "x_workbuddy_credits": (model.get("billing") or {}).get("credits"),
                    }
                )
            self._send_json(200, {"object": "list", "data": data})
            return
        self._error(404, "not found", "not_found_error")

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        path = self.path.split("?", 1)[0].rstrip("/")
        if path not in ("/v1/chat/completions", "/chat/completions"):
            self._error(404, "not found", "not_found_error")
            return
        if not self._authorized():
            return
        body = self._read_body()
        if body is None:
            return
        self._chat(body)

    # -- chat ------------------------------------------------------------

    def _chat(self, body: dict[str, Any]) -> None:
        model = body.get("model")
        if not isinstance(model, str) or not model.strip():
            self._error(400, "missing 'model'")
            return
        messages = body.get("messages")
        if not isinstance(messages, list) or not messages:
            self._error(400, "missing 'messages'")
            return

        selected = self._selected_variants()
        if not selected:
            self._error(503, "no signed-in WorkBuddy account found", "service_unavailable")
            return

        upstream_body: dict[str, Any] = {
            key: body[key] for key in FORWARD_FIELDS if key in body
        }
        if "max_tokens" not in upstream_body and isinstance(body.get("max_completion_tokens"), int):
            upstream_body["max_tokens"] = body["max_completion_tokens"]
        upstream_body["model"] = model.strip()

        models = _models_for_variants(selected)
        effort = _clamp_effort(upstream_body["model"], upstream_body.get("reasoning_effort"), models)
        if effort is None:
            upstream_body.pop("reasoning_effort", None)
        else:
            upstream_body["reasoning_effort"] = effort

        stream_requested = body.get("stream") is True
        completion_id = _completion_id()
        created = int(time.time())
        errors: list[str] = []

        for variant, credential in selected:
            try:
                events = upstream_module.chat_stream_events(
                    credential, upstream_body, timeout_s=UPSTREAM_TIMEOUT_S
                )
                try:
                    first = next(events)
                except StopIteration:
                    first = None
            except Exception as error:
                errors.append(f"{variant.display_name}: {error}")
                continue

            if stream_requested:
                self._stream_response(first, events, model, completion_id, created)
            else:
                self._complete_response(first, events, messages, model, completion_id, created)
            return

        self._error(502, "WorkBuddy upstream failed: " + "; ".join(errors), "upstream_error")

    def _stream_headers(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-store")
        self.send_header("Connection", "close")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        self.close_connection = True

    def _write_event(self, payload: dict[str, Any] | str) -> bool:
        if isinstance(payload, str):
            data = payload
        else:
            data = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        try:
            self.wfile.write(f"data: {data}\n\n".encode("utf-8"))
            self.wfile.flush()
            return True
        except (BrokenPipeError, ConnectionResetError, OSError):
            return False

    def _normalize_chunk(self, event: dict[str, Any], model: str, completion_id: str, created: int) -> dict[str, Any]:
        chunk = {key: value for key, value in event.items() if key != "error"}
        chunk["id"] = chunk.get("id") if isinstance(chunk.get("id"), str) and chunk["id"] else completion_id
        chunk["object"] = "chat.completion.chunk"
        chunk["created"] = chunk.get("created") if isinstance(chunk.get("created"), int) else created
        chunk["model"] = model
        choices = chunk.get("choices")
        if isinstance(choices, list):
            normalized = []
            for index, choice in enumerate(choices):
                if not isinstance(choice, dict):
                    continue
                item = dict(choice)
                item.setdefault("index", index)
                if item.get("finish_reason") == "":
                    # The upstream emits "" while a turn is still running; an
                    # empty string is not a valid OpenAI finish reason.
                    item["finish_reason"] = None
                delta = item.get("delta")
                if not isinstance(delta, dict) and isinstance(item.get("message"), dict):
                    delta = item["message"]
                    item.pop("message", None)
                if isinstance(delta, dict):
                    item["delta"] = _clean_delta(delta)
                else:
                    item["delta"] = {}
                normalized.append(item)
            chunk["choices"] = normalized
        return chunk

    def _stream_response(
        self,
        first: dict[str, Any] | None,
        events: Iterator[dict[str, Any]],
        model: str,
        completion_id: str,
        created: int,
    ) -> None:
        self._stream_headers()
        queue: list[dict[str, Any]] = []
        if isinstance(first, dict):
            queue.append(first)
        role_sent = False
        try:
            while True:
                event = queue.pop(0) if queue else next(events, None)
                if event is None:
                    break
                if not isinstance(event, dict):
                    continue
                if event.get("error"):
                    if not self._write_event({"error": event["error"]}):
                        return
                    break
                if not role_sent:
                    delta_seen = False
                    for choice in event.get("choices") or []:
                        if isinstance(choice, dict) and isinstance(choice.get("delta"), dict):
                            choice["delta"].setdefault("role", "assistant")
                            delta_seen = True
                    if delta_seen:
                        role_sent = True
                chunk = self._normalize_chunk(event, model, completion_id, created)
                if not _chunk_has_payload(chunk):
                    # After empty fields are stripped the event may carry nothing
                    # at all; forwarding it would only add noise for the client.
                    continue
                if not self._write_event(chunk):
                    return
        except Exception as error:
            self._write_event({"error": {"message": str(error), "type": "upstream_error"}})
        finally:
            self._write_event("[DONE]")

    def _complete_response(
        self,
        first: dict[str, Any] | None,
        events: Iterator[dict[str, Any]],
        messages: list[Any],
        model: str,
        completion_id: str,
        created: int,
    ) -> None:
        accumulator = _Accumulator()
        try:
            if isinstance(first, dict):
                accumulator.feed(first)
            for event in events:
                if isinstance(event, dict):
                    accumulator.feed(event)
        except Exception as error:
            self._error(502, str(error), "upstream_error")
            return
        usage = accumulator.usage or _usage_estimate(messages)
        message = accumulator.message()
        completion_tokens = 0
        if isinstance(usage.get("completion_tokens"), int):
            completion_tokens = usage["completion_tokens"]
        if completion_tokens == 0:
            completion_tokens = max(1, len(str(message.get("content") or "")) // 4)
        usage = {
            "prompt_tokens": int(usage.get("prompt_tokens") or 0),
            "completion_tokens": completion_tokens,
            "total_tokens": int(usage.get("total_tokens") or 0)
            or int(usage.get("prompt_tokens") or 0) + completion_tokens,
        }
        self._send_json(
            200,
            {
                "id": completion_id,
                "object": "chat.completion",
                "created": created,
                "model": model,
                "choices": [
                    {
                        "index": 0,
                        "message": message,
                        "finish_reason": accumulator.finish(),
                    }
                ],
                "usage": usage,
            },
        )


# ---------------------------------------------------------------------------
# Server entry


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    # Windows maps SO_REUSEADDR to a "hijack" semantic: several processes can
    # bind the same port and the last one silently wins. That produced several
    # live bridges fighting over one port, so exclusivity is required there.
    # POSIX needs reuse only to skip TIME_WAIT when restarting.
    allow_reuse_address = os.name != "nt"


def _acquire_start_lock(timeout_s: float = 30.0) -> tuple[Any, str] | None:
    """Best-effort cross-process start lock; returns (handle, kind) or None."""
    lock_path = state_home() / "bridge.lock"
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(lock_path, "a+b")
    except OSError:
        return None
    deadline = time.time() + timeout_s
    while True:
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0, os.SEEK_END)
                if handle.tell() == 0:
                    handle.write(b"\0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                return handle, "msvcrt"
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return handle, "flock"
        except OSError:
            if time.time() >= deadline:
                handle.close()
                return None
            time.sleep(0.1)


def _release_start_lock(handle: Any, kind: str) -> None:
    try:
        if kind == "msvcrt":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass
    finally:
        handle.close()


def serve(port: int, token: str, verbose: bool = False) -> int:
    handler = type("BridgeHandler", (_BridgeHandler,), {"token": token, "verbose": verbose})
    httpd = None
    for candidate in (port, 0):
        try:
            httpd = _Server(("127.0.0.1", candidate), handler)
            break
        except OSError:
            continue
    if httpd is None:
        raise RuntimeError("cannot bind the bridge to any loopback port")
    actual_port = httpd.server_address[1]
    state = {
        "pid": os.getpid(),
        "port": actual_port,
        "token": token,
        "baseUrl": f"http://127.0.0.1:{actual_port}/v1",
        "startedAtMs": int(time.time() * 1000),
        "version": BRIDGE_VERSION,
        "executable": sys.executable,
    }
    _write_json_atomic(state_path(), state)
    try:
        httpd.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        try:
            current = load_state()
            if current and current.get("pid") == os.getpid():
                state_path().unlink()
        except OSError:
            pass
    return 0


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="WorkBuddy local OpenAI-compatible bridge")
    subparsers = parser.add_subparsers(dest="command", required=True)

    serve_parser = subparsers.add_parser("serve", help="run the loopback HTTP server")
    serve_parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    serve_parser.add_argument("--token", default="")
    serve_parser.add_argument("--verbose", action="store_true")

    subparsers.add_parser("start", help="start in the background")
    subparsers.add_parser("stop", help="stop the background server")
    subparsers.add_parser("status", help="show bridge status")
    subparsers.add_parser("ensure", help="start the bridge and register the provider")

    arguments = parser.parse_args(argv)
    if arguments.command == "serve":
        token = arguments.token or secrets.token_urlsafe(32)
        return serve(arguments.port, token, verbose=arguments.verbose)
    if arguments.command == "start":
        print(json.dumps(start(), ensure_ascii=False, indent=2))
        return 0
    if arguments.command == "stop":
        print(json.dumps(stop(), ensure_ascii=False, indent=2))
        return 0
    if arguments.command == "status":
        print(json.dumps(status(), ensure_ascii=False, indent=2))
        return 0
    if arguments.command == "ensure":
        try:
            current = start()
            from . import zcode_provider

            result = zcode_provider.register(
                base_url=current.get("baseUrl"), token=current.get("token")
            )
            print(json.dumps({"bridge": current, "provider": result}, ensure_ascii=False, indent=2))
        except Exception as error:
            try:
                state_home().mkdir(parents=True, exist_ok=True)
                with open(log_path(), "a", encoding="utf-8") as handle:
                    handle.write(f"[ensure] {time.strftime('%Y-%m-%d %H:%M:%S')} {error!r}\n")
                    import traceback

                    traceback.print_exc(file=handle)
            except OSError:
                pass
            print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False))
            return 1
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
