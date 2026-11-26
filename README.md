# kitty_plugin_qc — qc

SSH-подключения, туннели (демон), быстрые команды и пароли — не выходя из терминала.
Меню: kitty — `Ctrl+Shift+M`, bash в любом терминале (Konsole) — `Ctrl+G`.

## Установка
```
pipx install git+https://github.com/frxmthe17/kitty_plugin_qc.git
qc setup [адрес-репозитория-данных]
```
`qc setup` ставит службы systemd (`qcd` — туннели, `qc-sync.timer` — синхронизация данных),
`Include` в `~/.ssh/config`, горячие клавиши в `~/.bashrc` и `kitty.conf` (в блоках `# >>> qc >>>`).
`qc uninstall` убирает всё это, данные не трогает. Обновление: `pipx upgrade qc`.

## Данные
`~/qc` — отдельный приватный git-репозиторий: `ssh/ tunnels/ commands/ secure/` (формат — `~/qc/README.md`).
Адрес задаётся в меню «Настройки» или `qc config sync.remote <адрес>`; синхронизация раз в день сама,
`qc sync` — сейчас.

## Команды
```
qc                       меню
qc ssh <сервер>          подключиться
qc t                     туннели и их состояние
qc t up|down|restart <туннель|группа|all>
qc t logs <туннель>
qc check                 конфликты портов, битые ссылки
qc status | qc sync | qc config
```

## Разработка
`pipx install -e .` — правки в `src/qc` сразу в деле (демон: `systemctl --user restart qcd`).
Песочница: `QC_HOME`, `QC_SOCKET`, `XDG_CONFIG_HOME`, `XDG_STATE_HOME`, `QC_USER_SSH_CONFIG`,
`QC_SSH` (подменить ssh), `KITTY_CONFIG_DIRECTORY`.
