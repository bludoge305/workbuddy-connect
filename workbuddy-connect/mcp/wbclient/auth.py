"""WorkBuddy credential resolution.

The primary source is the desktop app's own auth file, read-only; a
plugin-owned copy under the state home holds token refreshes so the desktop
file is never written. The effective credential is whichever of the two
expires later for the *same identity* — the desktop file is the authority on
who is signed in now.

Ported from corrinehu/dsh-workbuddy-connect ``src/auth.ts`` +
``desktop-credential-protection.ts`` (MIT).

Security notes: tokens are never logged; error messages carry paths, ids and
sizes only. The plugin-owned copy is written with best-effort 0600 perms.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable

from .variants import (
    ElectronProduct,
    Variant,
    desktop_auth_candidates,
    own_auth_path,
    region_of,
)

REFRESH_MARGIN_MS = 5 * 60 * 1000

HELPER_SCRIPT = 'process.stdout.write(String(process._linkedBinding("electron_browser_workbuddy_storage").loggerGet()))'
HELPER_TIMEOUT_S = 10
VERSION_PATTERN = re.compile(r"^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$")

# Windows uninstall-registry relative paths probed for the product's exe.
UNINSTALL_PATHS = (
    r"Software\Microsoft\Windows\CurrentVersion\Uninstall",
    r"Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall",
)


class WorkBuddyCredentialError(Exception):
    """A diagnosable credential failure; ``reason_code`` classifies the cause."""

    def __init__(self, reason_code: str, message: str):
        super().__init__(message)
        self.reason_code = reason_code


# ---------------------------------------------------------------------------
# Desktop document classification and parsing


def classify_desktop_document(text: str) -> dict[str, Any]:
    """Classify a desktop auth document as absent/plaintext/encrypted/unrecognized."""
    if not text.strip():
        return {"format": "absent"}
    try:
        parsed = json.loads(text)
    except ValueError:
        return {"format": "unrecognized"}
    if not isinstance(parsed, dict):
        return {"format": "unrecognized"}
    auth = parsed.get("auth") if isinstance(parsed.get("auth"), dict) else parsed
    fields: list[dict[str, Any]] = []
    for field_name in ("accessToken", "refreshToken"):
        value = auth.get(field_name)
        if value is None or isinstance(value, str):
            continue
        wrapped = _parse_wrapped_field(field_name, value)
        if wrapped is None:
            return {"format": "unrecognized"}
        fields.append(wrapped)
    if not fields:
        return {"format": "plaintext"}
    return {"format": "encrypted", "document": parsed, "fields": fields}


def _parse_wrapped_field(field_name: str, value: Any) -> dict[str, Any] | None:
    """Whether a value is the 5.6 wrapper ``{$wbEncrypted:1, envelope:<b64>}``."""
    if not isinstance(value, dict) or value.get("$wbEncrypted") != 1 or not isinstance(value.get("envelope"), str):
        return None
    try:
        inner = json.loads(base64.b64decode(value["envelope"]).decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(inner, dict):
        return None
    nonce = _parse_b64(inner.get("nonce"), 12)
    auth_tag = _parse_b64(inner.get("authTag"), 16)
    ciphertext = _parse_b64(inner.get("ciphertext"))
    if nonce is None or auth_tag is None or ciphertext is None:
        return None
    suite = inner.get("suite")
    key_id = inner.get("keyId")
    if not isinstance(suite, int) or isinstance(suite, bool) or suite != 1:
        return None
    if not isinstance(key_id, str) or not re.fullmatch(r"[0-9a-f]{16}", key_id):
        return None
    return {
        "field": field_name,
        "suite": suite,
        "keyId": key_id,
        "nonce": nonce,
        "authTag": auth_tag,
        "ciphertext": ciphertext,
    }


def _parse_b64(value: Any, length: int | None = None) -> bytes | None:
    if not isinstance(value, str) or value == "":
        return None
    try:
        decoded = base64.b64decode(value)
    except ValueError:
        return None
    if len(decoded) == 0 or base64.b64encode(decoded).rstrip(b"=").decode() != value.rstrip("="):
        return None
    if length is not None and len(decoded) != length:
        return None
    return decoded


def parse_workbuddy_auth(text: str) -> dict[str, Any] | None:
    """Parse either on-disk shape into a normalized credential; None without a token."""
    try:
        parsed = json.loads(text)
    except ValueError:
        return None
    if not isinstance(parsed, dict):
        return None
    if isinstance(parsed.get("auth"), dict):
        auth = parsed["auth"]
        identity = parsed.get("account") if isinstance(parsed.get("account"), dict) else {}
    else:
        auth = parsed
        identity = parsed
    access_token = auth.get("accessToken")
    if not isinstance(access_token, str) or access_token == "":
        return None
    return _normalize_credential(
        access_token=access_token,
        refresh_token=auth.get("refreshToken") if isinstance(auth.get("refreshToken"), str) else "",
        expires_at=auth.get("expiresAt"),
        refresh_expires_at=auth.get("refreshExpiresAt"),
        domain=auth.get("domain") if isinstance(auth.get("domain"), str) else "",
        uid=identity.get("uid") if isinstance(identity.get("uid"), str) else "",
        enterprise_id=identity.get("enterpriseId") if isinstance(identity.get("enterpriseId"), str) else None,
        nickname=identity.get("nickname") if isinstance(identity.get("nickname"), str) else None,
        source="desktop",
    )


def _normalize_credential(
    *,
    access_token: str,
    refresh_token: str,
    expires_at: Any,
    refresh_expires_at: Any,
    domain: str,
    uid: str,
    enterprise_id: str | None,
    nickname: str | None,
    source: str,
) -> dict[str, Any]:
    credential: dict[str, Any] = {
        "accessToken": access_token,
        "refreshToken": refresh_token,
        "expiresAtMs": _expiry_to_ms(expires_at),
        "domain": domain,
        "uid": uid,
        "source": source,
    }
    refresh_ms = _expiry_to_ms(refresh_expires_at)
    if refresh_ms:
        credential["refreshExpiresAtMs"] = refresh_ms
    if enterprise_id:
        credential["enterpriseId"] = enterprise_id
    if nickname:
        credential["nickname"] = nickname
    return credential


def _expiry_to_ms(value: Any) -> int:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
        return 0
    return int(value) if value > 1e12 else int(value * 1000)


# ---------------------------------------------------------------------------
# 5.6 at-rest encryption: AAD, key derivation, Electron key helper


def build_authenticated_context_aad(key_id: str, suite: int) -> bytes:
    """The AAD the app's own ``buildAuthenticatedContextAad`` builds (suite 1 fields)."""

    def length_prefixed(value: str) -> bytes:
        raw = value.encode("utf-8")
        return len(raw).to_bytes(4, "big") + raw

    return b"".join([
        b"WB-AAD\x00",
        bytes([1]),
        length_prefixed("WBEV1"),
        length_prefixed("sym-v1"),
        suite.to_bytes(4, "big"),
        length_prefixed(key_id),
        bytes([2]),
        bytes([0]),
        bytes([0]),
    ])


