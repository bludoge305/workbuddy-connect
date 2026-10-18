"""workbuddy-connect MCP server.

A stdio MCP server exposing the signed-in WorkBuddy / WorkBuddy AI desktop
session to ZCode: status, model catalog, chat, credits, diagnostics and
logout. Protocol and tool behavior follow
[corrinehu/dsh-workbuddy-connect](https://github.com/corrinehu/dsh-workbuddy-connect)
(MIT License, Copyright © 2026 Corrine Hu).
The server speaks newline-delimited JSON-RPC 2.0 per the MCP stdio
transport and uses only the Python standard library (``cryptography`` is
needed only to decrypt WorkBuddy 5.6+ encrypted sign-in files).
"""

from __future__ import annotations

import json
import sys
import traceback
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from wbclient import auth as auth_module  # noqa: E402
from wbclient import bridge as bridge_module  # noqa: E402
from wbclient import doctor as doctor_module  # noqa: E402
from wbclient import upstream as upstream_module  # noqa: E402
from wbclient import zcode_provider  # noqa: E402
from wbclient.catalog import model_with_current_promotion  # noqa: E402
from wbclient.variants import CN, GLOBAL, Variant, VARIANTS, variant_for  # noqa: E402

SERVER_INFO = {"name": "workbuddy-connect", "version": "0.2.0"}
PROTOCOL_VERSION = "2025-06-18"

VARIANT_IDS = ("cn", "global")
VARIANT_BY_KEY = {"cn": CN, "global": GLOBAL}


def variant_from_key(key: str) -> Variant:
    if key not in VARIANT_BY_KEY:
        raise ValueError(f"unknown variant {key!r}; expected one of {list(VARIANT_IDS)}")
    return VARIANT_BY_KEY[key]


def json_text(payload: Any) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False, indent=2)}]}


def error_text(message: str) -> dict[str, Any]:
    return {
        "content": [{"type": "text", "text": message}],
        "isError": True,
    }


# ---------------------------------------------------------------------------
# Tool implementations


def tool_status(args: dict[str, Any]) -> dict[str, Any]:
    include_credits = bool(args.get("includeCredits", False))
    key = args.get("variant", "all")
    selected = VARIANTS if key == "all" else (variant_from_key(key),)
    results = []
    for variant in selected:
        entry: dict[str, Any] = {"variant": variant.id, "displayName": variant.display_name}
        entry["auth"] = auth_module.status(variant)
        if include_credits and entry["auth"].get("state") == "signed-in":
            try:
                credential = upstream_module.resolve_credential(variant)
                entry["credits"] = upstream_module.fetch_credits(credential)
            except Exception as error:
                entry["credits"] = {"error": str(error)}
        results.append(entry)
    return json_text({"status": results})


def tool_models(args: dict[str, Any]) -> dict[str, Any]:
    key = args.get("variant", "cn")
    variant = variant_from_key(key)
    force = bool(args.get("refresh", False))
    credential = None
    signed_in = False
    try:
        credential = upstream_module.resolve_credential(variant)
        signed_in = True
    except Exception:
        credential = None
    catalog = upstream_module.fetch_models(variant, credential, force=force)
    effort_filter = args.get("effort")
    models = []
    for model in catalog.get("models", []):
        model = model_with_current_promotion(model)
        if effort_filter and effort_filter not in (model.get("supportedEfforts") or []):
            continue
        models.append(model)
    catalog["models"] = models
    catalog["signedIn"] = signed_in
    catalog["variant"] = variant.id
    return json_text(catalog)


