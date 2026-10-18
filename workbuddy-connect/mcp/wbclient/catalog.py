"""Model catalog parsing plus the built-in fallback rosters.

The live catalog comes from ``GET /v3/config`` (see upstream.py); these
fallbacks cover the window before the first successful fetch and an offline
start, captured from the same snapshots dsh-workbuddy-connect ships (MIT).
Promotional badges are deliberately NOT baked in: they are time-boxed offers.

Ported from corrinehu/dsh-workbuddy-connect ``src/catalog.ts`` + ``upstream.ts``.
"""

from __future__ import annotations

import json
import time
from typing import Any

EFFORT_VALUES = ("low", "medium", "high", "xhigh", "max")
BADGE_PREFIX = "badge:"


def normalize_credits(credits: str | None) -> str | None:
    """Reduce an upstream credits string (``x0.79 credits``) to ``x0.79``."""
    if credits is None:
        return None
    trimmed = credits.strip()
    if not trimmed:
        return None
    import re

    if re.fullmatch(r"credits?", trimmed, re.IGNORECASE):
        return None
    bare = re.sub(r"\s+credits?$", "", trimmed, flags=re.IGNORECASE).strip()
    return bare or None


def _positive(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0


def _is_object(value: Any) -> bool:
    return isinstance(value, dict)


def resolve_reasoning(wrapped: dict[str, Any]) -> dict[str, Any]:
    supports = wrapped.get("supportsReasoning") is True
    only_reasoning = wrapped.get("onlyReasoning") is True
    supported_efforts = None
    default_effort = None
    can_disable = False
    raw = wrapped.get("reasoning")
    if _is_object(raw):
        efforts = raw.get("supportedEfforts")
        if isinstance(efforts, list):
            valid = [e for e in efforts if isinstance(e, str) and e in EFFORT_VALUES]
            if valid:
                supported_efforts = valid
        for key in ("defaultEffort", "effort"):
            candidate = raw.get(key)
            if isinstance(candidate, str) and candidate in EFFORT_VALUES:
                default_effort = candidate
                break
        can_disable = raw.get("canDisableThinking") is True
    reasoning: dict[str, Any] = {
        "supports": supports,
        "onlyReasoning": only_reasoning,
        "canDisableThinking": can_disable,
    }
    if supported_efforts is not None:
        reasoning["supportedEfforts"] = supported_efforts
    if default_effort is not None:
        reasoning["defaultEffort"] = default_effort
    return reasoning


def resolve_billing(wrapped: dict[str, Any], extra_tags: list[str] | None = None) -> dict[str, Any]:
    raw_credits = wrapped.get("credits")
    credits = raw_credits.strip() if isinstance(raw_credits, str) and raw_credits.strip() else None
    badges: list[str] = []
    raw_tags: list[Any] = []
    if isinstance(wrapped.get("tags"), list):
        raw_tags.extend(wrapped["tags"])
    if extra_tags:
        raw_tags.extend(extra_tags)
    for tag in raw_tags:
        if not isinstance(tag, str) or not tag.lower().startswith(BADGE_PREFIX):
            continue
        label = tag[len(BADGE_PREFIX):].split(":", 1)[0]
        if label and label not in badges:
            badges.append(label)
    multiplier = normalize_credits(credits)
    free = multiplier is not None and _is_zero_multiplier(multiplier)
    billing: dict[str, Any] = {"free": free}
    if credits is not None:
        billing["credits"] = credits
    if badges:
        billing["badges"] = badges
    return billing


def _is_zero_multiplier(multiplier: str) -> bool:
    import re

    return bool(re.fullmatch(r"x?0\.0+", multiplier, re.IGNORECASE))


def parse_promotions(value: Any, model: str) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    promotions: list[dict[str, Any]] = []
    for item in value:
        if not _is_object(item) or item.get("enabled") is not True:
            continue
        model_ids = item.get("modelIds")
        if not isinstance(model_ids, list) or model not in model_ids:
            continue
        schedule = item.get("schedule")
        discount = item.get("discount")
        badge = item.get("badge")
        if not (_is_object(schedule) and _is_object(discount) and _is_object(badge)):
            continue
        if discount.get("displayMode") != "replace":
            continue
        try:
            start = _parse_ms(schedule.get("validFrom"))
            end = _parse_ms(schedule.get("validUntil"))
        except (TypeError, ValueError):
            continue
        factor = discount.get("factor")
        if end <= start or not isinstance(factor, (int, float)) or isinstance(factor, bool) or factor < 0:
            continue
        promotions.append({
            "start": start,
            "end": end,
            "factor": factor,
            "label": badge.get("label") if isinstance(badge.get("label"), str) else "",
            "priority": item.get("priority") if isinstance(item.get("priority"), (int, float)) else 0,
        })
    return promotions


def _parse_ms(value: Any) -> int:
    if not isinstance(value, str) or not value.strip():
        raise TypeError(value)
    from datetime import datetime, timezone

    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp() * 1000)


