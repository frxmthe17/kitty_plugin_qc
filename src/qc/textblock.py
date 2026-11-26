"""Помеченные блоки «# >>> qc >>> … # <<< qc <<<» в чужих файлах (.bashrc, ~/.ssh/config, kitty.conf).
qc правит только внутри своего блока; `qc uninstall` убирает блок целиком."""
from __future__ import annotations

from pathlib import Path

BEGIN = "# >>> qc >>>"
END = "# <<< qc <<<"


def _split(text: str) -> tuple[str, str | None, str]:
    i = text.find(BEGIN)
    if i < 0:
        return text, None, ""
    j = text.find(END, i)
    if j < 0:
        return text, None, ""
    j += len(END)
    if j < len(text) and text[j] == "\n":
        j += 1
    return text[:i], text[i:j], text[j:]


def has_block(path: Path) -> bool:
    try:
        return _split(path.read_text())[1] is not None
    except OSError:
        return False


def put_block(path: Path, body: str, top: bool = False) -> None:
    text = path.read_text() if path.exists() else ""
    block = f"{BEGIN}\n{body.rstrip()}\n{END}\n"
    before, old, after = _split(text)
    if old is not None:
        new = before + block + after
    elif top:
        new = block + ("\n" + text if text else "")
    else:
        new = text + ("" if not text or text.endswith("\n") else "\n") + ("\n" if text else "") + block
    if new != text:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(new)


def remove_block(path: Path) -> bool:
    if not path.exists():
        return False
    before, old, after = _split(path.read_text())
    if old is None:
        return False
    if before.endswith("\n\n") and not after:
        before = before[:-1]
    path.write_text(before + after)
    return True


def text_outside_block(path: Path) -> str:
    try:
        before, _, after = _split(path.read_text())
    except OSError:
        return ""
    return before + after