def tool_chat(args: dict[str, Any]) -> dict[str, Any]:
    model = args.get("model")
    if not isinstance(model, str) or not model:
        raise ValueError("'model' is required (see the workbuddy_models tool for available ids)")
    messages = args.get("messages")
    prompt = args.get("prompt")
    if not messages and isinstance(prompt, str) and prompt:
        messages = [{"role": "user", "content": prompt}]
    if not isinstance(messages, list) or not messages:
        raise ValueError("provide either 'prompt' (string) or 'messages' (OpenAI-format array)")

    variant_key = args.get("variant")
    variants_to_try: tuple[Variant, ...]
    if variant_key:
        variants_to_try = (variant_from_key(variant_key),)
    else:
        # Model ids overlap across regions, so an explicit variant is preferred;
        # without one, try whichever product is signed in, CN first.
        ordered = [CN, GLOBAL]
        available = []
        for variant in ordered:
            try:
                upstream_module.resolve_credential(variant)
                available.append(variant)
            except Exception:
                continue
        if not available:
            raise auth_module.WorkBuddyCredentialError(
                "no-credential",
                "no signed-in WorkBuddy / WorkBuddy AI account found; sign in once in either desktop app, "
                "or pass an explicit 'variant' after setting its auth env var",
            )
        variants_to_try = tuple(available)

    body: dict[str, Any] = {"model": model, "messages": messages}
    if args.get("reasoningEffort"):
        body["reasoning_effort"] = args["reasoningEffort"]
    if isinstance(args.get("maxTokens"), int) and args["maxTokens"] > 0:
        body["max_tokens"] = args["maxTokens"]
    if isinstance(args.get("temperature"), (int, float)) and not isinstance(args.get("temperature"), bool):
        body["temperature"] = args["temperature"]
    timeout_s = args.get("timeoutSeconds", 180)
    if not isinstance(timeout_s, int) or timeout_s <= 0:
        timeout_s = 180

    errors: list[str] = []
    for variant in variants_to_try:
        try:
            credential = upstream_module.resolve_credential(variant)
            result = upstream_module.chat_once(credential, body, timeout_s)
        except Exception as error:
            kind = getattr(error, "kind", None) or getattr(error, "reason_code", None)
            errors.append(f"{variant.display_name}: {f'[{kind}] ' if kind else ''}{error}")
            continue
        result["variant"] = variant.id
        result["model"] = model
        return json_text(result)
    raise RuntimeError(
        "all variants failed:\n" + "\n".join(f"  - {line}" for line in errors)
    )


def tool_credits(args: dict[str, Any]) -> dict[str, Any]:
    variant = variant_from_key(args.get("variant", "cn"))
    credential = upstream_module.resolve_credential(variant)
    result = upstream_module.fetch_credits(credential)
    result["variant"] = variant.id
    return json_text(result)


def tool_doctor(args: dict[str, Any]) -> dict[str, Any]:
    key = args.get("variant", "all")
    live = bool(args.get("live", False))
    variant_id = None
    if key != "all":
        variant_id = variant_from_key(key).id
    return json_text(doctor_module.run_doctor(variant_id, live=live))


def tool_logout(args: dict[str, Any]) -> dict[str, Any]:
    key = args.get("variant", "all")
    selected = VARIANTS if key == "all" else (variant_from_key(key),)
    results = []
    for variant in selected:
        removed = auth_module.logout(variant)
        results.append({
            "variant": variant.id,
            "removed": removed,
            "path": str(auth_module.own_auth_path(variant)),
        })
    return json_text({
        "logout": results,
        "notice": "only the plugin-owned refreshed copies were removed; the desktop app's own sign-in is untouched",
    })


def tool_bridge_start(args: dict[str, Any]) -> dict[str, Any]:
    port = args.get("port")
    if port is not None and (not isinstance(port, int) or port <= 0):
        raise ValueError("'port' must be a positive integer")
    state = bridge_module.start(port)
    result: dict[str, Any] = {"bridge": state}
    if args.get("register", True):
        result["provider"] = zcode_provider.register(
            variant=args.get("variant", "all"),
            base_url=state.get("baseUrl"),
            token=state.get("token"),
            force_refresh=bool(args.get("refresh", False)),
        )
    return json_text(result)


def tool_bridge_status(args: dict[str, Any]) -> dict[str, Any]:
    status = bridge_module.status()
    status["providerConfigPath"] = str(zcode_provider.zcode_config_path())
    status["providerConfigExists"] = zcode_provider.zcode_config_path().exists()
    return json_text(status)


def tool_bridge_stop(args: dict[str, Any]) -> dict[str, Any]:
    return json_text(bridge_module.stop())


def tool_provider_register(args: dict[str, Any]) -> dict[str, Any]:
    variant = args.get("variant", "all")
    state = bridge_module.start(args.get("port"))
    result = zcode_provider.register(
        variant=variant,
        base_url=state.get("baseUrl"),
        token=state.get("token"),
        force_refresh=bool(args.get("refresh", False)),
    )
    return json_text({"bridge": state, "provider": result})


def tool_provider_unregister(args: dict[str, Any]) -> dict[str, Any]:
    return json_text(zcode_provider.unregister())