def open_auth_field(key: bytes, envelope: dict[str, Any]) -> str | None:
    """Open one AES-256-GCM envelope; None when it will not open."""
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError:
        raise WorkBuddyCredentialError(
            "encrypted-credential-unreadable",
            "the Python package 'cryptography' is required to decrypt WorkBuddy 5.6+ encrypted "
            "credentials; install it into the plugin's Python environment (venv pip install cryptography)",
        ) from None
    try:
        plaintext = AESGCM(key).decrypt(
            envelope["nonce"],
            envelope["ciphertext"] + envelope["authTag"],
            build_authenticated_context_aad(envelope["keyId"], envelope["suite"]),
        )
        return plaintext.decode("utf-8")
    except Exception:
        return None


def parse_at_rest_payload(text: str) -> str | None:
    """Validate the helper payload: ``{version:1, atRestSecretKey:<canonical b64 32B>}``."""
    try:
        parsed = json.loads(text)
    except ValueError:
        return None
    if not isinstance(parsed, dict) or parsed.get("version") != 1:
        return None
    secret = parsed.get("atRestSecretKey")
    if not isinstance(secret, str) or secret == "":
        return None
    try:
        decoded = base64.b64decode(secret)
    except ValueError:
        return None
    if len(decoded) != 32 or base64.b64encode(decoded).decode() != secret:
        return None
    if decoded == b"\x00" * 32:
        return None
    return secret


def derive_protector_key(secret: str) -> bytes:
    return hashlib.sha256(secret.encode("utf-8")).digest()


