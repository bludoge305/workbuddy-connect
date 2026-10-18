"""Diagnostics assembly for the ``workbuddy_doctor`` tool.

Read-mostly by default: it classifies files, resolves the Electron binary and
reports cache state without spawning helpers or touching the network. With
``live=True`` it additionally attempts a real catalog fetch and credit read
per signed-in variant.

Ported from corrinehu/dsh-workbuddy-connect's ``doctor`` CLI command.
"""

from __future__ import annotations

import importlib.util
import os
import sys
import time
from typing import Any

from . import auth as auth_module
from . import upstream as upstream_module
from .catalog import load_cached_models
from .variants import Variant, VARIANTS, catalog_cache_path, desktop_auth_candidates, own_auth_path, state_home


def _cryptography_available() -> bool:
    return importlib.util.find_spec("cryptography") is not None


def diagnose_variant(variant: Variant, live: bool = False) -> dict[str, Any]:
    report: dict[str, Any] = {
        "variant": variant.id,
        "displayName": variant.display_name,
        "region": variant.region,
    }

    env_value = (os.environ.get(variant.auth_env) or "").strip()
    report["authEnv"] = {"name": variant.auth_env, "set": bool(env_value)}

    candidates = desktop_auth_candidates(variant)
    report["desktopAuthCandidates"] = [
        {"path": str(path), "exists": path.is_file()} for path in candidates
    ]

    env_value = (os.environ.get(variant.electron.env_var) or "").strip()
    provider = auth_module.key_provider_for_diagnostics(variant)
    helper = provider.helper_path()
    default = auth_module.default_electron_path_for(variant.electron)
    report["electron"] = {
        "envVar": variant.electron.env_var,
        "envSet": bool(env_value),
        "defaultPath": str(default) if default else None,
        "defaultPresent": bool(default and default.is_file()),
        "resolvedHelperPath": helper,
        "note": "only needed to decrypt WorkBuddy 5.6+ encrypted sign-in files",
    }

    try:
        status = auth_module.status(variant)
        report["auth"] = {key: value for key, value in status.items() if key != "diagnostics"}
        report["authFile"] = status.get("diagnostics", {})
    except Exception as error:  # pragma: no cover - status() is designed not to throw
        report["auth"] = {"state": "unknown", "error": str(error)}

    own_path = own_auth_path(variant)
    report["ownCopy"] = {"path": str(own_path), "present": own_path.is_file()}

    cache_path = catalog_cache_path(variant)
    cache_info: dict[str, Any] = {"path": str(cache_path), "present": cache_path.is_file()}
    cached = load_cached_models(variant)
    if cached:
        cache_info["fetchedAtMs"] = cached.get("fetchedAtMs")
        cache_info["ageMinutes"] = max(
            0, int((time.time() * 1000 - cached.get("fetchedAtMs", 0)) / 60_000)
        )
        cache_info["modelCount"] = len(cached.get("models", []))
        cache_info["source"] = cached.get("source")
    report["catalogCache"] = cache_info

    if live:
        report["live"] = _live_checks(variant)

    return report


def _live_checks(variant: Variant) -> dict[str, Any]:
    checks: dict[str, Any] = {}
    try:
        credential = upstream_module.resolve_credential(variant)
        checks["credential"] = "ok"
    except Exception as error:
        reason = getattr(error, "reason_code", None)
        checks["credential"] = f"failed ({reason or 'error'})" if reason else f"failed ({error})"
        return checks
    try:
        models, source = upstream_module.fetch_live_models(variant, credential)
        checks["catalog"] = f"ok ({len(models)} models via {source})"
    except Exception as error:
        checks["catalog"] = f"failed ({error})"
    try:
        credits = upstream_module.fetch_credits(credential)
        if credits.get("unlimited"):
            checks["credits"] = "ok (unlimited cycle quota)"
        else:
            checks["credits"] = f"ok ({credits.get('total', 0)} remaining)"
    except Exception as error:
        checks["credits"] = f"failed ({error})"
    return checks


def run_doctor(variant_id: str | None = None, live: bool = False) -> dict[str, Any]:
    variants = VARIANTS
    if variant_id:
        selected = next((v for v in VARIANTS if v.id == variant_id), None)
        if selected is None:
            raise ValueError(f"unknown variant {variant_id!r}; expected 'workbuddy' or 'workbuddy-ai'")
        variants = (selected,)
    report: dict[str, Any] = {
        "stateHome": str(state_home()),
        "cryptographyModule": "available" if _cryptography_available() else "missing",
        "platform": sys.platform,
        "variants": [diagnose_variant(variant, live) for variant in variants],
    }
    if not _cryptography_available():
        report["notice"] = (
            "the 'cryptography' package is missing, so WorkBuddy 5.6+ encrypted sign-in files cannot be "
            "decrypted (plaintext credentials still work); install it with: pip install cryptography"
        )
    return report
