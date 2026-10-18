"""WorkBuddy product variants: the two desktop apps this plugin serves.

Both products share one client framework and write their sign-in into the
*same* ``CodeBuddyExtension`` auth directory, differing only by file basename,
base URL and display identity. Everything that varies is collected here so no
other module carries its own ``if international`` branch.

Ported from corrinehu/dsh-workbuddy-connect ``src/variants.ts`` (MIT).
"""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

AUTH_DIR_RELATIVE = ("CodeBuddyExtension", "Data", "Public", "auth")

REGISTRY_ROOTS = (
    r"Software\Microsoft\Windows\CurrentVersion\Uninstall",
    r"Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall",
)


@dataclass(frozen=True)
class ElectronProduct:
    """How one product's Electron binary is identified and located."""

    product_name: str
    env_var: str
    exe_basename: str
    display_name_pattern: "re.Pattern[str]"
    # Windows default install layout; only set where it has been measured.
    default_path_segments: tuple[str, ...] | None


@dataclass(frozen=True)
class Variant:
    """One WorkBuddy product variant (CN WorkBuddy or international WorkBuddy AI)."""

    id: str
    display_name: str
    app_name: str
    region: str  # 'cn' | 'global'
    auth_env: str
    desktop_filename: str
    own_filename: str
    catalog_filename: str
    version_filename: str
    electron: ElectronProduct


def _pattern(source: str) -> "re.Pattern[str]":
    return re.compile(source, re.UNICODE)


CN = Variant(
    id="workbuddy",
    display_name="WorkBuddy",
    app_name="WorkBuddy",
    region="cn",
    auth_env="WORKBUDDY_AUTH_FILE",
    desktop_filename="workbuddy-desktop.info",
    own_filename=".workbuddy-auth.json",
    catalog_filename=".workbuddy-catalog.json",
    version_filename=".workbuddy-app-version.json",
    electron=ElectronProduct(
        product_name="WorkBuddy",
        env_var="WORKBUDDY_ELECTRON_BIN",
        exe_basename="workbuddy.exe",
        display_name_pattern=_pattern(r"^WorkBuddy(?:\s+\d+(?:\.\d+)+(?:[-+][0-9A-Za-z.-]+)?)?$"),
        default_path_segments=("Programs", "WorkBuddy", "WorkBuddy.exe"),
    ),
)

GLOBAL = Variant(
    id="workbuddy-ai",
    display_name="WorkBuddy AI",
    app_name="WorkBuddy AI",
    region="global",
    auth_env="WORKBUDDY_AI_AUTH_FILE",
    desktop_filename="workbuddy-desktop-ai.info",
    own_filename=".workbuddy-ai-auth.json",
    catalog_filename=".workbuddy-ai-catalog.json",
    version_filename=".workbuddy-ai-version.json",
    electron=ElectronProduct(
        product_name="WorkBuddy AI",
        env_var="WORKBUDDY_AI_ELECTRON_BIN",
        exe_basename="workbuddyai.exe",
        display_name_pattern=_pattern(r"^WorkBuddy AI(?:\s+\d+(?:\.\d+)+(?:[-+][0-9A-Za-z.-]+)?)?$"),
        default_path_segments=None,
    ),
)

VARIANTS: tuple[Variant, ...] = (CN, GLOBAL)


def variant_for(variant_id: str) -> Variant | None:
    for variant in VARIANTS:
        if variant.id == variant_id:
            return variant
    return None


def region_of(domain: str) -> str:
    """Region for a login domain; an empty domain means CN (matching upstream tooling)."""
    lowered = (domain or "").strip().lower()
    if lowered == "workbuddy.ai" or lowered.endswith(".workbuddy.ai"):
        return "global"
    return "cn"


def _windows_path_for_wsl(value: str | None) -> Path | None:
    path = (value or "").strip()
    if not path:
        return None
    if path.startswith("/"):
        return Path(path)
    parts = re.split(r"[\\/]+", path)
    if len(parts) < 2 or not re.fullmatch(r"[A-Za-z]", parts[0]):
        return None
    return Path("/mnt", parts[0].lower(), *parts[1:])


