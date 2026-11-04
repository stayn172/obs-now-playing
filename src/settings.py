"""Settings: schema, defaults, validation/clamping, persistence, migration.

Unknown keys are ignored, missing keys get defaults, and invalid values are
clamped (numbers) or rejected (keeping the previous value). Writes are atomic.
"""
from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import threading
from dataclasses import dataclass
from typing import Any, Callable, Optional

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1

FONTS = ["Inter", "Montserrat", "Poppins", "Roboto", "System"]
_HEX = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")


@dataclass(frozen=True)
class Field:
    kind: str  # bool | int | float | choice | color | str
    default: Any
    group: str
    min: Optional[float] = None
    max: Optional[float] = None
    choices: tuple = ()
    max_len: int = 200


SCHEMA: dict[str, Field] = {
    # Layout
    "card_radius": Field("int", 24, "layout", 0, 100),
    "art_radius": Field("int", 14, "layout", 0, 100),
    "scale": Field("float", 1.0, "layout", 0.25, 1.0),  # card size inside the canvas
    "card_align": Field("choice", "left", "layout", choices=("left", "center", "right")),
    "art_position": Field("choice", "left", "layout", choices=("left", "right")),
    "show_art": Field("bool", True, "layout"),
    # Style
    "bg_opacity": Field("float", 0.7, "style", 0.0, 1.0),
    "bg_blur": Field("bool", True, "style"),
    "bg_blur_amount": Field("int", 40, "style", 0, 100),
    "accent_mode": Field("choice", "custom", "style", choices=("auto", "custom")),
    "accent_color": Field("color", "#ffffff", "style"),
    "text_color": Field("color", "#ffffff", "style"),
    "font": Field("choice", "Inter", "style", choices=tuple(FONTS)),
    "title_size": Field("int", 30, "style", 12, 72),
    # Content
    "show_album": Field("bool", False, "content"),
    "show_progress": Field("bool", True, "content"),
    "show_times": Field("bool", True, "content"),
    "show_player_label": Field("bool", False, "content"),
    # Behavior
    "hide_when_paused": Field("bool", False, "behavior"),
    "hide_when_idle": Field("bool", True, "behavior"),
    "fade_ms": Field("int", 400, "behavior", 0, 5000),
    "hide_during_ads": Field("bool", True, "behavior"),
    # Player (explicit choice, no auto mode; see docs/SPEC.md)
    "selected_player": Field("str", "spotify", "player", max_len=200),
    # System
    "start_with_os": Field("bool", False, "system"),
    "port": Field("int", 8765, "system", 1024, 65535),
}

# Groups kept by "Reset to defaults" (user's source choice and system setup).
RESET_KEEPS = {"player", "system"}
RESTART_REQUIRED = {"port"}


def defaults() -> dict:
    return {k: f.default for k, f in SCHEMA.items()}


class InvalidValue(ValueError):
    pass


def coerce(key: str, value: Any) -> Any:
    """Validate one value; clamp numbers; raise InvalidValue otherwise."""
    f = SCHEMA[key]
    if f.kind == "bool":
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.lower() in ("true", "false"):
            return value.lower() == "true"
        raise InvalidValue("expected true/false")
    if f.kind in ("int", "float"):
        if isinstance(value, bool):
            raise InvalidValue("expected a number")
        try:
            num = float(value)
        except (TypeError, ValueError):
            raise InvalidValue("expected a number") from None
        if num != num or num in (float("inf"), float("-inf")):
            raise InvalidValue("expected a finite number")
        if f.min is not None:
            num = max(f.min, num)
        if f.max is not None:
            num = min(f.max, num)
        return int(round(num)) if f.kind == "int" else round(num, 4)
    if f.kind == "choice":
        if value in f.choices:
            return value
        raise InvalidValue(f"expected one of {', '.join(map(str, f.choices))}")
    if f.kind == "color":
        if isinstance(value, str) and _HEX.match(value.strip()):
            v = value.strip().lower()
            if len(v) == 4:
                v = "#" + "".join(c * 2 for c in v[1:])
            return v
        raise InvalidValue("expected a hex color like #ffffff")
    if f.kind == "str":
        if not isinstance(value, str):
            raise InvalidValue("expected a string")
        v = value.strip()[: f.max_len]
        if not v:
            raise InvalidValue("must not be empty")
        return v
    raise InvalidValue("unknown field type")  # pragma: no cover


