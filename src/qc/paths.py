"""Where things live. Everything can be overridden with environment variables (used by tests)."""
import os
from pathlib import Path

HOME = Path.home()


def _xdg(var: str, default: Path) -> Path:
    return Path(os.environ.get(var) or default)


def data_dir() -> Path:
    """User data (a git repo): ssh/ tunnels/ commands/ secure/."""
    return Path(os.environ.get("QC_HOME") or HOME / "qc").expanduser()


def config_dir() -> Path:
    """Settings of this computer (not synced)."""
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


def control_path(host: str) -> Path:
    """ssh master socket of a kept-open connection (a tunnel with only `via`); ssh_config points the host to it."""
    return runtime_dir() / "qc-cm" / host