def model_with_current_promotion(model: dict[str, Any], now_ms: int | None = None) -> dict[str, Any]:
    """Re-evaluate a model's time-boxed promotion against the current time."""
    promotions = model.get("promotions") or []
    if not promotions:
        return model
    now = now_ms if now_ms is not None else int(time.time() * 1000)
    promotion = min(
        (p for p in promotions if now >= p["start"] and now < p["end"]),
        key=lambda p: p["priority"],
        default=None,
    )
    billing = dict(model.get("billing") or {})
    if promotion is None:
        derived_from_promotion = (
            billing.get("free") is True
            or bool(billing.get("badges"))
            or any(p["factor"] != 1 for p in promotions)
        )
        if not derived_from_promotion:
            return model
        return {**model, "billing": {"free": False, "rateUnknown": True}}
    rate = normalize_credits(billing.get("credits"))
    try:
        original = float(rate[1:]) if rate and rate.startswith("x") else None
    except ValueError:
        original = None
    factor = promotion["factor"]
    if factor != 0 and original is None:
        return model
    value = 0 if factor == 0 else original * factor
    billing["credits"] = f"x{value:.2f}"
    billing["free"] = value == 0
    badges = list(billing.get("badges") or [])
    if promotion["label"]:
        badges.append(promotion["label"])
    if badges:
        billing["badges"] = badges
    return {**model, "billing": billing}


