"""Settings of this computer: ~/.config/qc/settings.toml (not part of the data repository)."""
from __future__ import annotations

import tomllib

from qc import paths

DEFAULTS: dict[str, dict] = {
    "sync": {"remote": "", "schedule": "daily"},  # daily | hourly | off
    # menu: popup is a window centered on screen above everything (kitty panel), inline is below the prompt, full is the whole terminal
    "menu": {"window": "popup", "size": "110x32"},
    # the daemon locks the password vault after this many hours (0 — only by hand or when it stops);
    # a copied password is cleared from the clipboard after clipboard_seconds
    "vault": {"timeout_hours": 8, "clipboard_seconds": 30},
}
WINDOWS = ("popup", "inline", "full")
SCHEDULES = ("daily", "hourly", "off")


def _file():
    return paths.config_dir() / "settings.toml"


def load() -> dict:
    out = {k: dict(v) for k, v in DEFAULTS.items()}
    try:
        with _file().open("rb") as f:
            for section, values in tomllib.load(f).items():
                if isinstance(values, dict):
                    out.setdefault(section, {}).update(values)
    except (OSError, tomllib.TOMLDecodeError):
        pass
    return out


def _toml_value(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    return '"' + str(v).replace("\\", "\\\\").replace('"', '\\"') + '"'


def save(s: dict) -> None:
    lines = ["# qc settings on this computer. Change them via the 'Settings' menu or `qc config`.", ""]
    for section, values in s.items():
        lines.append(f"[{section}]")
        lines += [f"{k} = {_toml_value(v)}" for k, v in values.items()]
        lines.append("")
    f = _file()
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("\n".join(lines))


def get(key: str):
    section, _, name = key.partition(".")
    return load().get(section, {}).get(name)


def set_value(key: str, value) -> None:
    section, _, name = key.partition(".")
    if not name:
        raise ValueError("key must be section.name, e.g. sync.remote")
    s = load()
    s.setdefault(section, {})[name] = value
    save(s)
