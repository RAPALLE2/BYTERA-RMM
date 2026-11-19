# i18n_loader.py - collect language files from all "lang" folders
# ------------------------------------------------------------------
# Searches:
#   backend/data/lang/<locale>.json              (core translations)
#   modules/backend/<module>/lang/<locale>.json  (per module)
#   modules/frontend/<module>/lang/<locale>.json (per module)
# A locale is determined by the file name: "de_DE", "en_US", "zb_ZB", ...
# Available languages = union of all file names found. The dropdown shows them.
#
# Merging per locale: core first, then modules (modules may override/extend core
# keys). "_label" (display name) is NOT treated as a translation key but kept
# separately.
#
# Display name of a locale: "_label" from a file (core first, then modules) ->
# otherwise the table (de_DE->Deutsch ...) -> otherwise the file name itself.
from __future__ import annotations

import json
import re
import threading

from app.paths import BACKEND_DIR, MODULES_DIR

CORE_LANG_DIR = BACKEND_DIR / "data" / "lang"
_LOCALE_RE = re.compile(r"^[a-zA-Z]{2,3}([_-][a-zA-Z0-9]{2,8})?$")

# Known display names (fallback when a file does not provide a "_label")
KNOWN_LABELS = {
    "de_DE": "Deutsch", "en_US": "English", "en_GB": "English (UK)",
    "fr_FR": "Français", "es_ES": "Español", "it_IT": "Italiano",
    "nl_NL": "Nederlands", "pl_PL": "Polski", "pt_PT": "Português",
    "pt_BR": "Português (BR)", "ru_RU": "Русский", "tr_TR": "Türkçe",
    "cs_CZ": "Čeština", "uk_UA": "Українська", "zh_CN": "中文",
    "ja_JP": "日本語", "ko_KR": "한국어",
}

_cache: dict = {"locales": None, "data": {}}      # data: locale -> merged dict (without _label)
_labels: dict = {}
_lock = threading.RLock()


def _lang_dirs() -> list:
    """All lang folders: core first, then modules (backend + frontend)."""
    dirs = [CORE_LANG_DIR]
    for area in ("backend", "frontend", "node"):
        base = MODULES_DIR / area
        if not base.is_dir():
            continue
        for mod in sorted(base.iterdir()):
            ld = mod / "lang"
            if ld.is_dir():
                dirs.append(ld)
    return dirs


def _scan() -> None:
    """Read all language files and merge them per locale."""
    locales: dict[str, dict] = {}
    labels: dict[str, str] = {}
    for d in _lang_dirs():
        for f in sorted(d.glob("*.json")):
            loc = f.stem
            if not _LOCALE_RE.match(loc):
                continue
            try:
                raw = json.loads(f.read_text(encoding="utf-8"))
            except (OSError, ValueError) as e:
                print(f"[i18n] skipped (invalid): {f} - {e}")
                continue
            if not isinstance(raw, dict):
                continue
            label = raw.pop("_label", None)
            bucket = locales.setdefault(loc, {})
            bucket.update({k: v for k, v in raw.items() if isinstance(v, str)})
            if label and loc not in labels:        # first _label found wins (core first)
                labels[loc] = str(label)
    for loc in locales:
        if loc not in labels:
            labels[loc] = KNOWN_LABELS.get(loc, loc)
    with _lock:
        _cache["locales"] = sorted(locales.keys())
        _cache["data"] = locales
        _labels.clear()
        _labels.update(labels)


def _ensure() -> None:
    if _cache["locales"] is None:
        _scan()


def reload() -> None:
    _scan()


def available() -> list[dict]:
    """[{code, label}] of all languages found (for the dropdown)."""
    _ensure()
    return [{"code": c, "label": _labels.get(c, c)} for c in _cache["locales"]]


def strings(locale: str) -> dict:
    """All translations of a locale (merged, without _label)."""
    _ensure()
    return dict(_cache["data"].get(locale, {}))


def has(locale: str) -> bool:
    _ensure()
    return locale in (_cache["locales"] or [])