def default_electron_path_for(product: ElectronProduct) -> Path | None:
    """The platform-default Electron binary for one product, when measured."""
    if os.name == "nt":
        segments = product.default_path_segments
        if segments is None:
            return None
        local = os.environ.get("LOCALAPPDATA")
        if not local:
            return None
        return Path(local, *segments)
    if sys.platform == "darwin":
        return Path("/Applications", f"{product.product_name}.app", "Contents", "MacOS", "Electron")
    return None


class AtRestKeyProvider:
    """Resolves the WorkBuddy at-rest protector key, spawning the app's own binary.

    An explicit env path wins and never falls back; then the measured default
    install location; then Windows uninstall-registry discovery with a strict
    layout check. The resolved key is cached in memory keyed by its id.
    """

    def __init__(self, product: ElectronProduct):
        self.product = product
        self._cache: tuple[str, bytes] | None = None
        self._discovered: str | None = None

    def helper_path(self) -> str | None:
        """The binary the resolver would use right now, for diagnostics only."""
        env = (os.environ.get(self.product.env_var) or "").strip()
        if env:
            return env
        if self._discovered and os.path.isfile(self._discovered):
            return self._discovered
        default = default_electron_path_for(self.product)
        if default is not None and default.is_file():
            return str(default)
        return None

    def protector_key_for(self, requested_ids: list[str]) -> bytes:
        if not requested_ids:
            raise WorkBuddyCredentialError(
                "encrypted-credential-unreadable", "encrypted desktop credential carries no key ids"
            )
        if self._cache is not None and self._cache[0] in requested_ids:
            return self._cache[1]
        key = self._resolve_key()
        key_id = hashlib.sha256(key).hexdigest()[:16]
        if key_id not in requested_ids:
            raise WorkBuddyCredentialError(
                "encrypted-credential-unreadable",
                f"WorkBuddy's current at-rest key (id {key_id}) does not match the credential's envelope "
                f"(id {' or '.join(requested_ids)}); the desktop credential was sealed by a different installation",
            )
        self._cache = (key_id, key)
        return key

    def _resolve_key(self) -> bytes:
        path = self._resolve_electron_path()
        payload = self._spawn_helper(path)
        secret = parse_at_rest_payload(payload)
        if secret is None:
            raise WorkBuddyCredentialError(
                "encrypted-credential-unreadable",
                f"the {self.product.product_name} key helper ({path}) returned an unusable at-rest payload",
            )
        return derive_protector_key(secret)

    def _resolve_electron_path(self) -> str:
        env = (os.environ.get(self.product.env_var) or "").strip()
        if env:
            if not os.path.isfile(env):
                raise WorkBuddyCredentialError(
                    "electron-path-invalid",
                    f"the configured {self.product.product_name} Electron binary is not available at {env}; "
                    f"check {self.product.env_var} or unset it to let the plugin look for the app itself",
                )
            return env
        default = default_electron_path_for(self.product)
        if default is not None and default.is_file():
            return str(default)
        if self._discovered and os.path.isfile(self._discovered):
            return self._discovered
        if os.name == "nt":
            found = self._discover_windows()
            if found:
                self._discovered = found
                return found
        raise WorkBuddyCredentialError(
            "electron-binary-unavailable",
            f"no {self.product.product_name} Electron binary was found; set {self.product.env_var} "
            "to the app's executable so its 5.6+ encrypted sign-in can be decrypted",
        )

    def _spawn_helper(self, electron_path: str) -> str:
        env = dict(os.environ)
        env["ELECTRON_RUN_AS_NODE"] = "1"
        kwargs: dict[str, Any] = {
            "capture_output": True,
            "timeout": HELPER_TIMEOUT_S,
            "env": env,
        }
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        try:
            completed = subprocess.run([electron_path, "-e", HELPER_SCRIPT], **kwargs)
        except subprocess.TimeoutExpired as error:
            raise WorkBuddyCredentialError(
                "encrypted-credential-unreadable",
                f"the {self.product.product_name} key helper ({electron_path}) timed out after {HELPER_TIMEOUT_S}s",
            ) from error
        except OSError as error:
            raise WorkBuddyCredentialError(
                "encrypted-credential-unreadable",
                f"the {self.product.product_name} key helper ({electron_path}) could not be started",
            ) from error
        if completed.returncode != 0:
            raise WorkBuddyCredentialError(
                "encrypted-credential-unreadable",
                f"the {self.product.product_name} key helper ({electron_path}) exited with code {completed.returncode}",
            )
        output = completed.stdout.decode("utf-8", errors="replace").strip()
        if not output:
            raise WorkBuddyCredentialError(
                "encrypted-credential-unreadable",
                f"the {self.product.product_name} key helper ({electron_path}) produced no payload",
            )
        return output

    def _discover_windows(self) -> str | None:
        """Locate the product exe through uninstall records + a layout check."""
        try:
            import winreg
        except ImportError:
            return None
        candidates: list[str] = []
        for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            for relative in UNINSTALL_PATHS:
                try:
                    key = winreg.OpenKey(hive, relative)
                except OSError:
                    continue
                with key:
                    index = 0
                    while True:
                        try:
                            subkey_name = winreg.EnumKey(key, index)
                        except OSError:
                            break
                        index += 1
                        try:
                            with winreg.OpenKey(key, subkey_name) as subkey:
                                display_name = self._query_value(subkey, "DisplayName")
                                display_icon = self._query_value(subkey, "DisplayIcon")
                        except OSError:
                            continue
                        if not isinstance(display_name, str):
                            continue
                        if not self.product.display_name_pattern.match(display_name.strip()):
                            continue
                        icon = _parse_display_icon(display_icon or "")
                        if icon:
                            candidates.append(icon)
        for candidate in dict.fromkeys(candidates):
            resolved = _inspect_windows_candidate(candidate, self.product.exe_basename)
            if resolved:
                return resolved
        return None

    @staticmethod
    def _query_value(key: Any, name: str) -> str | None:
        try:
            import winreg

            value, _type = winreg.QueryValueEx(key, name)
        except (OSError, ImportError):
            return None
        return value if isinstance(value, str) else None