# ---------------------------------------------------------------------------
# Tool declarations

TOOLS = [
    {
        "name": "workbuddy_status",
        "description": (
            "Show the sign-in state of the WorkBuddy (CN) and/or WorkBuddy AI (global) desktop apps this "
            "plugin reuses: signed-in state, account nickname, token expiry, credential source, and — with "
            "includeCredits — the remaining credit balance. Never refreshes tokens."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "variant": {
                    "type": "string",
                    "enum": ["cn", "global", "all"],
                    "description": "Which product to inspect; 'all' (default) reports both.",
                    "default": "all",
                },
                "includeCredits": {
                    "type": "boolean",
                    "description": "Also query the live credit balance for signed-in variants (network).",
                    "default": False,
                },
            },
        },
    },
    {
        "name": "workbuddy_models",
        "description": (
            "List the models the signed-in WorkBuddy account can use (the upstream 'cli' roster). Falls back "
            "to the last saved catalog, then a built-in snapshot. Each row carries context window, max output, "
            "image support, reasoning efforts and the credits multiplier (e.g. x0.79, x0.00 = free)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "variant": {"type": "string", "enum": ["cn", "global"], "description": "Which product's catalog; default cn."},
                "refresh": {"type": "boolean", "description": "Bypass the 30-minute cache and fetch live.", "default": False},
                "effort": {
                    "type": "string",
                    "enum": ["low", "medium", "high", "xhigh", "max"],
                    "description": "Only list models declaring this reasoning effort.",
                },
            },
        },
    },
    {
        "name": "workbuddy_chat",
        "description": (
            "Send one chat completion to a WorkBuddy model and return the full answer (streams internally, "
            "consumes credits). 'messages' is OpenAI format and may carry image parts for multimodal models; "
            "'prompt' is a shorthand for a single user message. 'reasoningEffort' only for models declaring it. "
            "Without 'variant', tries CN then global, whichever is signed in."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "model": {"type": "string", "description": "Model id from workbuddy_models, e.g. glm-5.3."},
                "prompt": {"type": "string", "description": "Single user message shorthand."},
                "messages": {
                    "type": "array",
                    "description": "OpenAI-format messages ({role, content}); content may be a string or an array of {type:'text'|'image_url', ...} parts.",
                    "items": {"type": "object"},
                },
                "reasoningEffort": {"type": "string", "enum": ["low", "medium", "high", "xhigh", "max"]},
                "maxTokens": {"type": "integer", "minimum": 1},
                "temperature": {"type": "number"},
                "variant": {"type": "string", "enum": ["cn", "global"], "description": "Pin the product; default auto."},
                "timeoutSeconds": {"type": "integer", "minimum": 1, "default": 180},
            },
        },
    },
    {
        "name": "workbuddy_credits",
        "description": (
            "Query the live remaining credit balance for a signed-in WorkBuddy account: total, per-package "
            "remain/size/end-time rows, expiry buckets (24h/3d/7d), or the enterprise cycle quota."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "variant": {"type": "string", "enum": ["cn", "global"], "description": "Which product; default cn."},
            },
        },
    },
    {
        "name": "workbuddy_doctor",
        "description": (
            "Diagnose the WorkBuddy integration: auth-file candidates and their formats, Electron binary "
            "resolution, plugin-owned copies, catalog cache, and cryptography availability. live=true also "
            "performs a real catalog fetch and credit read per signed-in variant."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "variant": {"type": "string", "enum": ["cn", "global", "all"], "default": "all"},
                "live": {"type": "boolean", "description": "Include live network checks.", "default": False},
            },
        },
    },
    {
        "name": "workbuddy_logout",
        "description": (
            "Delete the plugin-owned refreshed credential copies (the desktop app's own sign-in is never "
            "touched). Use after signing out or switching accounts to clear stale plugin state."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "variant": {"type": "string", "enum": ["cn", "global", "all"], "default": "all"},
            },
        },
    },
    {
        "name": "workbuddy_bridge_start",
        "description": (
            "Start the loopback OpenAI-compatible bridge that lets ZCode use WorkBuddy models directly in "
            "the model picker, and (by default) register it in ZCode's personal provider config. The bridge "
            "listens on 127.0.0.1 and reuses the desktop sign-in; no API key is needed."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "port": {"type": "integer", "description": "Loopback port; default 38271 (falls back if busy)."},
                "variant": {
                    "type": "string",
                    "enum": ["cn", "global", "all"],
                    "description": "Which signed-in product to register; default all.",
                    "default": "all",
                },
                "register": {
                    "type": "boolean",
                    "description": "Write the provider entry into ZCode's provider_config.json (default true).",
                    "default": True,
                },
                "refresh": {
                    "type": "boolean",
                    "description": "Fetch the upstream model catalog before registering (default cache).",
                    "default": False,
                },
            },
        },
    },
    {
        "name": "workbuddy_bridge_status",
        "description": (
            "Show whether the local WorkBuddy bridge is running, its loopback base URL/port, PID, log path, "
            "and the ZCode provider config path it writes to."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "workbuddy_bridge_stop",
        "description": "Stop the loopback WorkBuddy bridge started by this plugin.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "workbuddy_provider_register",
        "description": (
            "Register (or refresh) the WorkBuddy models in ZCode's model picker by upserting this plugin's "
            "own providers in ~/.zcode/v2/provider_config.json. Starts the bridge if needed. Existing "
            "providers and model rules are preserved."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "variant": {"type": "string", "enum": ["cn", "global", "all"], "default": "all"},
                "port": {"type": "integer", "description": "Only used when the bridge is not running."},
                "refresh": {"type": "boolean", "default": False},
            },
        },
    },
    {
        "name": "workbuddy_provider_unregister",
        "description": (
            "Remove only the providers and model rules owned by this plugin from ZCode's personal provider "
            "config. Other providers and user edits are preserved."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
]

TOOL_HANDLERS = {
    "workbuddy_status": tool_status,
    "workbuddy_models": tool_models,
    "workbuddy_chat": tool_chat,
    "workbuddy_credits": tool_credits,
    "workbuddy_doctor": tool_doctor,
    "workbuddy_logout": tool_logout,
    "workbuddy_bridge_start": tool_bridge_start,
    "workbuddy_bridge_status": tool_bridge_status,
    "workbuddy_bridge_stop": tool_bridge_stop,
    "workbuddy_provider_register": tool_provider_register,
    "workbuddy_provider_unregister": tool_provider_unregister,
}


# ---------------------------------------------------------------------------
# JSON-RPC / MCP stdio loop


def handle_request(message: dict[str, Any]) -> dict[str, Any] | None:
    method = message.get("method")
    message_id = message.get("id")
    is_notification = message_id is None
    if method == "initialize":
        requested = message.get("params", {}).get("protocolVersion")
        return {
            "jsonrpc": "2.0",
            "id": message_id,
            "result": {
                "protocolVersion": requested if isinstance(requested, str) else PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": SERVER_INFO,
            },
        }
    if method == "notifications/initialized":
        return None
    if method == "ping":
        return {"jsonrpc": "2.0", "id": message_id, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": message_id, "result": {"tools": TOOLS}}
    if method == "tools/call":
        params = message.get("params", {})
        name = params.get("name")
        handler = TOOL_HANDLERS.get(name)
        if handler is None:
            if is_notification:
                return None
            return {
                "jsonrpc": "2.0",
                "id": message_id,
                "error": {"code": -32602, "message": f"unknown tool {name!r}"},
            }
        try:
            result = handler(params.get("arguments") or {})
        except Exception as error:
            return {"jsonrpc": "2.0", "id": message_id, "result": error_text(str(error))}
        return {"jsonrpc": "2.0", "id": message_id, "result": result}
    if is_notification:
        return None
    return {
        "jsonrpc": "2.0",
        "id": message_id,
        "error": {"code": -32601, "message": f"method not found: {method}"},
    }


def main() -> int:
    # Binary stdio keeps newline-delimited JSON intact on Windows.
    input_stream = sys.stdin.buffer
    output_stream = sys.stdout.buffer
    for raw_line in input_stream:
        line = raw_line.strip()
        if not line:
            continue
        try:
            message = json.loads(line.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            continue
        if not isinstance(message, dict):
            continue
        try:
            response = handle_request(message)
        except Exception:
            message_id = message.get("id")
            if message_id is None:
                continue
            response = {
                "jsonrpc": "2.0",
                "id": message_id,
                "error": {"code": -32603, "message": "internal error"},
            }
            sys.stderr.write(traceback.format_exc())
        if response is None:
            continue
        output_stream.write((json.dumps(response, ensure_ascii=False) + "\n").encode("utf-8"))
        output_stream.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