def migrate(raw: dict) -> dict:
    """Bring an older settings dict up to SCHEMA_VERSION."""
    version = raw.get("schema_version", 0)
    if not isinstance(version, int):
        version = 0
    if version < 1:
        # Pre-release drafts had auto/locked modes; keep a locked choice.
        if raw.get("player_mode") == "locked" and raw.get("locked_player"):
            raw.setdefault("selected_player", raw["locked_player"])
    raw["schema_version"] = SCHEMA_VERSION
    return raw


def sanitize(raw: Any) -> dict:
    """Full settings dict from untrusted input: defaults + valid values."""
    out = defaults()
    if not isinstance(raw, dict):
        return out
    raw = migrate(dict(raw))
    for k in SCHEMA:
        if k in raw:
            try:
                out[k] = coerce(k, raw[k])
            except InvalidValue:
                log.warning("settings: ignoring invalid %s=%r", k, raw[k])
    return out


class SettingsStore:
    def __init__(self, path: Optional[str]) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._listeners: list[Callable[[dict, set], None]] = []
        self._data = self._load()

    # ---------------------------------------------------------------- io

    def _load(self) -> dict:
        if not self.path or not os.path.exists(self.path):
            return defaults()
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                return sanitize(json.load(f))
        except Exception:
            log.exception("settings: could not read %s, using defaults", self.path)
            try:  # keep the broken file around for debugging
                os.replace(self.path, self.path + ".broken")
            except OSError:
                pass
            return defaults()

    def save(self) -> None:
        if not self.path:
            return
        data = {"schema_version": SCHEMA_VERSION, **self._data}
        d = os.path.dirname(self.path) or "."
        os.makedirs(d, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".settings-", suffix=".json", dir=d)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, sort_keys=True)
            os.replace(tmp, self.path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    # ------------------------------------------------------------ access

    def all(self) -> dict:
        with self._lock:
            return dict(self._data)

    def get(self, key: str) -> Any:
        return self._data[key]

    def add_listener(self, cb: Callable[[dict, set], None]) -> None:
        """cb(settings, changed_keys) after every successful change."""
        self._listeners.append(cb)

    def update(self, partial: Any) -> tuple[dict, dict, set]:
        """Apply a partial update. Returns (settings, errors, changed_keys)."""
        errors: dict[str, str] = {}
        changed: set[str] = set()
        if not isinstance(partial, dict):
            return self.all(), {"_": "expected a JSON object"}, changed
        with self._lock:
            for k, v in partial.items():
                if k not in SCHEMA:
                    continue  # unknown keys are ignored
                try:
                    nv = coerce(k, v)
                except InvalidValue as e:
                    errors[k] = str(e)
                    continue
                if self._data[k] != nv:
                    self._data[k] = nv
                    changed.add(k)
            snapshot = dict(self._data)
        if changed:
            self._persist_and_notify(snapshot, changed)
        return snapshot, errors, changed

    def set(self, key: str, value: Any) -> None:
        _, errors, _ = self.update({key: value})
        if errors:
            raise InvalidValue(errors[key])

    def reset(self) -> tuple[dict, set]:
        """Reset everything except the source choice and system settings."""
        d = defaults()
        partial = {k: d[k] for k, f in SCHEMA.items() if f.group not in RESET_KEEPS}
        settings, _, changed = self.update(partial)
        return settings, changed

    def _persist_and_notify(self, snapshot: dict, changed: set) -> None:
        try:
            self.save()
        except Exception:
            log.exception("settings: save failed")
        for cb in list(self._listeners):
            try:
                cb(snapshot, changed)
            except Exception:
                log.exception("settings listener failed")


def schema_json() -> dict:
    """Schema description for the dashboard (ranges, choices, defaults)."""
    out = {}
    for k, f in SCHEMA.items():
        d: dict[str, Any] = {"type": f.kind, "default": f.default, "group": f.group}
        if f.min is not None:
            d["min"] = f.min
        if f.max is not None:
            d["max"] = f.max
        if f.choices:
            d["choices"] = list(f.choices)
        out[k] = d
    return out