def _parse_display_icon(value: str) -> str | None:
    raw = value.strip()
    if raw.startswith('"'):
        closing = raw.find('"', 1)
        if closing < 0:
            return None
        suffix = raw[closing + 1:].strip()
        if suffix and not re.fullmatch(r",\d+", suffix):
            return None
        path = raw[1:closing]
    else:
        match = re.match(r"^(.+?\.exe)(?:,\d+)?$", raw, re.IGNORECASE)
        if not match:
            return None
        path = match.group(1)
    path = path.strip()
    return path if re.search(r"\.exe$", path, re.IGNORECASE) else None


def _inspect_windows_candidate(electron_path: str, exe_basename: str) -> str | None:
    """Validate the known install layout; return the exe path or None."""
    if os.path.basename(electron_path).lower() != exe_basename.lower():
        return None
    if not os.path.isfile(electron_path):
        return None
    install_root = os.path.dirname(electron_path)
    try:
        with open(os.path.join(install_root, "version"), encoding="utf-8") as handle:
            version = handle.read().strip()
    except OSError:
        return None
    if not VERSION_PATTERN.match(version):
        return None
    try:
        if "app.asar" not in os.listdir(os.path.join(install_root, "resources")):
            return None
    except OSError:
        return None
    return os.path.normpath(electron_path)


# ---------------------------------------------------------------------------
# Credential store


