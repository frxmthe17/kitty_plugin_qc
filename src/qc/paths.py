"""Где что лежит. Всё переопределяется переменными окружения (нужно для тестов)."""
import os
from pathlib import Path

HOME = Path.home()


def _xdg(var: str, default: Path) -> Path:
    return Path(os.environ.get(var) or default)


def data_dir() -> Path:
    """Данные пользователя (git-репозиторий): ssh/ tunnels/ commands/ secure/."""
    return Path(os.environ.get("QC_HOME") or HOME / "qc").expanduser()


def config_dir() -> Path:
    """Настройки этого компьютера (не синхронизируются)."""
    return _xdg("XDG_CONFIG_HOME", HOME / ".config") / "qc"


def state_dir() -> Path:
    return _xdg("XDG_STATE_HOME", HOME / ".local" / "state") / "qc"


def runtime_dir() -> Path:
    return _xdg("XDG_RUNTIME_DIR", Path(f"/tmp/qc-{os.getuid()}"))


def socket_path() -> Path:
    return Path(os.environ.get("QC_SOCKET") or runtime_dir() / "qc.sock")


def generated_ssh_config() -> Path:
    return data_dir() / ".generated" / "ssh_config"


def ssh_bin() -> str:
    return os.environ.get("QC_SSH") or "ssh"
