"""PTA compiler enums → ICPC Contest API standard language ids.

The Contest API 2026-01 "known languages" appendix defines standardized
ids (``c``, ``cpp``, ``python3`` ...) with source-file extensions; PTA
exposes compiler enums (``GXX``, ``GCC`` ...) whose display names come
from ``COMPILER_LANGUAGES``. This module is the single mapping point:

- ``std_language_id(raw)`` accepts either a raw PTA enum or a display
  name (submissions rows carry the latter) and returns the standard id —
  unknown values pass through ``sanitize_id`` unchanged.
- ``extensions_for(std_id)`` / ``primary_extension(std_id)`` implement the
  spec appendix's extension table (unknown id → ``[]`` / ``".txt"``).

Merged enums (GCC / MODERN_GCC / CLANG all → ``c``) share one id; the
display name of the first enum seen wins (translate uses setdefault).
"""

from __future__ import annotations

from ..models import sanitize_id
from .client import COMPILER_LANGUAGES

# PTA compiler enum → Contest API standard language id (merged variants).
# Enums not listed here keep sanitize_id(enum) as their id.
STD_ID_FOR_ENUM: dict[str, str] = {
    "GCC": "c", "MODERN_GCC": "c", "CLANG": "c",
    "GXX": "cpp", "CLANGXX": "cpp",
    "JAVAC": "java",
    "PYTHON2": "python2", "PYTHON3": "python3",
    "RUBY": "ruby", "GO": "go",
    "GHC": "haskell", "FPC": "pascal",
}

# Display name → standard id for the mapped enums (first-seen name wins
# when several enums share one id — the dict literal keeps GCC's name).
_STD_ID_FOR_NAME: dict[str, str] = {}
for _enum, _sid in STD_ID_FOR_ENUM.items():
    _STD_ID_FOR_NAME.setdefault(COMPILER_LANGUAGES[_enum], _sid)

# Contest API 2026-01 known-languages appendix, verbatim (order kept;
# cpp's first extension is "cc" per the spec table). Unknown id → [].
STD_EXTENSIONS: dict[str, list[str]] = {
    "ada": ["adb", "ads"],
    "c": ["c"],
    "cpp": ["cc", "cpp", "cxx", "c++", "C"],
    "csharp": ["cs"],
    "go": ["go"],
    "haskell": ["hs"],
    "java": ["java"],
    "javascript": ["js"],
    "kotlin": ["kt"],
    "objectivec": ["m"],
    "pascal": ["pas"],
    "php": ["php"],
    "prolog": ["pl"],
    "python2": ["py"],
    "python3": ["py"],
    "ruby": ["rb"],
    "rust": ["rs"],
    "scala": ["scala"],
}


def std_language_id(raw) -> str:
    """PTA compiler enum or display name → Contest API language id.

    Accepts both forms because PTA hands us enums (contest meta
    compilers) and display names (COMPILER_LANGUAGES-mapped submission
    rows). Unknown values keep their sanitized original id.
    """
    s = str(raw or "unknown")
    if s in STD_ID_FOR_ENUM:
        return STD_ID_FOR_ENUM[s]
    if s in _STD_ID_FOR_NAME:
        return _STD_ID_FOR_NAME[s]
    return sanitize_id(s)


def extensions_for(std_id) -> list[str]:
    """Known-languages extensions for a standard id (unknown → [])."""
    return list(STD_EXTENSIONS.get(str(std_id), []))


def primary_extension(std_id) -> str:
    """First extension of the standard id, ".txt" fallback (with the
    leading dot for both forms so callers can do ``f"{label}{ext}"``)."""
    exts = extensions_for(std_id)
    return f".{exts[0]}" if exts else ".txt"