def read_desktop_credential(variant: Variant) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Read the desktop auth file; returns (credential|None, diagnostics).

    Only an absent (or empty) file falls through to the next candidate; a
    present-but-unusable document raises rather than being papered over — the
    desktop file, while it exists, is the identity authority.
    """
    diagnostics: dict[str, Any] = {"candidates": [], "resolvedPath": None, "format": "absent"}
    for path in desktop_auth_candidates(variant):
        entry: dict[str, Any] = {"path": str(path), "exists": path.is_file()}
        diagnostics["candidates"].append(entry)
        if not entry["exists"]:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as error:
            raise WorkBuddyCredentialError(
                "credential-unreadable", f"the desktop auth file at {path} could not be read ({error})"
            ) from error
        classification = classify_desktop_document(text)
        entry["format"] = classification["format"]
        if classification["format"] == "absent":
            continue
        diagnostics["resolvedPath"] = str(path)
        diagnostics["format"] = classification["format"]
        if classification["format"] == "unrecognized":
            raise WorkBuddyCredentialError(
                "credential-unreadable",
                f"the desktop auth file at {path} exists but is unreadable (neither a plaintext credential "
                "nor a decodable WorkBuddy 5.6 envelope); fix or remove the file",
            )
        if classification["format"] == "plaintext":
            return parse_workbuddy_auth(text), diagnostics
        wrapped = classification["fields"]
        key_ids = list(dict.fromkeys(field["keyId"] for field in wrapped))
        provider = _key_provider(variant)
        key = provider.protector_key_for(key_ids)
        rebuilt = json.loads(text)
        auth = rebuilt.get("auth") if isinstance(rebuilt.get("auth"), dict) else rebuilt
        for field in wrapped:
            plaintext = open_auth_field(key, field)
            if plaintext is None:
                raise WorkBuddyCredentialError(
                    "encrypted-credential-unreadable",
                    f"the encrypted desktop credential's {field['field']} could not be decrypted "
                    f"(envelope key id {field['keyId']}); the WorkBuddy app may hold a different at-rest "
                    "key — open it once to reseal the sign-in",
                )
            auth[field["field"]] = plaintext
        return parse_workbuddy_auth(json.dumps(rebuilt)), diagnostics
    return None, diagnostics


_KEY_PROVIDERS: dict[str, AtRestKeyProvider] = {}


def _key_provider(variant: Variant) -> AtRestKeyProvider:
    provider = _KEY_PROVIDERS.get(variant.id)
    if provider is None:
        provider = AtRestKeyProvider(variant.electron)
        _KEY_PROVIDERS[variant.id] = provider
    return provider


def key_provider_for_diagnostics(variant: Variant) -> AtRestKeyProvider:
    return _key_provider(variant)


def _parse_own_document(text: str) -> dict[str, Any] | None:
    try:
        parsed = json.loads(text)
    except ValueError:
        return None
    if not isinstance(parsed, dict) or parsed.get("version") != 1 or not isinstance(parsed.get("credential"), dict):
        return None
    stored = parsed["credential"]
    access_token = stored.get("accessToken")
    if not isinstance(access_token, str) or access_token == "":
        return None
    credential: dict[str, Any] = {
        "accessToken": access_token,
        "refreshToken": stored.get("refreshToken") if isinstance(stored.get("refreshToken"), str) else "",
        "expiresAtMs": stored.get("expiresAtMs") if isinstance(stored.get("expiresAtMs"), (int, float)) else 0,
        "domain": stored.get("domain") if isinstance(stored.get("domain"), str) else "",
        "uid": stored.get("uid") if isinstance(stored.get("uid"), str) else "",
        "source": "plugin",
    }
    if isinstance(stored.get("refreshExpiresAtMs"), (int, float)):
        credential["refreshExpiresAtMs"] = stored["refreshExpiresAtMs"]
    if isinstance(stored.get("enterpriseId"), str) and stored["enterpriseId"]:
        credential["enterpriseId"] = stored["enterpriseId"]
    if isinstance(stored.get("nickname"), str) and stored["nickname"]:
        credential["nickname"] = stored["nickname"]
    return credential


def read_own_credential(variant: Variant) -> dict[str, Any] | None:
    try:
        return _parse_own_document(own_auth_path(variant).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _atomic_write_private(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=str(path.parent), prefix=path.name + ".", suffix=".tmp", delete=False
    )
    try:
        handle.write(text)
        handle.flush()
        handle.close()
        if os.name == "posix":
            os.chmod(handle.name, 0o600)
        os.replace(handle.name, path)
    except Exception:
        try:
            os.unlink(handle.name)
        except OSError:
            pass
        raise


def save_own_credential(variant: Variant, credential: dict[str, Any]) -> None:
    document = {"version": 1, "credential": {k: v for k, v in credential.items() if k != "source"}}
    _atomic_write_private(own_auth_path(variant), json.dumps(document, ensure_ascii=False, indent=2) + "\n")


def logout(variant: Variant) -> bool:
    """Remove the plugin-owned copy; the desktop file is untouched."""
    removed = False
    path = own_auth_path(variant)
    for target in (path, path.with_name(path.name + ".lock")):
        try:
            target.unlink()
            removed = True
        except OSError:
            continue
    return removed


def current_credential(variant: Variant) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """The freshest stored credential without refreshing; never raises.

    Returns (credential|None, diagnostics). A credential from the other
    product's region is refused with a diagnosable signed-out reason.
    """
    try:
        desktop, desktop_diag = read_desktop_credential(variant)
        diagnostics: dict[str, Any] = {"desktop": desktop_diag}
    except WorkBuddyCredentialError as error:
        return None, {
            "desktop": {"format": "unreadable"},
            "reason": str(error),
            "reasonCode": error.reason_code,
        }
    own = read_own_credential(variant)
    diagnostics["ownCopyPath"] = str(own_auth_path(variant))
    diagnostics["ownCopyPresent"] = own is not None
    for label, credential in (("desktop file", desktop), ("plugin copy", own)):
        if credential is None:
            continue
        if region_of(credential.get("domain", "")) != variant.region:
            other = "WorkBuddy AI" if variant.region == "cn" else "WorkBuddy (CN)"
            return None, {
                **diagnostics,
                "reason": (
                    f"{variant.display_name} received a {other} credential in its {label} "
                    f"(domain {json.dumps(credential.get('domain', ''))}); point {variant.auth_env} at the "
                    f"{variant.app_name} sign-in, or remove the mismatched file"
                ),
                "reasonCode": "credential-region-mismatch",
            }
    if desktop is None and own is None:
        return None, diagnostics
    if desktop is None:
        return own, diagnostics
    if own is None:
        return desktop, diagnostics
    if desktop.get("uid") != own.get("uid") or desktop.get("enterpriseId") != own.get("enterpriseId"):
        return desktop, diagnostics
    return (own if own.get("expiresAtMs", 0) > desktop.get("expiresAtMs", 0) else desktop), diagnostics


def status(variant: Variant) -> dict[str, Any]:
    """Read-only sign-in summary; never refreshes and never throws."""
    credential, diagnostics = current_credential(variant)
    if credential is None:
        result: dict[str, Any] = {
            "state": "signed-out",
            "reasonCode": diagnostics.get("reasonCode", "no-credential"),
            "diagnostics": diagnostics,
        }
        if diagnostics.get("reason"):
            result["reason"] = diagnostics["reason"]
        return result
    result = {
        "state": "signed-in",
        "expiresAtMs": credential.get("expiresAtMs", 0),
        "source": credential.get("source"),
        "diagnostics": diagnostics,
    }
    if credential.get("refreshExpiresAtMs"):
        result["refreshExpiresAtMs"] = credential["refreshExpiresAtMs"]
    if credential.get("nickname"):
        result["nickname"] = credential["nickname"]
    if credential.get("domain"):
        result["domain"] = credential["domain"]
    return result


def resolve_credential(variant: Variant, refresh_fn: Callable[[dict[str, Any]], dict[str, Any]]) -> dict[str, Any]:
    """The credential to send upstream, refreshed on demand when inside the margin.

    A failed refresh still returns a not-yet-expired token so an unreachable
    refresh endpoint does not take down a working session.
    """
    credential, _diagnostics = current_credential(variant)
    if credential is None:
        candidates = ", ".join(str(p) for p in desktop_auth_candidates(variant))
        raise WorkBuddyCredentialError(
            "no-credential",
            f"no signed-in {variant.app_name} account found; sign in once in the {variant.app_name} desktop "
            f"app (expected one of: {candidates}, or set {variant.auth_env})",
        )
    now = int(time.time() * 1000)
    expires_at = credential.get("expiresAtMs", 0)
    if expires_at > 0 and now + REFRESH_MARGIN_MS < expires_at:
        return credential
    if not credential.get("refreshToken", ""):
        if expires_at > now + 30_000:
            return credential
        raise WorkBuddyCredentialError(
            "session-dead",
            "access token expired and no refresh token is stored; sign in again in the "
            f"{variant.app_name} desktop app",
        )
    try:
        outcome = refresh_fn(credential)
    except Exception as error:
        if expires_at > now + 30_000:
            return credential
        raise WorkBuddyCredentialError(
            "session-dead",
            f"token refresh failed and the access token is expired ({error}); "
            f"open the {variant.app_name} desktop app once to sign in again",
        ) from error
    refreshed = dict(credential)
    refreshed["accessToken"] = outcome["accessToken"]
    if outcome.get("refreshToken"):
        refreshed["refreshToken"] = outcome["refreshToken"]
    if outcome.get("expiresInSec"):
        refreshed["expiresAtMs"] = now + int(outcome["expiresInSec"] * 1000)
    if outcome.get("domain"):
        refreshed["domain"] = outcome["domain"]
    refreshed["source"] = "plugin"
    try:
        save_own_credential(variant, refreshed)
    except OSError:
        pass
    return refreshed
