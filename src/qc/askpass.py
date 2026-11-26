"""qc-askpass: ssh calls it (SSH_ASKPASS) for a password; the answer comes from the unlocked vault in the daemon.

Prints the password and exits 0, or exits 1 — then ssh treats it as "no password" and stops.
Only password prompts are answered: host key questions and key passphrases are refused.
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path


def program() -> str:
    """Path to qc-askpass: next to the running Python (pipx venv), otherwise from PATH."""
    near = Path(sys.executable).parent / "qc-askpass"
    if near.exists():
        return str(near)
    return shutil.which("qc-askpass") or "qc-askpass"


def env(chain: list[str], token: str, tunnel: str = "") -> dict[str, str]:
    """Environment for an ssh process that takes passwords from the vault — or, for a tunnel,
    from the user through the menu when the vault has none."""
    e = dict(os.environ)
    e.update({
        "SSH_ASKPASS": program(),
        "SSH_ASKPASS_REQUIRE": "force",
        "QC_ASKPASS_CHAIN": ",".join(chain),
        "QC_ASKPASS_TOKEN": token,
        "QC_ASKPASS_TUNNEL": tunnel,
    })
    return e


def main() -> None:
    from qc import client, vault

    prompt = " ".join(sys.argv[1:])
    tunnel = os.environ.get("QC_ASKPASS_TUNNEL", "")
    if not tunnel and vault.parse_prompt(prompt) is None:
        sys.exit(1)
    try:
        # a tunnel may wait for the user to type the password in the menu — up to ~100 s
        r = client.request("askpass", timeout=130 if tunnel else 10, prompt=prompt,
                           chain=[c for c in os.environ.get("QC_ASKPASS_CHAIN", "").split(",") if c],
                           token=os.environ.get("QC_ASKPASS_TOKEN", ""), tunnel=tunnel)
    except (client.DaemonDown, OSError, ValueError) as e:
        print(f"qc-askpass: {e}", file=sys.stderr)
        sys.exit(1)
    if not r.get("ok"):
        print(f"qc-askpass: {r.get('error', 'no password')}", file=sys.stderr)
        sys.exit(1)
    sys.stdout.write(r["secret"] + "\n")
    sys.stdout.flush()


if __name__ == "__main__":
    main()
