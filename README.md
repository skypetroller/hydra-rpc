# hydra-rpc

Show "Playing <Game>" in Discord (via [arRPC](https://github.com/OpenAsar/arrpc)) for games
launched through [Hydra Launcher](https://github.com/hydralauncher/hydra) on Linux.

## The problem

- Hydra Launcher has no Discord Rich Presence support.
- arRPC's Linux process scanner only matches the executable's own path, so it never
  sees the `.exe` name inside a `wine` / `proton` / `umu-run` process (see
  [OpenAsar/arrpc#35](https://github.com/OpenAsar/arrpc/issues/35)).

Hydra runs Windows games through umu/Proton, so games launched from Hydra never show
up as activity. `hydra-rpc` fills that gap.

## How it works

```
Hydra -> umu/wine -> Game.exe (running)
                          |
              /proc scanning (every 5s)
                          |
                     hydra-rpc  --maps .exe->app id-->  Discord's detectable DB
                          |
              SET_ACTIVITY over the Discord IPC socket
                          |
                        arRPC  --bridge/websocket-->  Vesktop (or any client)
```

`hydra-rpc` is a dependency-free Python 3 script that:

1. Scans `/proc/*/cmdline` for Windows executables running under Wine/Proton/umu
   (any argument ending in `.exe`, minus a blocklist of Wine service processes).
2. Maps each executable to its Discord application id using Discord's
   [`applications/detectable`](https://discord.com/api/v9/applications/detectable)
   database (~10,000 games), cached locally and auto-refreshed every 7 days.
3. Sends `SET_ACTIVITY` to arRPC's IPC socket, and clears it when the game exits.

## Requirements

- Linux with a running [arRPC](https://github.com/OpenAsar/arrpc) instance
  (it owns the `$XDG_RUNTIME_DIR/discord-ipc-*` socket and forwards to your client).
- A Discord client that consumes arRPC, e.g. [Vesktop](https://github.com/Vencord/Vesktop)
  with the **WebRichPresence (arRPC)** plugin enabled.
- Python 3 (stdlib only — no `pip install` needed).

## Install

```sh
git clone https://github.com/<you>/hydra-rpc.git
cd hydra-rpc
sudo cp hydra-rpc /usr/local/bin/hydra-rpc   # or: cp hydra-rpc ~/.local/bin/
```

Run it once to generate the default config and cache the game database:

```sh
hydra-rpc
```

Start it at login with either the provided autostart entry or a systemd user service:

```sh
# GNOME/KDE autostart
mkdir -p ~/.config/autostart
cp hydra-rpc.desktop ~/.config/autostart/

# or systemd user service
mkdir -p ~/.config/systemd/user
cp hydra-rpc.service ~/.config/systemd/user/
systemctl --user enable --now hydra-rpc.service
```

## Configuration

`~/.config/hydra-rpc/config.json` (created automatically on first run):

| Key               | Default                          | Description                                      |
| ----------------- | -------------------------------- | ------------------------------------------------ |
| `poll_seconds`    | `5`                              | How often to rescan `/proc`                      |
| `db_url`          | Discord detectable endpoint       | Source of the executable -> app-id database      |
| `db_ttl_seconds`  | `604800` (7 days)                | How long the cached database is considered fresh |
| `socket_dir`      | `""` (`$XDG_RUNTIME_DIR`)        | Directory containing arRPC's `discord-ipc-*`     |
| `blocklist`       | Wine service processes           | Executables to never report as games             |
| `overrides`       | `{}`                             | Manual `"exe" -> {"id","name"}` mappings         |

### Overrides

If a game isn't in Discord's database (or maps to the wrong title, e.g. a generic
`nw.exe`), add an override. `id` is a Discord application id — create one free at
<https://discord.com/developers/applications>, or reuse an existing game's id:

```json
{
  "overrides": {
    "nw.exe": { "id": "425749842451496961", "name": "Game Dev Tycoon" }
  }
}
```

## How it detects a game

The running process for a Hydra game looks like one of these (the `.exe` is what
matters — the rest of the path is ignored):

```
c:\windows\system32\umu.exe /mnt/.../Megabonk.exe
S:\Other Games\...\Megabonk.exe
/usr/bin/python3 /opt/Hydra/resources/umu-run /mnt/.../Stardew Valley.exe
```

`hydra-rpc` takes the basename (`megabonk.exe`), lowercases it, and looks it up in
the database. Case and drive-letter/backslash paths are handled automatically.

## Limitations

- Linux only (uses `/proc`).
- Wine/Proton/umu games only — native Linux binaries are not scanned.
- Only games present in Discord's detectable database (or with an override) show up.

## License

[MIT](LICENSE)
