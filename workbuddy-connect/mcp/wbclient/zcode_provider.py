"""Register the local WorkBuddy bridge as a ZCode model provider.

ZCode's desktop and CLI read the personal provider config at
``$ZCODE_PERSONAL_PROVIDER_CONFIG_FILE`` (default ``~/.zcode/v2/provider_config.json``)
and poll it once per second. Writing a valid provider rule there is therefore
enough for the models to appear in the model picker and in
**Settings → Model settings**, without patching ZCode or restarting it.

The file is user-owned: this module preserves every existing provider and model
rule, only replaces the entries whose ``providerId`` it owns, writes atomically,
and keeps a one-time backup under the plugin state directory.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path
from typing import Any

from . import catalog as catalog_module
from . import upstream as upstream_module
from .variants import CN, GLOBAL, VARIANTS, Variant, state_home, variant_for

SCHEMA_VERSION = 1
PROVIDER_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "https://github.com/corrinehu/dsh-workbuddy-connect")
PROVIDER_IDS = {
    "workbuddy": str(uuid.uuid5(PROVIDER_NAMESPACE, "provider/workbuddy")),
    "workbuddy-ai": str(uuid.uuid5(PROVIDER_NAMESPACE, "provider/workbuddy-ai")),
}
PROVIDER_NAMES = {"workbuddy": "WorkBuddy", "workbuddy-ai": "WorkBuddy AI"}
OWNED_PROVIDER_IDS = set(PROVIDER_IDS.values())


def zcode_config_path() -> Path:
    override = (os.environ.get("ZCODE_PERSONAL_PROVIDER_CONFIG_FILE") or "").strip()
    if override:
        return Path(override)
    data_base = (os.environ.get("ZCODE_DATA_BASE_DIR") or "").strip()
    root = Path(data_base) if data_base else Path.home()
    return root / ".zcode" / "v2" / "provider_config.json"


def backup_path() -> Path:
    return state_home() / "provider_config.backup.json"


def _read_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"schemaVersion": SCHEMA_VERSION, "config": {}}
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as error:
        raise ValueError(f"cannot parse {path}: {error}") from error
    if not isinstance(document, dict):
        raise ValueError(f"{path} must contain a JSON object")
    version = document.get("schemaVersion")
    if version != SCHEMA_VERSION:
        raise ValueError(
            f"unsupported provider config schemaVersion {version!r} in {path}; expected {SCHEMA_VERSION}"
        )
    if not isinstance(document.get("config"), dict):
        raise ValueError(f"{path} has no 'config' object")
    return document


def _section(document: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    config = document["config"]
    provider_rules = config.setdefault("providerConfigRules", {})
    if not isinstance(provider_rules, dict):
        raise ValueError("config.providerConfigRules must be an object")
    providers = provider_rules.setdefault("providerRules", [])
    if not isinstance(providers, list):
        raise ValueError("config.providerConfigRules.providerRules must be an array")
    model_rules = config.setdefault("modelConfigRules", {})
    if not isinstance(model_rules, dict):
        raise ValueError("config.modelConfigRules must be an object")
    model_rules.setdefault("providerModelRules", [])
    model_rules.setdefault("manualProviderModelRules", [])
    if not isinstance(model_rules["providerModelRules"], list):
        raise ValueError("config.modelConfigRules.providerModelRules must be an array")
    if not isinstance(model_rules["manualProviderModelRules"], list):
        raise ValueError("config.modelConfigRules.manualProviderModelRules must be an array")
    return provider_rules, model_rules


def _write_config(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not backup_path().exists():
        try:
            backup_path().parent.mkdir(parents=True, exist_ok=True)
            backup_path().write_text(
                json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
        except OSError:
            pass
    # Unique temp name: the SessionStart hook and a manual register call can run
    # at the same time, and a shared temp path makes one of them fail with a
    # sharing violation on Windows.
    tmp = path.with_suffix(path.suffix + f".{os.getpid()}.workbuddy.tmp")
    tmp.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    for attempt in range(20):
        try:
            tmp.replace(path)
            return
        except PermissionError:
            # ZCode polls this file about once a second and may hold it open.
            if attempt == 19:
                raise
            time.sleep(0.05)


def _signed_in_variants() -> list[tuple[Variant, dict[str, Any]]]:
    found: list[tuple[Variant, dict[str, Any]]] = []
    for variant in VARIANTS:
        try:
            found.append((variant, upstream_module.resolve_credential(variant)))
        except Exception:
            continue
    return found


def _variant_from_key(key: str) -> Variant | None:
    normalized = key.strip().lower()
    if normalized in ("cn", "workbuddy"):
        return CN
    if normalized in ("global", "workbuddy-ai", "workbuddy_ai", "ai"):
        return GLOBAL
    return variant_for(normalized)


def _models_for(
    variant: Variant, credential: dict[str, Any], force: bool = False
) -> list[dict[str, Any]]:
    try:
        result = upstream_module.fetch_models(variant, credential, force=force)
        models = result.get("models") if isinstance(result.get("models"), list) else []
        models = [catalog_module.model_with_current_promotion(model) for model in models]
    except Exception:
        models = catalog_module.builtin_models(variant)
    result_list: list[dict[str, Any]] = []
    seen: set[str] = set()
    for model in models:
        model_id = model.get("id")
        if not isinstance(model_id, str) or not model_id or model_id in seen:
            continue
        seen.add(model_id)
        result_list.append(model)
    return result_list


def _provider_rule(
    variant: Variant,
    models: list[dict[str, Any]],
    base_url: str,
    token: str,
) -> dict[str, Any]:
    ids = [model["id"] for model in models]
    provider_id = PROVIDER_IDS[variant.id]
    return {
        "providerId": provider_id,
        "providerName": PROVIDER_NAMES[variant.id],
        "enabled": True,
        "config": {
            "group": "standard-personal",
            "access": {"type": "api-key", "apiKey": token},
            "api": {
                "type": "openai-chat-completions",
                "baseUrl": base_url,
                "headers": {"X-WorkBuddy-Variant": variant.id},
            },
            "personalModelIds": ids,
            "modelOrder": ids,
            "visibility": "visible",
        },
    }


def _model_rule(provider_id: str, model: dict[str, Any]) -> dict[str, Any]:
    context_window = model.get("contextWindow")
    if not isinstance(context_window, int) or context_window <= 0:
        context_window = 200_000
    supports_images = model.get("supportsImages") is True
    return {
        "providerId": provider_id,
        "modelId": model["id"],
        "config": {
            "enabled": True,
            "properties": {
                "contextWindow": context_window,
                "inputFormat": {
                    "supportsText": True,
                    "supportsImage": supports_images,
                    "supportsVideo": False,
                    "supportsAudio": False,
                    "supportsPdf": False,
                },
                "outputFormat": {"supportsText": True},
                "supportsToolCall": True,
                "supportsJsonSchemaOutput": False,
                "supportsNativeWebSearch": False,
                "supportsMidConversationSystem": False,
            },
        },
    }


def register(
    variant: str = "all",
    base_url: str | None = None,
    token: str | None = None,
    force_refresh: bool = False,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Upsert the WorkBuddy providers into ZCode's personal provider config.

    ``variant`` is ``"all"``, ``"cn"``/``"workbuddy"`` or
    ``"global"``/``"workbuddy-ai"``. Only signed-in products are registered.
    Returns a summary; the write is skipped when ``dry_run`` is true.
    """
    if not base_url or not token:
        from . import bridge

        state = bridge.load_state()
        if not state:
            raise RuntimeError("bridge is not running; start it first")
        base_url = base_url or state.get("baseUrl")
        token = token or state.get("token")
    if not isinstance(base_url, str) or not base_url.startswith(("http://127.0.0.1:", "http://localhost:")):
        raise ValueError("base_url must be a loopback HTTP URL")
    if not isinstance(token, str) or not token:
        raise ValueError("a bridge token is required")

    requested: list[tuple[Variant, dict[str, Any]]]
    if variant.strip().lower() in ("all", ""):
        requested = _signed_in_variants()
    else:
        selected = _variant_from_key(variant)
        if selected is None:
            raise ValueError(f"unknown variant {variant!r}")
        try:
            requested = [(selected, upstream_module.resolve_credential(selected))]
        except Exception as error:
            raise RuntimeError(f"{selected.display_name} is not signed in: {error}") from error
    if not requested:
        raise RuntimeError(
            "no signed-in WorkBuddy account found; sign in once in the WorkBuddy desktop app"
        )

    compiled: list[tuple[Variant, list[dict[str, Any]]]] = []
    for item_variant, credential in requested:
        models = _models_for(item_variant, credential, force=force_refresh)
        if models:
            compiled.append((item_variant, models))

    path = zcode_config_path()
    document = _read_config(path)
    provider_rules, model_rules = _section(document)

    providers = [
        rule
        for rule in provider_rules["providerRules"]
        if not (isinstance(rule, dict) and rule.get("providerId") in OWNED_PROVIDER_IDS)
    ]
    model_rule_list = [
        rule
        for rule in model_rules["providerModelRules"]
        if not (isinstance(rule, dict) and rule.get("providerId") in OWNED_PROVIDER_IDS)
    ]
    preserved_providers = len(providers)
    preserved_models = len(model_rule_list)
    for item_variant, item_models in compiled:
        provider_id = PROVIDER_IDS[item_variant.id]
        providers.append(_provider_rule(item_variant, item_models, base_url, token))
        model_rule_list.extend(_model_rule(provider_id, model) for model in item_models)

    provider_rules["providerRules"] = providers
    model_rules["providerModelRules"] = model_rule_list

    provider_order = document["config"].get("providerOrder")
    if isinstance(provider_order, list):
        trimmed = [key for key in provider_order if key not in OWNED_PROVIDER_IDS]
        trimmed.extend(PROVIDER_IDS[item_variant.id] for item_variant, _ in compiled)
        document["config"]["providerOrder"] = trimmed

    if not dry_run:
        _write_config(path, document)

    return {
        "ok": True,
        "configPath": str(path),
        "backupPath": str(backup_path()) if backup_path().exists() else None,
        "baseUrl": base_url,
        "written": not dry_run,
        "providers": [
            {
                "providerId": PROVIDER_IDS[item_variant.id],
                "providerName": PROVIDER_NAMES[item_variant.id],
                "variant": item_variant.id,
                "modelCount": len(item_models),
                "models": [model["id"] for model in item_models],
            }
            for item_variant, item_models in compiled
        ],
        "preservedProviders": preserved_providers,
        "preservedModelRules": preserved_models,
        "notice": (
            "ZCode polls this file about once per second; open the model picker again "
            "if the new provider does not appear immediately."
        ),
    }


def unregister() -> dict[str, Any]:
    """Remove only the providers and model rules this plugin owns."""
    path = zcode_config_path()
    if not path.exists():
        return {"ok": True, "configPath": str(path), "removed": 0, "written": False}
    document = _read_config(path)
    provider_rules, model_rules = _section(document)
    before = len(provider_rules["providerRules"]) + len(model_rules["providerModelRules"])
    provider_rules["providerRules"] = [
        rule
        for rule in provider_rules["providerRules"]
        if not (isinstance(rule, dict) and rule.get("providerId") in OWNED_PROVIDER_IDS)
    ]
    model_rules["providerModelRules"] = [
        rule
        for rule in model_rules["providerModelRules"]
        if not (isinstance(rule, dict) and rule.get("providerId") in OWNED_PROVIDER_IDS)
    ]
    provider_order = document["config"].get("providerOrder")
    if isinstance(provider_order, list):
        document["config"]["providerOrder"] = [
            key for key in provider_order if key not in OWNED_PROVIDER_IDS
        ]
    after = len(provider_rules["providerRules"]) + len(model_rules["providerModelRules"])
    _write_config(path, document)
    return {
        "ok": True,
        "configPath": str(path),
        "removed": before - after,
        "written": True,
    }