def disambiguate_display_names(models: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Make display names unique; the CN document names ``hy3`` and ``hy3-x`` both "Hy3"."""
    seen: set[str] = set()
    result: list[dict[str, Any]] = []
    for model in models:
        key = model["name"].strip().lower()
        if key not in seen:
            seen.add(key)
            result.append(model)
            continue
        tail = model["id"][len(key):] if model["id"].lower().startswith(key) else ""
        if not tail:
            result.append(model)
            continue
        name = f"{model['name']}{tail.upper()}"
        seen.add(name.lower())
        result.append({**model, "name": name})
    return result


def parse_model_catalog(
    data: dict[str, Any],
    international: bool = False,
    promo_badges: dict[str, list[str]] | None = None,
) -> list[dict[str, Any]]:
    """Parse either response shape after its envelope has been checked.

    Membership is the ``cli`` agent's roster intersected with usable rows
    (has a row, not disabled, positive input and output caps).
    """
    raw_models = data.get("models") if isinstance(data.get("models"), list) else []
    agents = data.get("agents") if isinstance(data.get("agents"), list) else []
    cli_ids: list[str] | None = None
    for agent in agents:
        if _is_object(agent) and agent.get("name") == "cli" and isinstance(agent.get("models"), list):
            cli_ids = [m for m in agent["models"] if isinstance(m, str)]
            break
    if not cli_ids:
        raise ValueError("workbuddy model catalog lists no cli agent models")

    by_id: dict[str, dict[str, Any]] = {}
    for raw in raw_models:
        if not _is_object(raw):
            continue
        model_id = raw.get("id") if isinstance(raw.get("id"), str) else ""
        if not model_id or raw.get("disabled") is True:
            continue
        max_input = raw.get("maxInputTokens") if isinstance(raw.get("maxInputTokens"), (int, float)) else 0
        max_output = raw.get("maxOutputTokens") if isinstance(raw.get("maxOutputTokens"), (int, float)) else 0
        if max_input <= 0 or max_output <= 0:
            continue
        context_window_obj = raw.get("contextWindow") if _is_object(raw.get("contextWindow")) else {}
        default_length = context_window_obj.get("defaultLength")
        model: dict[str, Any] = {
            "id": model_id,
            "name": raw.get("name") if isinstance(raw.get("name"), str) and raw.get("name") else model_id,
            "contextWindow": default_length if (international and _positive(default_length)) else max_input,
            "maxTokens": max_output,
            "supportsImages": raw.get("supportsImages") is True and raw.get("disabledMultimodal") is not True,
        }
        model.update(resolve_reasoning(raw))
        model["billing"] = resolve_billing(raw, (promo_badges or {}).get(model_id))
        if international:
            if _positive(default_length):
                model["defaultContextWindow"] = default_length
                model["maxInputTokens"] = max_input
            supported = context_window_obj.get("supportedLengths")
            model["supportedContextWindows"] = (
                [n for n in supported if _positive(n)] if isinstance(supported, list) else []
            )
            promotions = parse_promotions(data.get("modelPromotions"), model_id)
            if promotions:
                model["promotions"] = promotions
        by_id[model_id] = model

    models = [by_id[i] for i in cli_ids if i in by_id]
    if not models:
        raise ValueError("workbuddy model catalog resolved to an empty list")
    return disambiguate_display_names(models)


def _m(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Normalize a built-in row list into the runtime model shape."""
    models: list[dict[str, Any]] = []
    for row in rows:
        model: dict[str, Any] = {
            "id": row["id"],
            "name": row["name"],
            "contextWindow": row["contextWindow"],
            "maxTokens": row["maxTokens"],
            "supportsImages": row["supportsImages"],
        }
        model.update(row.get("reasoning") or {})
        model["billing"] = dict(row.get("billing") or {})
        models.append(model)
    return models


# CN fallback roster (dsh-workbuddy-connect FALLBACK_WORKBUDDY_MODELS snapshot).
BUILTIN_CN = _m([
    {"id": "hy4-preview", "name": "Hy4 preview", "contextWindow": 960_000, "maxTokens": 64_000, "supportsImages": True, "reasoning": {"supports": True, "onlyReasoning": True, "supportedEfforts": ["high"], "defaultEffort": "high", "canDisableThinking": False}, "billing": {"credits": "x0.29", "free": False}},
    {"id": "hy3", "name": "Hy3", "contextWindow": 192_000, "maxTokens": 64_000, "supportsImages": True, "reasoning": {"supports": True, "onlyReasoning": True, "defaultEffort": "high", "canDisableThinking": False}, "billing": {"credits": "x0.00", "free": True}},
    {"id": "hy3-x", "name": "Hy3-X", "contextWindow": 192_000, "maxTokens": 64_000, "supportsImages": True, "reasoning": {"supports": True, "onlyReasoning": True, "defaultEffort": "high", "canDisableThinking": False}, "billing": {"credits": "x0.05", "free": False}},
    {"id": "step-5-preview", "name": "Step-5-Preview", "contextWindow": 1_000_000, "maxTokens": 64_000, "supportsImages": True, "reasoning": {"supports": True, "onlyReasoning": True, "supportedEfforts": ["low", "medium", "high"], "defaultEffort": "high", "canDisableThinking": False}, "billing": {"credits": "x0.00", "free": True}},
    {"id": "space-bunny", "name": "Space-Bunny", "contextWindow": 1_000_000, "maxTokens": 128_000, "supportsImages": True, "reasoning": {"supports": True, "onlyReasoning": True, "supportedEfforts": ["low", "medium", "high", "xhigh", "max"], "defaultEffort": "max", "canDisableThinking": False}, "billing": {"credits": "x0.08", "free": False}},
    {"id": "deepseek-v4.1-flash", "name": "Deepseek-V4.1-Flash", "contextWindow": 1_000_000, "maxTokens": 128_000, "supportsImages": True, "reasoning": {"supports": True, "onlyReasoning": True, "supportedEfforts": ["low", "high", "max"], "defaultEffort": "high", "canDisableThinking": True}, "billing": {"credits": "x0.11", "free": False}},
    {"id": "glm-5.3", "name": "GLM-5.3", "contextWindow": 1_000_000, "maxTokens": 64_000, "supportsImages": True, "reasoning": {"supports": True, "onlyReasoning": True, "supportedEfforts": ["low", "high", "max"], "defaultEffort": "max", "canDisableThinking": False}, "billing": {"credits": "x0.79", "free": False}},
    {"id": "glm-5.3-flash", "name": "GLM-5.3-Flash", "contextWindow": 1_000_000, "maxTokens": 131_072, "supportsImages": True, "reasoning": {"supports": True, "onlyReasoning": True, "supportedEfforts": ["low", "high", "max"], "defaultEffort": "high", "canDisableThinking": False}, "billing": {"credits": "x0.06", "free": False}},
    {"id": "glm-5.3-flashx", "name": "GLM-5.3-FlashX", "contextWindow": 1_000_000, "maxTokens": 131_072, "supportsImages": True, "reasoning": {"supports": True, "onlyReasoning": True, "supportedEfforts": ["low", "high", "max"], "defaultEffort": "high", "canDisableThinking": False}, "billing": {"credits": "x0.14", "free": False}},
    {"id": "glm-5.2", "name": "GLM-5.2", "contextWindow": 1_000_000, "maxTokens": 64_000, "supportsImages": False, "reasoning": {"supports": True, "onlyReasoning": True, "defaultEffort": "medium", "canDisableThinking": False}, "billing": {"credits": "x0.79", "free": False}},
    {"id": "glm-5.1", "name": "GLM-5.1", "contextWindow": 200_000, "maxTokens": 48_000, "supportsImages": False, "reasoning": {"supports": True, "onlyReasoning": True, "defaultEffort": "medium", "canDisableThinking": False}, "billing": {"credits": "x0.79", "free": False}},
    {"id": "glm-5v-turbo", "name": "GLM-5v-Turbo", "contextWindow": 200_000, "maxTokens": 64_000, "supportsImages": True, "reasoning": {"supports": True, "onlyReasoning": True, "defaultEffort": "medium", "canDisableThinking": False}, "billing": {"credits": "x0.71", "free": False}},
    {"id": "minimax-m3-pay", "name": "MiniMax-M3", "contextWindow": 512_000, "maxTokens": 128_000, "supportsImages": True, "reasoning": {"supports": True, "onlyReasoning": True, "defaultEffort": "medium", "canDisableThinking": False}, "billing": {"credits": "x0.25", "free": False}},
    {"id": "minimax-m2.7", "name": "MiniMax-M2.7", "contextWindow": 200_000, "maxTokens": 48_000, "supportsImages": True, "reasoning": {"supports": True, "onlyReasoning": True, "defaultEffort": "medium", "canDisableThinking": False}, "billing": {"credits": "x0.26", "free": False}},
    {"id": "kimi-k3-2", "name": "Kimi-K3", "contextWindow": 1_000_000, "maxTokens": 32_000, "supportsImages": True, "reasoning": {"supports": True, "onlyReasoning": True, "defaultEffort": "medium", "canDisableThinking": False}, "billing": {"credits": "x1.62", "free": False}},
    {"id": "kimi-k2.8-preview", "name": "Kimi-K2.8-Preview", "contextWindow": 1_000_000, "maxTokens": 64_000, "supportsImages": True, "reasoning": {"supports": True, "onlyReasoning": True, "supportedEfforts": ["low", "high", "max"], "defaultEffort": "high", "canDisableThinking": True}, "billing": {"credits": "x0.77", "free": False}},
    {"id": "kimi-k2.7", "name": "Kimi-K2.7-Code", "contextWindow": 256_000, "maxTokens": 32_000, "supportsImages": True, "reasoning": {"supports": True, "onlyReasoning": True, "defaultEffort": "medium", "canDisableThinking": False}, "billing": {"credits": "x0.57", "free": False}},
    {"id": "kimi-k2.6", "name": "Kimi-K2.6", "contextWindow": 256_000, "maxTokens": 32_000, "supportsImages": True, "reasoning": {"supports": True, "onlyReasoning": True, "defaultEffort": "medium", "canDisableThinking": False}, "billing": {"credits": "x0.52", "free": False}},
    {"id": "deepseek-v4-pro", "name": "Deepseek-V4-Pro", "contextWindow": 1_000_000, "maxTokens": 128_000, "supportsImages": True, "reasoning": {"supports": True, "onlyReasoning": True, "defaultEffort": "high", "canDisableThinking": False}, "billing": {"credits": "x0.51", "free": False}},
])

# International fallback roster (FALLBACK_WORKBUDDY_AI_MODELS snapshot).
BUILTIN_GLOBAL = _m([
    {"id": "default-model", "name": "Auto", "contextWindow": 176_000, "maxTokens": 24_000, "supportsImages": True, "reasoning": {"supports": False, "onlyReasoning": False, "canDisableThinking": True}, "billing": {"free": False}},
    {"id": "fast-model", "name": "Fast", "contextWindow": 200_000, "maxTokens": 32_000, "supportsImages": True, "reasoning": {"supports": True, "onlyReasoning": True, "defaultEffort": "medium", "canDisableThinking": False}, "billing": {"credits": "x0.34", "free": False}},
    {"id": "balanced-model", "name": "Balanced", "contextWindow": 256_000, "maxTokens": 32_000, "supportsImages": True, "reasoning": {"supports": True, "onlyReasoning": True, "defaultEffort": "medium", "canDisableThinking": False}, "billing": {"credits": "x0.59", "free": False}},
    {"id": "primary-model", "name": "Primary", "contextWindow": 272_000, "maxTokens": 72_000, "supportsImages": True, "reasoning": {"supports": True, "onlyReasoning": True, "defaultEffort": "high", "canDisableThinking": False}, "billing": {"credits": "x3.31", "free": False}},
    {"id": "deep-model", "name": "Deep", "contextWindow": 176_000, "maxTokens": 24_000, "supportsImages": True, "reasoning": {"supports": False, "onlyReasoning": False, "canDisableThinking": True}, "billing": {"credits": "x3.33", "free": False}},
    {"id": "hy4-preview-f", "name": "Hy4 preview", "contextWindow": 300_000, "defaultContextWindow": 300_000, "supportedContextWindows": [300_000, 1_000_000], "maxTokens": 64_000, "supportsImages": True, "reasoning": {"supports": True, "onlyReasoning": True, "supportedEfforts": ["high"], "defaultEffort": "high", "canDisableThinking": False}, "billing": {"free": False, "rateUnknown": True}},
    {"id": "hy3", "name": "Hy3", "contextWindow": 192_000, "maxTokens": 64_000, "supportsImages": True, "reasoning": {"supports": True, "onlyReasoning": True, "supportedEfforts": ["low", "high"], "defaultEffort": "high", "canDisableThinking": False}, "billing": {"free": False, "rateUnknown": True}},
    {"id": "deepseek-v4.1-flash", "name": "Deepseek-V4.1-Flash", "contextWindow": 300_000, "defaultContextWindow": 300_000, "supportedContextWindows": [300_000, 1_000_000], "maxTokens": 128_000, "supportsImages": True, "reasoning": {"supports": True, "onlyReasoning": True, "defaultEffort": "high", "canDisableThinking": False}, "billing": {"free": False, "rateUnknown": True}},
    {"id": "gpt-6-astra", "name": "GPT-6-Astra", "contextWindow": 400_000, "defaultContextWindow": 400_000, "supportedContextWindows": [400_000, 1_000_000], "maxTokens": 128_000, "supportsImages": True, "reasoning": {"supports": True, "onlyReasoning": True, "supportedEfforts": ["low", "medium", "high", "xhigh", "max"], "defaultEffort": "medium", "canDisableThinking": True}, "billing": {"credits": "x6.67", "free": False}},
    {"id": "gpt-5.6-sol", "name": "GPT-5.6-Sol", "contextWindow": 1_000_000, "maxTokens": 128_000, "supportsImages": True, "reasoning": {"supports": True, "onlyReasoning": True, "supportedEfforts": ["low", "medium", "high", "xhigh", "max"], "defaultEffort": "medium", "canDisableThinking": True}, "billing": {"credits": "x3.47", "free": False}},
    {"id": "gpt-5.6-terra", "name": "GPT-5.6-Terra", "contextWindow": 1_000_000, "maxTokens": 128_000, "supportsImages": True, "reasoning": {"supports": True, "onlyReasoning": True, "supportedEfforts": ["low", "medium", "high", "xhigh", "max"], "defaultEffort": "medium", "canDisableThinking": True}, "billing": {"credits": "x1.39", "free": False}},
    {"id": "gpt-5.6-luna", "name": "GPT-5.6-Luna", "contextWindow": 1_000_000, "maxTokens": 128_000, "supportsImages": True, "reasoning": {"supports": True, "onlyReasoning": True, "supportedEfforts": ["low", "medium", "high", "xhigh", "max"], "defaultEffort": "medium", "canDisableThinking": True}, "billing": {"credits": "x0.14", "free": False}},
    {"id": "gpt-5.5", "name": "GPT-5.5", "contextWindow": 1_000_000, "maxTokens": 128_000, "supportsImages": True, "reasoning": {"supports": True, "onlyReasoning": True, "supportedEfforts": ["low", "medium", "high", "xhigh"], "defaultEffort": "medium", "canDisableThinking": False}, "billing": {"credits": "x3.31", "free": False}},
    {"id": "gpt-5.4", "name": "GPT-5.4", "contextWindow": 272_000, "maxTokens": 72_000, "supportsImages": True, "reasoning": {"supports": True, "onlyReasoning": True, "supportedEfforts": ["low", "medium", "high", "xhigh"], "defaultEffort": "medium", "canDisableThinking": False}, "billing": {"credits": "x1.65", "free": False}},
    {"id": "gpt-5.3-codex", "name": "GPT-5.3-Codex", "contextWindow": 272_000, "maxTokens": 72_000, "supportsImages": True, "reasoning": {"supports": True, "onlyReasoning": True, "defaultEffort": "medium", "canDisableThinking": False}, "billing": {"credits": "x1.25", "free": False}},
    {"id": "gemini-3.5-flash", "name": "Gemini-3.5-Flash", "contextWindow": 1_000_000, "maxTokens": 65_536, "supportsImages": True, "reasoning": {"supports": True, "onlyReasoning": True, "defaultEffort": "medium", "canDisableThinking": False}, "billing": {"credits": "x0.99", "free": False}},
    {"id": "glm-5.3", "name": "GLM-5.3", "contextWindow": 1_000_000, "maxTokens": 48_000, "supportsImages": True, "reasoning": {"supports": True, "onlyReasoning": True, "supportedEfforts": ["low", "high", "max"], "defaultEffort": "high", "canDisableThinking": True}, "billing": {"credits": "x0.79", "free": False}},
    {"id": "glm-5.2", "name": "GLM-5.2", "contextWindow": 1_000_000, "maxTokens": 48_000, "supportsImages": True, "reasoning": {"supports": True, "onlyReasoning": True, "supportedEfforts": ["high", "xhigh"], "defaultEffort": "high", "canDisableThinking": True}, "billing": {"credits": "x0.79", "free": False}},
    {"id": "kimi-k3", "name": "Kimi-K3", "contextWindow": 1_000_000, "maxTokens": 32_000, "supportsImages": True, "reasoning": {"supports": True, "onlyReasoning": True, "defaultEffort": "medium", "canDisableThinking": False}, "billing": {"credits": "x1.62", "free": False}},
    {"id": "kimi-k2.6", "name": "Kimi-K2.6", "contextWindow": 256_000, "maxTokens": 32_000, "supportsImages": True, "reasoning": {"supports": True, "onlyReasoning": True, "defaultEffort": "medium", "canDisableThinking": False}, "billing": {"credits": "x0.52", "free": False}},
])


def builtin_models(variant) -> list[dict[str, Any]]:
    return BUILTIN_CN if variant.region == "cn" else BUILTIN_GLOBAL


CATALOG_CACHE_TTL_MS = 30 * 60 * 1000


def load_cached_models(variant) -> dict[str, Any] | None:
    """Load the persisted catalog cache; ``None`` when absent or unreadable."""
    from .variants import catalog_cache_path

    path = catalog_cache_path(variant)
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(document, dict) or not isinstance(document.get("models"), list):
        return None
    return document


def save_cached_models(variant, models: list[dict[str, Any]], source: str) -> None:
    from .variants import catalog_cache_path, state_home

    directory = state_home()
    try:
        directory.mkdir(parents=True, exist_ok=True)
        document = {"fetchedAtMs": int(time.time() * 1000), "source": source, "models": models}
        path = catalog_cache_path(variant)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        pass