def auth_dir_candidates() -> list[Path]:
    """Platform-default candidates for the shared auth directory, in probe order.

    Windows probes both AppData roots: current builds write under
    ``%LOCALAPPDATA%``, older ones under ``%APPDATA%``. macOS uses the
    Application Support layout. Linux probes XDG config then data home; under
    WSL the mounted Windows profile is probed first.
    """
    home = Path.home()
    if sys.platform == "win32" or os.name == "nt":
        local = os.environ.get("LOCALAPPDATA") or str(home / "AppData" / "Local")
        roaming = os.environ.get("APPDATA") or str(home / "AppData" / "Roaming")
        return [Path(local, *AUTH_DIR_RELATIVE), Path(roaming, *AUTH_DIR_RELATIVE)]
    if sys.platform == "darwin":
        return [Path(home, "Library", "Application Support", *AUTH_DIR_RELATIVE)]
    config_home = os.environ.get("XDG_CONFIG_HOME")
    data_home = os.environ.get("XDG_DATA_HOME")
    dirs = [
        Path(config_home, *AUTH_DIR_RELATIVE)
        if config_home and config_home.startswith("/")
        else Path(home, ".config", *AUTH_DIR_RELATIVE),
        Path(data_home, *AUTH_DIR_RELATIVE)
        if data_home and data_home.startswith("/")
        else Path(home, ".local", "share", *AUTH_DIR_RELATIVE),
    ]
    if sys.platform.startswith("linux") and Path("/proc/version").exists():
        try:
            proc_version = Path("/proc/version").read_text(encoding="utf-8", errors="replace").lower()
        except OSError:
            proc_version = ""
        if "microsoft" in proc_version:
            profile = _windows_path_for_wsl(os.environ.get("USERPROFILE")) or Path(
                "/mnt/c/Users", home.name
            )
            local = _windows_path_for_wsl(os.environ.get("LOCALAPPDATA")) or profile / "AppData" / "Local"
            roaming = _windows_path_for_wsl(os.environ.get("APPDATA")) or profile / "AppData" / "Roaming"
            dirs = [
                local.joinpath(*AUTH_DIR_RELATIVE),
                roaming.joinpath(*AUTH_DIR_RELATIVE),
                *dirs,
            ]
    seen: set[str] = set()
    unique: list[Path] = []
    for directory in dirs:
        key = str(directory)
        if key not in seen:
            seen.add(key)
            unique.append(directory)
    return unique


def desktop_auth_candidates(variant: Variant) -> list[Path]:
    """Both apps share one auth directory and differ only by file basename."""
    seen: set[str] = set()
    candidates: list[Path] = []
    for directory in auth_dir_candidates():
        path = directory / variant.desktop_filename
        key = str(path).lower() if os.name == "nt" else str(path)
        if key not in seen:
            seen.add(key)
            candidates.append(path)
    return candidates


def default_electron_path(variant: Variant) -> Path | None:
    """The platform-default Electron binary for one product, when measured."""
    segments = variant.electron.default_path_segments
    if segments is None:
        return None
    if sys.platform == "win32" or os.name == "nt":
        local = os.environ.get("LOCALAPPDATA")
        if not local:
            return None
        return Path(local, *segments)
    if sys.platform == "darwin":
        return Path("/Applications", f"{variant.app_name}.app", "Contents", "MacOS", "Electron")
    return None


def state_home() -> Path:
    """Plugin-owned state directory for refreshed credentials and catalog caches."""
    override = (os.environ.get("WORKBUDDY_CONNECT_HOME") or "").strip()
    if override:
        return Path(override)
    return Path.home() / ".zcode" / "workbuddy-connect"


def own_auth_path(variant: Variant) -> Path:
    return state_home() / variant.own_filename


def catalog_cache_path(variant: Variant) -> Path:
    return state_home() / variant.catalog_filename


def version_cache_path(variant: Variant) -> Path:
    return state_home() / variant.version_filename
