"""Offline self-test for the wbclient protocol port.

Run with the plugin's Python:

    python mcp/wbclient/selftest.py

Covers the pieces that can be verified without a signed-in desktop app or
network: the 5.6 envelope seal/open roundtrip (AES-256-GCM + AAD), document
classification and parsing, chat-body normalization, model catalog parsing,
and the credits accounting helpers. Exits non-zero on the first failure.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from wbclient import catalog as catalog_module  # noqa: E402
from wbclient import upstream as upstream_module  # noqa: E402
from wbclient.auth import (  # noqa: E402
    build_authenticated_context_aad,
    classify_desktop_document,
    open_auth_field,
    parse_workbuddy_auth,
)


def fail(message: str) -> None:
    raise AssertionError(message)


def test_envelope_roundtrip() -> None:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    key = hashlib.sha256(b"selftest-secret").digest()
    key_id = hashlib.sha256(key).hexdigest()[:16]
    plaintext = json.dumps({
        "auth": {"accessToken": "token-a", "refreshToken": "token-r", "expiresAt": 1760000000, "domain": "workbuddy.ai"},
        "account": {"uid": "u1", "nickname": "tester"},
    })

    # Seal with the same AAD layout the app uses, then open through our reader.
    nonce = b"\x01" * 12
    cipher = AESGCM(key)
    sealed = cipher.encrypt(nonce, plaintext.encode("utf-8"), build_authenticated_context_aad(key_id, 1))
    envelope = {
        "field": "accessToken",
        "suite": 1,
        "keyId": key_id,
        "nonce": nonce,
        "authTag": sealed[-16:],
        "ciphertext": sealed[:-16],
    }
    opened = open_auth_field(key, envelope)
    if opened != plaintext:
        fail(f"envelope roundtrip mismatch: {opened!r}")

    # Tampered ciphertext must not open.
    envelope_bad = dict(envelope, ciphertext=b"\x00" + envelope["ciphertext"][1:])
    if open_auth_field(key, envelope_bad) is not None:
        fail("tampered envelope opened")


def test_classification_and_parsing() -> None:
    plaintext_doc = json.dumps({
        "auth": {"accessToken": "at", "refreshToken": "rt", "expiresAt": 1760000000, "domain": "copilot.tencent.com"},
        "account": {"uid": "42", "enterpriseId": "ent1", "nickname": "nick"},
    })
    classification = classify_desktop_document(plaintext_doc)
    if classification["format"] != "plaintext":
        fail(f"expected plaintext, got {classification['format']}")
    credential = parse_workbuddy_auth(plaintext_doc)
    if credential is None:
        fail("plaintext document did not parse")
    if credential["uid"] != "42" or credential["enterpriseId"] != "ent1":
        fail(f"identity lost: {credential}")
    if credential["expiresAtMs"] != 1_760_000_000_000:
        fail(f"seconds expiry not converted: {credential['expiresAtMs']}")
    if upstream_module.region_of("workbuddy.ai") != "global" or upstream_module.region_of("") != "cn":
        fail("region_of broken")

    wrapped_doc = {
        "auth": {
            "accessToken": {"$wbEncrypted": 1, "envelope": base64.b64encode(json.dumps({
                "suite": 1, "keyId": "0123456789abcdef", "nonce": "", "authTag": "", "ciphertext": "",
            }).encode()).decode()},
            "refreshToken": "rt",
        },
    }
    if classify_desktop_document(json.dumps(wrapped_doc))["format"] != "unrecognized":
        fail("malformed envelope should classify as unrecognized")
    if classify_desktop_document("")["format"] != "absent":
        fail("empty document should be absent")
    if classify_desktop_document("not json")["format"] != "unrecognized":
        fail("garbage should be unrecognized")


def test_prepare_chat_body() -> None:
    body = {
        "model": "glm-5.3",
        "messages": [
            {"role": "developer", "content": "sys"},
            {"role": "user", "content": "hi"},
        ],
        "tool_choice": {"type": "function", "function": {"name": "grep"}},
        "reasoning_effort": "off",
    }
    prepared = upstream_module.prepare_chat_body(body, international=False)
    if prepared["messages"][0]["role"] != "system":
        fail("developer role not rewritten to system")
    if prepared["tool_choice"] != "grep":
        fail(f"tool_choice not flattened: {prepared.get('tool_choice')}")
    if "reasoning_effort" in prepared:
        fail("off effort not dropped")
    if prepared["stream"] is not True:
        fail("stream not forced")

    global_prepared = upstream_module.prepare_chat_body(
        {"model": "m", "messages": [{"role": "user", "content": "hi"}]}, international=True
    )
    if global_prepared["messages"][0]["role"] != "system":
        fail("international gateway requires a leading system message")


def test_catalog_parsing() -> None:
    document = {
        "models": [
            {"id": "glm-5.3", "name": "GLM-5.3", "maxInputTokens": 200_000, "maxOutputTokens": 64_000,
             "supportsImages": True, "supportsReasoning": True, "onlyReasoning": True,
             "reasoning": {"supportedEfforts": ["low", "high", "max"], "defaultEffort": "max"},
             "credits": "x0.79 credits", "tags": ["badge:限时免费:#FF0000"]},
            {"id": "broken", "name": "Broken", "maxInputTokens": 0, "maxOutputTokens": 10},
            {"id": "disabled-m", "name": "D", "maxInputTokens": 10, "maxOutputTokens": 10, "disabled": True},
        ],
        "agents": [{"name": "cli", "models": ["glm-5.3", "broken", "disabled-m", "missing"]}],
    }
    models = catalog_module.parse_model_catalog(document, international=False, promo_badges={"glm-5.3": ["badge:限时免费:#FF0000"]})
    if len(models) != 1 or models[0]["id"] != "glm-5.3":
        fail(f"roster intersection wrong: {[m['id'] for m in models]}")
    model = models[0]
    if model["billing"]["credits"] != "x0.79 credits" or model["billing"]["free"]:
        fail(f"billing parse wrong: {model['billing']}")
    if model["billing"]["badges"] != ["限时免费"]:
        fail(f"badge not extracted: {model['billing'].get('badges')}")
    if not catalog_module.normalize_credits("x0.00 credits") == "x0.00":
        fail("normalize_credits broken")

    bare_document = {"models": document["models"], "agents": document["agents"]}
    if len(catalog_module.parse_model_catalog(bare_document)) != 1:
        fail("bare document shape not accepted")

    promotion_model = {
        "id": "m", "name": "M", "contextWindow": 1, "maxTokens": 1, "supportsImages": False,
        "promotions": [{"start": 0, "end": 9_999_999_999_999, "factor": 0, "label": "Free now", "priority": 1}],
        "billing": {"credits": "x2.00", "free": False},
    }
    promoted = catalog_module.model_with_current_promotion(promotion_model, now_ms=1)
    if not promoted["billing"]["free"] or promoted["billing"]["credits"] != "x0.00":
        fail(f"promotion not applied: {promoted['billing']}")
    expired = catalog_module.model_with_current_promotion(promotion_model, now_ms=9_999_999_999_999)
    if expired["billing"].get("rateUnknown") is not True:
        fail("expired promotion must report rateUnknown, never keep claiming a discount")


def test_credits_accounting() -> None:
    from wbclient.upstream import _expiring_within, _parse_cycle_end_time

    now = 1_000_000_000_000
    accounts = [
        {"remain": 10, "endTimeMs": now + 2 * 3_600_000},
        {"remain": 5, "endTimeMs": now + 5 * 24 * 3_600_000},
        {"remain": 100, "endTimeMs": now - 3_600_000},
        {"remain": 7, "endTimeMs": None},
    ]
    day = 24 * 3_600_000
    if _expiring_within(accounts, now, day)["total"] != 10:
        fail("24h bucket wrong")
    if _expiring_within(accounts, now, 7 * day)["total"] != 15:
        fail("7d bucket wrong (lapsed and unknown-end packages must not count)")
    if _parse_cycle_end_time("2026-10-18 08:00:00") is None:
        fail("valid CycleEndTime rejected")
    if _parse_cycle_end_time("2026-02-30 08:00:00") is not None:
        fail("impossible calendar date must read as unknown")


def main() -> int:
    tests = [
        ("envelope_roundtrip", test_envelope_roundtrip),
        ("classification_and_parsing", test_classification_and_parsing),
        ("prepare_chat_body", test_prepare_chat_body),
        ("catalog_parsing", test_catalog_parsing),
        ("credits_accounting", test_credits_accounting),
    ]
    failures = 0
    for name, test in tests:
        try:
            test()
            print(f"ok   {name}")
        except ImportError as error:
            failures += 1
            print(f"skip {name}: {error}")
        except AssertionError as error:
            failures += 1
            print(f"FAIL {name}: {error}")
    if failures:
        print(f"{failures} test(s) failed")
        return 1
    print("all self-tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
