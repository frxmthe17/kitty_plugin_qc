"""Настройки этого компьютера: ~/.config/qc/settings.toml (в репозиторий данных не попадают)."""
from __future__ import annotations

import tomllib

from qc import paths

DEFAULTS: dict[str, dict] = {
    "sync": {"remote": "", "schedule": "daily"},  # daily | hourly | off
}
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
    lines = ["# Настройки qc на этом компьютере. Меняются через меню «Настройки» или `qc config`.", ""]
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
        raise ValueError("ключ вида раздел.имя, например sync.remote")
    s = load()
    s.setdefault(section, {})[name] = value
    save(s)
