"""WorkBuddy upstream HTTP client: chat streaming, token refresh, catalog, credits.

The wire behavior is ported from
[corrinehu/dsh-workbuddy-connect](https://github.com/corrinehu/dsh-workbuddy-connect)
``src/upstream.ts``
(MIT License, Copyright © 2026 Corrine Hu),
whose protocol work itself credits
[Sliverkiss/workbuddy2api](https://github.com/Sliverkiss/workbuddy2api)
(MIT). Uses only the Python standard library.

These are private client interfaces, not an official open API; a WorkBuddy
update may require adjusting endpoints here.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
import uuid
from typing import Any, Callable, Iterator

from . import auth as auth_module
from . import catalog as catalog_module
from .variants import CN as CN_VARIANT
from .variants import GLOBAL as GLOBAL_VARIANT
from .variants import Variant, region_of

CN_CHAT_BASE = "https://copilot.tencent.com"
CN_BILLING_BASE = "https://www.codebuddy.cn"
GLOBAL_BASE = "https://www.workbuddy.ai"

CLIENT_UA = "CLI/2.63.2 CodeBuddy/2.63.2"
FALLBACK_CN_APP_VERSION = "5.5.6"
FALLBACK_GLOBAL_APP_VERSION = "5.5.2"
JSON_TIMEOUT_S = 30
ERROR_BODY_LIMIT = 4096

HARD_CREDIT_MARKERS = (
    "insufficient credit", "no credit", "credit exhausted", "credits exhausted", "out of credit",
    "quota exceeded", "quota exhaust", "payment required", "credit not enough", "not enough credit",
    "积分不足", "额度不足", "余额不足", "积分用完", "额度用尽", "没有积分",
)
SESSION_DEAD_MARKERS = ("Offline user session not found", "12153")

INTERNATIONAL_SYSTEM_PROMPT = "You are a helpful assistant."


class UpstreamError(Exception):
    """A classified upstream failure (kind mirrors the reference's error kinds)."""

    def __init__(self, kind: str, message: str, status: int = 0):
        super().__init__(message)
        self.kind = kind
        self.status = status


def chat_base(credential: dict[str, Any]) -> str:
    return GLOBAL_BASE if region_of(credential.get("domain", "")) == "global" else CN_CHAT_BASE


def billing_base(credential: dict[str, Any]) -> str:
    return GLOBAL_BASE if region_of(credential.get("domain", "")) == "global" else CN_BILLING_BASE


def origin_referer(credential: dict[str, Any]) -> str:
    # CN chat presents the codebuddy.cn origin; the global product its own base.
    return GLOBAL_BASE if region_of(credential.get("domain", "")) == "global" else CN_BILLING_BASE


def classify_upstream_error(status: int, body: str) -> str:
    if status == 402:
        return "hard_credit"
    if status == 401:
        return "session_dead"
    lowered = body.lower()
    for marker in HARD_CREDIT_MARKERS:
        if marker.lower() in lowered or marker in body:
            return "hard_credit"
    for marker in SESSION_DEAD_MARKERS:
        if marker in body:
            return "session_dead"
    if status == 429:
        return "soft_rate"
    if status == 404:
        return "not_found"
    if status >= 500:
        return "server"
    return "client"


def extract_display_error_message(body: str) -> str | None:
    """Prefer WorkBuddy's localized ``displayMsg.zh``/``displayMsg.en``, then ``msg``."""
    trimmed = body.strip()
    if not trimmed.startswith("{"):
        return None
    try:
        parsed = json.loads(trimmed)
    except ValueError:
        return None
    if not isinstance(parsed, dict):
        return None
    display_msg = parsed.get("displayMsg")
    if isinstance(display_msg, dict):
        for key in ("zh", "en"):
            value = display_msg.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    message = parsed.get("msg")
    if isinstance(message, str) and message.strip():
        return message.strip()
    return None


# ---------------------------------------------------------------------------
# Chat identity (desktop-shaped User-Agent for chat requests)


def _valid_version(value: Any) -> bool:
    import re

    return isinstance(value, str) and bool(re.fullmatch(r"\d{1,6}(?:\.\d{1,6}){1,3}", value))


def _valid_cli_version(value: Any) -> bool:
    import re

    return isinstance(value, str) and bool(
        re.fullmatch(r"\d{1,6}(?:\.\d{1,6}){1,3}(?:-[0-9A-Za-z.]+)?", value)
    )


def read_cli_version_from_install(install_dir: str) -> str | None:
    """Bundled agent CLI version from an install layout; best-effort, never throws."""
    import os

    package_path = os.path.join(install_dir, "resources", "app.asar.unpacked", "cli", "package.json")
    try:
        with open(package_path, encoding="utf-8") as handle:
            package = json.load(handle)
    except (OSError, ValueError):
        return None
    if not isinstance(package, dict):
        return None
    declared = package.get("version")
    if _valid_cli_version(declared) and declared != "0.0.0":
        return declared
    publish_config = package.get("publishConfig")
    if isinstance(publish_config, dict):
        custom = publish_config.get("customPackage")
        if isinstance(custom, dict) and _valid_cli_version(custom.get("version")):
            return custom["version"]
    return None


def _installed_version_from_electron(variant: Variant) -> str | None:
    """Read the ``version`` file beside a located Electron binary, when found."""
    provider = auth_module.key_provider_for_diagnostics(variant)
    helper = provider.helper_path()
    if not helper:
        return None
    import os

    version_path = os.path.join(os.path.dirname(helper), "version")
    try:
        with open(version_path, encoding="utf-8") as handle:
            version = handle.read().strip()
    except OSError:
        return None
    return version if _valid_version(version) else None


def resolve_chat_identity(variant: Variant) -> dict[str, Any]:
    """Installed App (via its ``version`` file) → saved cache → compiled-in fallback.

    Never throws and never blocks a message: every failure degrades to the
    built-in fallback version without the ``CLI/…`` token.
    """
    from .variants import version_cache_path

    fallback = FALLBACK_CN_APP_VERSION if variant.region == "cn" else FALLBACK_GLOBAL_APP_VERSION
    client_version = _installed_version_from_electron(variant)
    source = "installed" if client_version else None
    cli_version = None
    if client_version:
        provider = auth_module.key_provider_for_diagnostics(variant)
        helper = provider.helper_path()
        if helper:
            import os

            cli_version = read_cli_version_from_install(os.path.dirname(helper))
    if not client_version:
        try:
            saved = json.loads(version_cache_path(variant).read_text(encoding="utf-8"))
            if isinstance(saved, dict) and _valid_version(saved.get("version")):
                client_version = saved["version"]
                source = "saved"
        except (OSError, ValueError):
            pass
    if not client_version:
        return {"clientVersion": fallback, "source": "fallback"}
    identity: dict[str, Any] = {"clientVersion": client_version, "source": source}
    if _valid_cli_version(cli_version):
        identity["cliVersion"] = cli_version
    # Best-effort remember of the version so a moved app does not break the UA.
    try:
        from .variants import state_home

        version_cache_path(variant).parent.mkdir(parents=True, exist_ok=True)
        version_cache_path(variant).write_text(
            json.dumps({"version": client_version, "observedAt": int(time.time() * 1000)}, indent=2) + "\n",
            encoding="utf-8",
        )
    except OSError:
        pass
    return identity


def chat_user_agent(identity: dict[str, Any], region: str) -> str:
    product = "WorkBuddy AI" if region == "global" else "WorkBuddy"
    parts = [f"WorkBuddy/{identity['clientVersion']}", f"{product}/{identity['clientVersion']}"]
    if identity.get("cliVersion"):
        parts.append(f"CLI/{identity['cliVersion']}")
    return " ".join(parts)


def app_user_agent(version: str) -> str:
    """The App-shaped catalog UA; ``WorkBuddyAI/<v>`` (no space) is the measured form."""
    if not _valid_version(version):
        raise ValueError(f"invalid WorkBuddy AI version for User-Agent: {version!r}")
    return f"WorkBuddyAI/{version}"


# ---------------------------------------------------------------------------
# Headers


def common_headers(credential: dict[str, Any]) -> dict[str, str]:
    origin = origin_referer(credential)
    return {
        "Accept": "application/json, text/plain, */*",
        "X-Requested-With": "XMLHttpRequest",
        "Origin": origin,
        "Referer": f"{origin}/",
        "User-Agent": CLIENT_UA,
    }


def conversation_headers() -> dict[str, str]:
    """The trio the chat endpoints require (fresh UUIDs mark the channel)."""
    return {
        "X-Conversation-ID": str(uuid.uuid4()),
        "X-Conversation-Request-ID": str(uuid.uuid4()),
        "X-Conversation-Message-ID": str(uuid.uuid4()),
    }


def chat_headers(credential: dict[str, Any], identity: dict[str, Any], region: str) -> dict[str, str]:
    enterprise_id = credential.get("enterpriseId")
    headers = common_headers(credential)
    headers.update({
        "User-Agent": chat_user_agent(identity, region),
        "Content-Type": "application/json",
        # Chat requests never carry the refresh token.
        **({"X-No-User-Id": "1"} if not credential.get("uid") else {"X-User-Id": credential["uid"]}),
        **(
            {"X-No-Enterprise-Id": "1"}
            if not enterprise_id
            else {"X-Enterprise-Id": enterprise_id}
        ),
        **(
            {"X-No-Department-Info": "1"}
            if not credential.get("domain")
            else {"X-Domain": credential["domain"]}
        ),
        "X-IDE-Type": "WorkBuddy",
        "X-IDE-Name": "WorkBuddy",
        "X-IDE-Version": identity["clientVersion"],
        "X-Product": "SaaS",
        **conversation_headers(),
    })
    return headers


def refresh_headers(credential: dict[str, Any]) -> dict[str, str]:
    headers = common_headers(credential)
    headers["X-Refresh-Token"] = credential.get("refreshToken", "")
    headers["X-Auth-Refresh-Source"] = "workbuddy"
    enterprise_id = credential.get("enterpriseId")
    if enterprise_id:
        headers["X-Enterprise-Id"] = enterprise_id
    return headers


def billing_headers(credential: dict[str, Any]) -> dict[str, str]:
    headers = {
        "Authorization": f"Bearer {credential.get('accessToken', '')}",
        "Accept": "application/json",
        "Content-Type": "application/json",
        # The billing gateway rejects the Python default UA with 403/10085;
        # present the same client identity the other requests carry.
        "User-Agent": CLIENT_UA,
    }
    if credential.get("uid"):
        headers["X-User-Id"] = credential["uid"]
    enterprise_id = credential.get("enterpriseId")
    if enterprise_id:
        headers["X-Enterprise-Id"] = enterprise_id
        headers["X-Tenant-Id"] = enterprise_id
    if credential.get("domain"):
        headers["X-Domain"] = credential["domain"]
    return headers


def identity_headers(credential: dict[str, Any]) -> dict[str, str]:
    """CN catalog account identity; the pair is all-or-nothing for enterprise rosters."""
    uid = credential.get("uid", "")
    enterprise_id = credential.get("enterpriseId")
    if not uid or not enterprise_id:
        return {}
    headers = {"X-User-Id": uid, "X-Enterprise-Id": enterprise_id, "X-Tenant-Id": enterprise_id}
    if credential.get("domain"):
        headers["X-Domain"] = credential["domain"]
    return headers


# ---------------------------------------------------------------------------
# HTTP plumbing


def _http_json(
    method: str,
    url: str,
    headers: dict[str, str],
    body: dict[str, Any] | None = None,
    timeout: int = JSON_TIMEOUT_S,
) -> tuple[int, Any, str]:
    """One JSON round-trip; returns (status, parsed|None, raw_text)."""
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = response.status
            text = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as error:
        status = error.code
        text = error.read().decode("utf-8", errors="replace")[:ERROR_BODY_LIMIT]
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise UpstreamError("server", f"transport error: {error}") from error
    try:
        parsed = json.loads(text)
    except ValueError:
        parsed = None
    return status, parsed, text


def _read_envelope(status: int, parsed: Any, text: str) -> dict[str, Any]:
    """Unwrap ``{code,msg,data}``; tolerate the bare-document shape."""
    if parsed is None:
        raise UpstreamError(
            classify_upstream_error(status, text),
            f"workbuddy upstream returned non-JSON (http {status}): {text[:160]}",
            status,
        )
    if not isinstance(parsed, dict):
        raise UpstreamError("client", f"workbuddy upstream returned an unexpected document (http {status})", status)
    document = parsed
    if status >= 400 or (isinstance(document.get("code"), int) and document["code"] != 0 and "data" in document):
        kind = classify_upstream_error(status, document.get("msg", "") or text)
        message = extract_display_error_message(text) or (document.get("msg") or text[:160])
        raise UpstreamError(kind, f"workbuddy upstream {kind} (http {status}): {message}", status)
    return document


# ---------------------------------------------------------------------------
# Token refresh


def refresh_token(credential: dict[str, Any]) -> dict[str, Any]:
    """POST the refresh endpoint; returns {accessToken, refreshToken?, expiresInSec?, domain?}."""
    url = f"{chat_base(credential)}/v2/plugin/auth/token/refresh"
    status, parsed, text = _http_json("POST", url, refresh_headers(credential))
    document = _read_envelope(status, parsed, text)
    data = document.get("data") if isinstance(document.get("data"), dict) else {}
    access_token = data.get("accessToken")
    if not isinstance(access_token, str) or access_token == "":
        raise UpstreamError(
            "session_dead",
            "workbuddy token refresh returned no accessToken; sign in again in the WorkBuddy app",
            status,
        )
    outcome: dict[str, Any] = {"accessToken": access_token}
    if isinstance(data.get("refreshToken"), str) and data["refreshToken"]:
        outcome["refreshToken"] = data["refreshToken"]
    if isinstance(data.get("expiresIn"), (int, float)) and data["expiresIn"] > 0:
        outcome["expiresInSec"] = data["expiresIn"]
    if isinstance(data.get("domain"), str) and data["domain"]:
        outcome["domain"] = data["domain"]
    return outcome


# ---------------------------------------------------------------------------
# Model catalog


def _fetch_promo_badges(credential: dict[str, Any]) -> dict[str, list[str]] | None:
    """Console document's ``badge:*`` tags by model id; best-effort, never fails."""
    try:
        url = f"{chat_base(credential)}/console/enterprises/personal/models"
        headers = common_headers(credential)
        headers.update({
            "Authorization": f"Bearer {credential.get('accessToken', '')}",
            "Accept": "application/json",
            **identity_headers(credential),
            "User-Agent": CLIENT_UA,
        })
        status, parsed, _text = _http_json("GET", url, headers)
        if status >= 400:
            return None
        document = _read_envelope(status, parsed, "")
        data = document.get("data") if isinstance(document.get("data"), dict) else document
        raw_models = data.get("models") if isinstance(data.get("models"), list) else []
        badges: dict[str, list[str]] = {}
        for model in raw_models:
            if not isinstance(model, dict):
                continue
            model_id = model.get("id")
            tags = model.get("tags") if isinstance(model.get("tags"), list) else []
            valid = [
                tag
                for tag in tags
                if isinstance(tag, str) and tag.lower().startswith(catalog_module.BADGE_PREFIX)
            ]
            if isinstance(model_id, str) and model_id and valid:
                badges[model_id] = valid
        return badges or None
    except Exception:
        return None


def fetch_live_models(variant: Variant, credential: dict[str, Any]) -> tuple[list[dict[str, Any]], str]:
    """GET ``/v3/config`` and parse the cli roster; returns (models, source_label)."""
    international = variant.region == "global"
    headers = {
        "Authorization": f"Bearer {credential.get('accessToken', '')}",
        "Accept": "application/json",
        "Origin": origin_referer(credential),
        "Referer": f"{origin_referer(credential)}/",
    }
    if international:
        identity = resolve_app_version_identity(variant)
        headers["User-Agent"] = app_user_agent(identity["version"])
        headers["X-Requested-With"] = "XMLHttpRequest"
        headers["X-Product"] = "SaaS"
    else:
        headers["User-Agent"] = CLIENT_UA
        headers.update(identity_headers(credential))
    status, parsed, text = _http_json("GET", f"{chat_base(credential)}/v3/config", headers)
    document = _read_envelope(status, parsed, text)
    data = (
        document.get("data")
        if isinstance(document.get("data"), dict)
        else document
        if ("models" in document or "agents" in document)
        else {}
    )
    promo_badges = None if international else _fetch_promo_badges(credential)
    models = catalog_module.parse_model_catalog(data, international, promo_badges)
    source = "workbuddy-ai:app" if international else "workbuddy:cli"
    return models, source


def resolve_app_version_identity(variant: Variant) -> dict[str, Any]:
    """Version for the international catalog UA: installed → saved → fallback."""
    from .variants import version_cache_path

    installed = _installed_version_from_electron(variant)
    if installed:
        return {"version": installed, "source": "installed"}
    try:
        saved = json.loads(version_cache_path(variant).read_text(encoding="utf-8"))
        if isinstance(saved, dict) and _valid_version(saved.get("version")):
            return {"version": saved["version"], "source": "saved"}
    except (OSError, ValueError):
        pass
    return {"version": FALLBACK_GLOBAL_APP_VERSION, "source": "fallback"}


def fetch_models(
    variant: Variant,
    credential: dict[str, Any] | None,
    force: bool = False,
) -> dict[str, Any]:
    """Catalog with the full fallback chain: live → cache (incl. stale) → built-in."""
    now_ms = int(time.time() * 1000)
    cached = catalog_module.load_cached_models(variant)
    if not force:
        if cached and cached.get("fetchedAtMs", 0) + catalog_module.CATALOG_CACHE_TTL_MS > now_ms:
            return {
                "source": "cache",
                "fetchedAtMs": cached.get("fetchedAtMs"),
                "models": cached["models"],
            }
    if credential is not None:
        try:
            models, source = fetch_live_models(variant, credential)
            catalog_module.save_cached_models(variant, models, source)
            return {"source": source, "fetchedAtMs": now_ms, "models": models}
        except Exception as error:
            if cached:
                return {
                    "source": "cache-stale",
                    "fetchedAtMs": cached.get("fetchedAtMs"),
                    "models": cached["models"],
                    "notice": f"live catalog fetch failed ({error}); serving the last saved catalog",
                }
            raise
    if cached:
        return {
            "source": "cache-stale",
            "fetchedAtMs": cached.get("fetchedAtMs"),
            "models": cached["models"],
            "notice": "no usable credential; serving the last saved catalog",
        }
    return {
        "source": "builtin-fallback",
        "models": catalog_module.builtin_models(variant),
        "notice": (
            f"no usable credential and no saved catalog; serving the built-in {variant.display_name} "
            "fallback roster (a snapshot, not a live list)"
        ),
    }


# ---------------------------------------------------------------------------
# Credits / billing


def _local_stamp(seconds: float) -> str:
    struct = time.localtime(seconds)
    return time.strftime("%Y-%m-%d %H:%M:%S", struct)


def _parse_cycle_end_time(value: Any) -> int | None:
    """Parse the zone-less ``YYYY-MM-DD HH:mm:ss`` stamp as local time, strictly."""
    import re

    if not isinstance(value, str) or not value.strip():
        return None
    match = re.fullmatch(
        r"(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2}):(\d{2})", value.strip()
    )
    if not match:
        return None
    year, month, day, hour, minute, second = (int(g) for g in match.groups())
    try:
        stamp = (year, month, day, hour, minute, second, 0, 0, -1)
        parsed = time.mktime(stamp)
        roundtrip = time.localtime(parsed)
    except (OverflowError, ValueError):
        return None
    expected = (year, month, day, hour, minute, second)
    actual = (roundtrip.tm_year, roundtrip.tm_mon, roundtrip.tm_mday, roundtrip.tm_hour, roundtrip.tm_min, roundtrip.tm_sec)
    return int(parsed * 1000) if actual == expected else None


def _expiring_within(accounts: list[dict[str, Any]], now_ms: int, horizon_ms: int) -> dict[str, Any]:
    total = 0
    packages = 0
    earliest = None
    for account in accounts:
        end = account.get("endTimeMs")
        if end is None or account.get("remain", 0) <= 0:
            continue
        if not (end > now_ms and end - now_ms <= horizon_ms):
            continue
        total += account["remain"]
        packages += 1
        if earliest is None or end < earliest:
            earliest = end
    result: dict[str, Any] = {"total": total, "packages": packages}
    if earliest is not None:
        result["earliestEndTimeMs"] = earliest
    return result


def _summarise_expiry(accounts: list[dict[str, Any]], now_ms: int) -> dict[str, Any]:
    if not any(a.get("endTimeMs") is not None for a in accounts):
        return {}
    hour = 3_600_000
    day = 24 * hour
    return {
        "expiry": {
            "within24h": _expiring_within(accounts, now_ms, day),
            "within3d": _expiring_within(accounts, now_ms, 3 * day),
            "within7d": _expiring_within(accounts, now_ms, 7 * day),
        }
    }


def fetch_credits(credential: dict[str, Any]) -> dict[str, Any]:
    """Aggregated remaining credit; enterprise accounts take the cycle-quota path."""
    if region_of(credential.get("domain", "")) == "cn" and credential.get("enterpriseId"):
        return _fetch_enterprise_credits(credential)
    now = time.time()
    url = f"{billing_base(credential)}/v2/billing/meter/get-user-resource"
    body = {
        "PageNumber": 1,
        "PageSize": 100,
        "ProductCode": "p_tcaca",
        "Status": [0, 3],
        "PackageEndTimeRangeBegin": _local_stamp(now),
        "PackageEndTimeRangeEnd": _local_stamp(now + 365 * 101 * 24 * 3600),
    }
    status, parsed, text = _http_json("POST", url, billing_headers(credential), body)
    document = _read_envelope(status, parsed, text)
    wrapper = document.get("data") if isinstance(document.get("data"), dict) else {}
    response = wrapper.get("Response") if isinstance(wrapper.get("Response"), dict) else {}
    data = response.get("Data") if isinstance(response.get("Data"), dict) else {}
    raw_accounts = data.get("Accounts") if isinstance(data.get("Accounts"), list) else []
    accounts: list[dict[str, Any]] = []
    total = 0
    for raw in raw_accounts:
        if not isinstance(raw, dict):
            continue

        def number(key: str, _raw: dict[str, Any] = raw) -> int:
            value = _raw.get(key)
            return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0

        size = number("CycleCapacitySize")
        cycle_remain = number("CycleCapacityRemain")
        cycle_used = number("CycleCapacityUsed")
        capacity_remain = number("CapacityRemain")
        if size > 0 or cycle_remain > 0 or cycle_used > 0:
            remain = cycle_remain
        else:
            remain = capacity_remain
        remain = max(remain, 0)
        total += remain
        account: dict[str, Any] = {
            "packageName": raw.get("PackageName") if isinstance(raw.get("PackageName"), str) else "(unnamed)",
            "remain": remain,
            "size": size if size > 0 else number("CapacitySize"),
        }
        end_ms = _parse_cycle_end_time(raw.get("CycleEndTime"))
        if end_ms is not None:
            account["endTimeMs"] = end_ms
        accounts.append(account)
    result: dict[str, Any] = {"total": total, "accounts": accounts}
    result.update(_summarise_expiry(accounts, int(now * 1000)))
    return result


def _fetch_enterprise_credits(credential: dict[str, Any]) -> dict[str, Any]:
    """CN enterprise read: one cycle quota; ``-1`` is the upstream's no-cap marker."""
    status, parsed, text = _http_json(
        "POST",
        f"{CN_BILLING_BASE}/v2/billing/meter/get-enterprise-user-usage",
        billing_headers(credential),
        {},
    )
    document = _read_envelope(status, parsed, text)

    def describe_shape(value: Any) -> str:
        if not isinstance(value, dict):
            return type(value).__name__
        top = ", ".join(f"{k}:{type(v).__name__}" for k, v in list(value.items())[:24]) or "(empty)"
        inner = value.get("data")
        if isinstance(inner, dict):
            return f"top-level {{ {top} }}; data {{ {', '.join(inner.keys())} }}"
        return f"top-level {{ {top} }}"

    sources: list[dict[str, Any]] = []
    for candidate in (document.get("data"), document):
        if not isinstance(candidate, dict):
            continue
        if isinstance(candidate.get("data"), dict):
            sources.append(candidate["data"])
        sources.append(candidate)
    limit = used = None
    reset_time = None
    for source in sources:
        limit = _number_at(source, "limitNum", "limit_num")
        if limit is None:
            continue
        used = _number_at(source, "credit", "used_num")
        value = source.get("cycleResetTime")
        if isinstance(value, str) and value:
            reset_time = value
        break
    if limit is None:
        raise UpstreamError(
            "client",
            "workbuddy enterprise billing response carried no recognised quota field "
            f"(expected limitNum/limit_num + credit/used_num; received {describe_shape(document)})",
            status,
        )
    result: dict[str, Any]
    if limit == -1:
        result = {
            "total": 0,
            "accounts": [{"packageName": "enterprise", "remain": 0, "size": 0, "unlimited": True}],
            "unlimited": True,
        }
    else:
        if used is None:
            raise UpstreamError(
                "client",
                "workbuddy enterprise billing response carried a quota limit but no recognised usage field "
                f"(expected credit/used_num alongside limitNum/limit_num; received {describe_shape(document)})",
                status,
            )
        remain = max(limit - used, 0)
        result = {"total": remain, "accounts": [{"packageName": "enterprise", "remain": remain, "size": limit}]}
    if reset_time:
        result["cycleResetTime"] = reset_time
    return result


def _number_at(source: dict[str, Any], *keys: str) -> int | None:
    for key in keys:
        value = source.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return int(value)
    return None


# ---------------------------------------------------------------------------
# Chat


def prepare_chat_body(body: dict[str, Any], international: bool) -> dict[str, Any]:
    """Normalize an OpenAI-style body for the WorkBuddy upstream.

    Forces ``stream`` (the upstream rejects non-streaming), rewrites
    ``developer`` messages as ``system``, flattens ``tool_choice``, drops the
    adapter's ``off`` effort spelling, and — international only — prepends the
    system prompt the gateway requires.
    """
    prepared = dict(body)
    prepared["stream"] = True
    messages = prepared.get("messages")
    if isinstance(messages, list):
        normalized: list[Any] = []
        for message in messages:
            if isinstance(message, dict) and message.get("role") == "developer":
                message = {**message, "role": "system"}
            normalized.append(message)
        prepared["messages"] = normalized
    if "tool_choice" in prepared:
        choice = prepared["tool_choice"]
        if isinstance(choice, str):
            if choice.strip().lower() == "none":
                prepared.pop("tool_choice", None)
                prepared.pop("tools", None)
                prepared.pop("functions", None)
        elif isinstance(choice, dict):
            kind = str(choice.get("type", "")).strip().lower()
            if kind == "none":
                prepared.pop("tool_choice", None)
                prepared.pop("tools", None)
                prepared.pop("functions", None)
            elif kind in ("auto", "required"):
                prepared["tool_choice"] = kind
            elif kind == "function":
                fn = choice.get("function") if isinstance(choice.get("function"), dict) else {}
                name = str(fn.get("name") or choice.get("name") or "").strip()
                prepared["tool_choice"] = name or "auto"
            else:
                prepared.pop("tool_choice", None)
        else:
            prepared.pop("tool_choice", None)
    if prepared.get("reasoning_effort") == "off":
        prepared.pop("reasoning_effort", None)
    if international and isinstance(prepared.get("messages"), list):
        messages = prepared["messages"]
        if not (messages and isinstance(messages[0], dict) and messages[0].get("role") == "system"):
            prepared["messages"] = [{"role": "system", "content": INTERNATIONAL_SYSTEM_PROMPT}, *messages]
    return prepared


def chat_stream_events(
    credential: dict[str, Any],
    body: dict[str, Any],
    timeout_s: int = 180,
) -> Iterator[dict[str, Any]]:
    """POST ``/v2/chat/completions`` and yield parsed SSE event payloads.

    The caller aggregates deltas; a classified failure raises UpstreamError.
    """
    international = region_of(credential.get("domain", "")) == "global"
    prepared = prepare_chat_body(body, international)
    try:
        identity = resolve_chat_identity(GLOBAL_VARIANT if international else CN_VARIANT)
    except Exception:
        identity = None
    if identity is None:
        fallback = FALLBACK_GLOBAL_APP_VERSION if international else FALLBACK_CN_APP_VERSION
        identity = {"clientVersion": fallback}
    url = f"{chat_base(credential)}/v2/chat/completions"
    headers = chat_headers(credential, identity, "global" if international else "cn")
    headers["Authorization"] = f"Bearer {credential.get('accessToken', '')}"
    request = urllib.request.Request(
        url, data=json.dumps(prepared).encode("utf-8"), headers=headers, method="POST"
    )
    try:
        response = urllib.request.urlopen(request, timeout=timeout_s)
    except urllib.error.HTTPError as error:
        text = error.read().decode("utf-8", errors="replace")[:ERROR_BODY_LIMIT]
        kind = classify_upstream_error(error.code, text)
        message = extract_display_error_message(text) or text[:200]
        raise UpstreamError(kind, f"(http {error.code}) {message}", error.code) from error
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise UpstreamError("server", f"transport error: {error}") from error
    try:
        content_type = response.headers.get_content_type()
        if content_type == "application/json":
            text = response.read().decode("utf-8", errors="replace")
            parsed = json.loads(text) if text.strip() else {}
            if isinstance(parsed, dict) and parsed.get("error"):
                raise UpstreamError("client", json.dumps(parsed["error"], ensure_ascii=False)[:400])
            yield parsed
            return
        for raw_line in response:
            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload == "[DONE]":
                return
            try:
                event = json.loads(payload)
            except ValueError:
                continue
            if isinstance(event, dict) and event.get("error"):
                raise UpstreamError(
                    "client", json.dumps(event["error"], ensure_ascii=False)[:400]
                )
            yield event
    finally:
        response.close()


def chat_once(credential: dict[str, Any], body: dict[str, Any], timeout_s: int = 180) -> dict[str, Any]:
    """Aggregate one streaming chat answer into text plus metadata."""
    content_parts: list[str] = []
    reasoning_parts: list[str] = []
    usage = None
    finish_reason = None
    for event in chat_stream_events(credential, body, timeout_s):
        if not isinstance(event, dict):
            continue
        if isinstance(event.get("usage"), dict):
            usage = event["usage"]
        choices = event.get("choices") if isinstance(event.get("choices"), list) else []
        if not choices:
            continue
        choice = choices[0] if isinstance(choices[0], dict) else {}
        if choice.get("finish_reason"):
            finish_reason = choice["finish_reason"]
        delta = choice.get("delta") if isinstance(choice.get("delta"), dict) else choice.get("message")
        if not isinstance(delta, dict):
            continue
        piece = delta.get("content")
        if isinstance(piece, str):
            content_parts.append(piece)
        for key in ("reasoning_content", "reasoning"):
            piece = delta.get(key)
            if isinstance(piece, str):
                reasoning_parts.append(piece)
    result: dict[str, Any] = {"content": "".join(content_parts)}
    reasoning = "".join(reasoning_parts)
    if reasoning:
        result["reasoning"] = reasoning
    if finish_reason:
        result["finishReason"] = finish_reason
    if usage:
        result["usage"] = usage
    return result


def refresh_fn_for(variant: Variant) -> Callable[[dict[str, Any]], dict[str, Any]]:
    """Wire the auth store's refresh step to the upstream refresh endpoint."""

    def _refresh(credential: dict[str, Any]) -> dict[str, Any]:
        return refresh_token(credential)

    return _refresh


def resolve_credential(variant: Variant) -> dict[str, Any]:
    """Convenience wrapper the tools call; refreshes on demand."""
    return auth_module.resolve_credential(variant, refresh_fn_for(variant))
