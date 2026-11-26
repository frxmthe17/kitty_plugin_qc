# kitty_plugin_qc — qc

SSH connections, tunnels (daemon), quick commands and passwords — without leaving the terminal.
Menu: kitty — `Ctrl+Shift+M`, bash in any terminal (Konsole) — `Ctrl+G`.
The menu opens as a window centered on screen, on top of everything (a kitty panel; needs kitty and a graphical session,
otherwise it opens below the prompt). `qc config menu.window popup|inline|full`, `qc config menu.size 110x32`
(for kitty, after changing it run `qc setup` or reload the block in kitty.conf).

Connections, tunnels and commands look like folders and files: Enter / → — open a folder, ← / Backspace — back,
search looks in the current folder and below.
- `Ctrl+E` — edit in vim (an entry — cursor on its line; a folder — its files), `Ctrl+N` — new entry in the folder.
  After you exit, the file is checked: on an error it offers to reopen the file at that line; if all is well —
  a commit in `~/qc`. Editor: `qc config edit.editor <command>`, otherwise `$VISUAL`/`$EDITOR`/vim.
- Tunnels: `Ctrl+U` — start, `Ctrl+D` — stop (the tunnel under the cursor or the whole folder), Enter — toggle.

## Installation
```
pipx install git+https://github.com/frxmthe17/kitty_plugin_qc.git
qc setup [data-repo-url]
```
`qc setup` installs systemd services (`qcd` — tunnels, `qc-sync.timer` — data sync),
an `Include` in `~/.ssh/config`, hotkeys in `~/.bashrc` and `kitty.conf` (inside `# >>> qc >>>` blocks).
`qc uninstall` removes all of that and leaves the data alone. Upgrade: `pipx upgrade qc`.

## Data
`~/qc` is a separate private git repo: `ssh/ tunnels/ commands/ secure/` (format — see `~/qc/README.md`).
The URL is set in the Settings menu or with `qc config sync.remote <url>`; sync runs automatically once a day,
`qc sync` — right now.

## Passwords
Passwords live in the data repo too, encrypted with [age](https://age-encryption.org) — so they sync through
your private git like everything else:

- `secure/<name>.age` — one file per password, encrypted to the vault's public key (`secure/recipient.txt`).
  Adding a password needs no master password; reading one does.
- `secure/identity.age` — the vault's private key, itself encrypted with your master password.
  A new computer needs nothing but the repo and the master password. Lose the master password — lose the passwords.
- A server refers to a password by name: `password = "db"` in `ssh/*.toml`.

The daemon keeps the vault unlocked in memory for `vault.timeout_hours` (8 by default), so `qc ssh`, tunnels
and the menu do not ask again. ssh gets the password through `qc-askpass` (`SSH_ASKPASS`): it never appears
in the command line or the shell history. A saved password that the server rejects is not sent again
(fail2ban). Menu: the Passwords tab — Enter copies (the clipboard is cleared after 30 s), Ctrl+T types it
into the kitty window, Ctrl+N / Ctrl+E / Ctrl+D — new / change / delete, Ctrl+L — lock.

Tunnels ask like a browser: if ssh needs a password that is not saved (or the saved one is rejected),
the tunnel waits and the menu (or `qc t up`) shows a password field — one per server on the way, jump hosts
included, up to 3 tries. When the tunnel is up, qc asks whether to save each typed password; for a server
without `password = …` it adds that line to its `ssh/*.toml`. Typed passwords that are not saved live only
in the daemon's memory until the tunnel is stopped (so it can reconnect). One-time codes are asked the same
way and never kept.

Note: the KDE clipboard history (Klipper) may still remember a copied password; Ctrl+T in kitty does not touch the clipboard.

## Commands
```
qc                       menu
qc ssh <server>          connect
qc t                     tunnels and their state
qc t up|down|restart <tunnel|folder|all>   (a folder includes its subfolders)
qc t logs <tunnel>
qc edit [name|folder|file] edit in vim (no name — the ~/qc folder)
qc pass [ls|add|set|rm|get|copy|init|passwd] [name]   password vault
qc unlock | qc lock      unlock / lock the vault in the daemon
qc check                 port conflicts, broken references
qc status | qc sync | qc config
```

## Development
`pipx install -e .` — changes in `src/qc` take effect immediately (daemon: `systemctl --user restart qcd`).
Sandbox: `QC_HOME`, `QC_SOCKET`, `XDG_CONFIG_HOME`, `XDG_STATE_HOME`, `QC_USER_SSH_CONFIG`,
`QC_SSH` (substitute ssh), `KITTY_CONFIG_DIRECTORY`.
