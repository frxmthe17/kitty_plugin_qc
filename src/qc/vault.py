"""Password vault in ~/qc/secure — safe to keep in a (private) git repo.

Layout:
  secure/recipient.txt   public age key ("age1…"); anyone with the repo can ADD passwords, nobody can read them
  secure/identity.age    the private age key, itself encrypted with the master password (scrypt)
  secure/<name>.age      one password per file, encrypted to the public key — so two computers editing
                         different passwords never conflict in git

Reading a password needs the private key, which needs the master password. The daemon keeps the unlocked key
in memory for a while (settings vault.timeout_hours), so ssh, tunnels and the menu do not ask every time.
"""
from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path

from qc import paths

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
IDENTITY = "identity.age"
RECIPIENT = "recipient.txt"
GITATTRIBUTES = "*.age binary\n"


class VaultError(Exception):
    pass


def _pyrage():
    try:
        import pyrage
        from pyrage import passphrase, x25519  # noqa: F401
    except ImportError:
        raise VaultError("the pyrage library is missing — reinstall qc: pipx install --force qc") from None
    return pyrage


def secure_dir() -> Path:
    return paths.data_dir() / "secure"


def initialized() -> bool:
    d = secure_dir()
    return (d / IDENTITY).is_file() and (d / RECIPIENT).is_file()


def check_name(name: str) -> str:
    name = name.strip()
    if not NAME_RE.match(name) or name.endswith(".age"):
        raise VaultError(f"bad name '{name}': only Latin letters, digits, . _ -")
    return name


def entry_path(name: str) -> Path:
    return secure_dir() / f"{check_name(name)}.age"


def names() -> list[str]:
    d = secure_dir()
    if not d.is_dir():
        return []
    return sorted(p.stem for p in d.glob("*.age") if p.name != IDENTITY and NAME_RE.match(p.stem))


def _write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    os.replace(tmp, path)


def init(master: str) -> None:
    """Create the vault: a new age key, its private half locked with the master password."""
    if initialized():
        raise VaultError("the vault already exists")
    if not master:
        raise VaultError("the master password is empty")
    pyrage = _pyrage()
    ident = pyrage.x25519.Identity.generate()
    d = secure_dir()
    _write(d / IDENTITY, pyrage.passphrase.encrypt(str(ident).encode(), master))
    (d / RECIPIENT).write_text(str(ident.to_public()) + "\n")
    ga = d / ".gitattributes"
    if not ga.exists():
        ga.write_text(GITATTRIBUTES)
    (d / ".gitkeep").unlink(missing_ok=True)


def _recipient():
    pyrage = _pyrage()
    try:
        return pyrage.x25519.Recipient.from_str((secure_dir() / RECIPIENT).read_text().strip())
    except (OSError, pyrage.RecipientError) as e:
        raise VaultError(f"the vault is not set up ({e})") from None


def unlock(master: str):
    """Master password → the private key (pyrage Identity). Slow on purpose (scrypt, about a second)."""
    pyrage = _pyrage()
    try:
        blob = (secure_dir() / IDENTITY).read_bytes()
    except OSError:
        raise VaultError("the vault is not set up — create it in the Passwords tab or with qc pass init") from None
    try:
        return pyrage.x25519.Identity.from_str(pyrage.passphrase.decrypt(blob, master).decode())
    except pyrage.DecryptError:
        raise VaultError("wrong master password") from None


def change_master(old: str, new: str) -> None:
    if not new:
        raise VaultError("the master password is empty")
    ident = unlock(old)
    _write(secure_dir() / IDENTITY, _pyrage().passphrase.encrypt(str(ident).encode(), new))


def put(name: str, secret: str) -> None:
    """Add or replace a password. Needs only the public key — no master password."""
    if not secret:
        raise VaultError("the password is empty")
    _write(entry_path(name), _pyrage().encrypt(secret.encode(), [_recipient()]))


def remove(name: str) -> None:
    try:
        entry_path(name).unlink()
    except FileNotFoundError:
        raise VaultError(f"no password '{name}'") from None


def get(name: str, identity) -> str:
    pyrage = _pyrage()
    try:
        blob = entry_path(name).read_bytes()
    except OSError:
        raise VaultError(f"no password '{name}' in the vault") from None
    try:
        return pyrage.decrypt(blob, [identity]).decode()
    except pyrage.DecryptError:
        raise VaultError(f"password '{name}' was encrypted with another vault key") from None


def commit(message: str) -> None:
    """Commit the change to ~/qc right away (it goes to the server with the next sync)."""
    from qc import sync

    try:
        if sync.is_repo():
            sync.commit_local(message)
    except Exception:  # noqa: BLE001 — the file is already saved; qc sync will commit it later
        pass


def fingerprint(secret: str) -> str:
    """To compare clipboard contents without keeping the password around."""
    return hashlib.sha256(secret.encode()).hexdigest()


# ---------- ssh prompts ----------

# "user@host's password: " (password auth) and "(user@host) Password: " (keyboard-interactive)
PROMPT_RE = re.compile(r"(?:\(([^@()\s]+)@([^()\s]+)\)\s*)?(?:([^@\s]+)@([^'\s]+)'s\s+)?password\s*:", re.I)


def parse_prompt(prompt: str) -> tuple[str | None, str | None] | None:
    """(user, host) from an ssh password prompt; None if this is not a password prompt
    (host key confirmation, key passphrase…)."""
    if "passphrase" in prompt.lower():
        return None
    m = PROMPT_RE.search(prompt)
    if not m:
        return None
    user = m.group(1) or m.group(3)
    host = m.group(2) or m.group(4)
    return user, host


def match_host(data, prompt: str, chain: list[str], any_host: bool = False) -> str | None:
    """Which server from ~/qc/ssh the prompt is about. The tunnel/connection chain goes first,
    so two servers with the same address but different passwords do not get mixed up.
    any_host — also servers without a saved password (a tunnel asks the user for those)."""
    parsed = parse_prompt(prompt)
    if parsed is None:
        return None
    user, host = parsed
    pool = [n for n in chain if n in data.hosts] + [n for n in data.hosts if n not in chain]
    with_pw = [n for n in pool if any_host or data.hosts[n].password]
    if host is None:
        in_chain = [n for n in chain if n in data.hosts and (any_host or data.hosts[n].password)]
        return in_chain[0] if len(in_chain) == 1 else None
    for name in with_pw:
        h = data.hosts[name]
        if host in (h.host, name) and (user is None or h.user is None or user == h.user):
            return name
    return None


def host_chain(data, name: str) -> list[str]:
    """The server and its jump hosts: ['db', 'bastion']."""
    out: list[str] = []
    while name and name in data.hosts and name not in out:
        out.append(name)
        name = data.hosts[name].jump
    return out


def chain_passwords(data, name: str) -> list[str]:
    """Vault entries needed to connect to the server (itself and its jump hosts)."""
    return [data.hosts[n].password for n in host_chain(data, name) if data.hosts[n].password]
