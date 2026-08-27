# hydra-rpc

Show "Playing <Game>" in Discord (via [arRPC](https://github.com/OpenAsar/arrpc)) for Windows
games launched on Linux through [Hydra Launcher](https://github.com/hydralauncher/hydra) —
or any other Wine-based launcher.

## Compatibility

`hydra-rpc` is **launcher-agnostic**: it doesn't care what compatibility layer started
the game, it just looks for a running `.exe`. It is designed to work with:

- **Proton** (standard) and **Proton GE**
- **UMU-Proton** (Hydra's default)
- **plain Wine**
- **Lutris**, **Bottles**, **Heroic Games Launcher**, **Rare**

As long as the game is a Windows `.exe` (not a native Linux binary) and its basename is
in Discord's detectable database (or in your `overrides`), it will show up. Detection
requires the game process to be visible in `/proc`; sandboxed or containerized runners
that hide the game process may not be detectable.

## Support at a glance

**Supported**

- Windows games launched on Linux through Hydra, standard Proton, Proton GE, UMU-Proton,
  plain Wine, Lutris, Bottles, Heroic, or similar launchers.
- Vesktop/Vencord, ArmCord, or browser Discord setups that consume arRPC.
- Games listed in Discord's detectable-applications database, plus manually configured
  executable overrides.

**Not supported**

- Native Linux game binaries.
- The official native Discord desktop client, because it does not consume arRPC.
- Games whose process is hidden by a sandbox or PID namespace, unless the runner exposes
  the `.exe` in `/proc`.
- More than one visible activity at the same time; one mapped game is selected.

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

1. Scans `/proc/*/cmdline` for Windows executables running under any Wine/Proton
   runtime (any argument ending in `.exe`, minus a blocklist of Wine service processes).
2. Maps each executable to its Discord application id using Discord's
   [`applications/detectable`](https://discord.com/api/v9/applications/detectable)
   database (over 20,000 detectable applications; the live count can change), cached
   locally and auto-refreshed every 7 days.
3. Sends `SET_ACTIVITY` to arRPC's IPC socket when a game starts or changes, refreshes
   it periodically to detect lost connections, and clears it when the game exits.

The last working arRPC socket is tried first during the current run. If arRPC is
unavailable, reconnect attempts back off from 1 second up to 60 seconds instead of
retrying continuously at the normal scan interval.

## Requirements

- **Linux** (uses `/proc`).
- **Python 3** (stdlib only — no `pip install` needed).
- A running [arRPC](https://github.com/OpenAsar/arrpc) instance — either the standalone
  `arrpc` package, or a client's built-in arRPC. It owns the
  `$XDG_RUNTIME_DIR/discord-ipc-*` socket and forwards activity to your client.
- A Discord client that consumes arRPC, e.g. [Vesktop](https://github.com/Vencord/Vesktop)
  with the **WebRichPresence (arRPC)** plugin enabled, [ArmCord](https://github.com/ArmCord/ArmCord),
  or Discord in a browser with the arRPC userscript/extension.

> **Note:** the official native Discord desktop app does **not** use arRPC, so it is
> not supported. It relies on Discord's own game detection, which is exactly what
> misses Wine/Proton games — this tool only works with arRPC-based clients.

### Flatpak / Snap sandboxing

If arRPC or your client runs sandboxed, the `discord-ipc-*` socket can be hidden.
For a Flatpak client you'll typically need a filesystem override such as
`flatpak override --user --filesystem=xdg-run/discord-ipc-0 <app>`.

## Install

```sh
git clone https://github.com/skypetroller/hydra-rpc.git
cd hydra-rpc
sudo cp hydra-rpc /usr/local/bin/hydra-rpc
```

> The provided `hydra-rpc.desktop` and `hydra-rpc.service` assume the binary is at
> `/usr/local/bin/hydra-rpc`. If you install to `~/.local/bin` instead, edit the
> `Exec=` / `ExecStart=` lines to match, and make sure `~/.local/bin` is on your `$PATH`
> (autostart entries don't always pick it up).

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

## Troubleshooting

Run the program in a terminal to see its diagnostics:

```sh
/usr/local/bin/hydra-rpc  # or: ~/.local/bin/hydra-rpc
```

You should see `loaded ... executable mappings`, followed by `detected game: ...`
when a supported game is running. Stop a foreground copy with `Ctrl-C`.

For a systemd installation, inspect its status and live logs:

```sh
systemctl --user status hydra-rpc.service
journalctl --user -u hydra-rpc.service -f
```

Check that arRPC created an IPC socket:

```sh
ls -l "${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"/discord-ipc-*
```

If multiple arRPC instances are running, set `socket_path` to the exact socket in
`~/.config/hydra-rpc/config.json`. The value supports `~` and environment variables:

```json
{
  "socket_path": "${XDG_RUNTIME_DIR}/discord-ipc-0"
}
```

Automatic discovery checks up to three local `discord-ipc-*` sockets with a one-second
per-socket connection timeout. The limit can be changed with `max_socket_attempts` from
1 to 10; this is the total number of paths tried per cycle, including the cached path.
Setting `socket_path` avoids discovery entirely and is recommended when you always use
the same arRPC instance.

If a game is reported as unrecognized, add an override as described below. To force
a fresh detectable-applications database, remove the cache and restart:

```sh
rm -f ~/.cache/hydra-rpc/detectable.json
```

## Uninstall

```sh
# 1. Stop it (systemd users)
systemctl --user disable --now hydra-rpc.service

# 2. Remove the autostart entry and/or service file
rm -f ~/.config/autostart/hydra-rpc.desktop
rm -f ~/.config/systemd/user/hydra-rpc.service
systemctl --user daemon-reload

# 3. Remove the binary
sudo rm -f /usr/local/bin/hydra-rpc    # or: rm -f ~/.local/bin/hydra-rpc

# 4. Remove config and cached game database (optional)
rm -rf ~/.config/hydra-rpc
rm -rf ~/.cache/hydra-rpc
```

If you started a detached manual copy, stop only the hydra-rpc process:

```sh
pkill -f '[/]hydra-rpc$'
```

## Development

Run the dependency-free test suite locally:

```sh
python3 -m unittest discover --start-directory tests --verbose
```

Tests run automatically for pushes and pull requests through GitHub Actions.

## Configuration

`~/.config/hydra-rpc/config.json` (created automatically on first run):

| Key                        | Default                          | Description                                      |
| -------------------------- | -------------------------------- | ------------------------------------------------ |
| `poll_seconds`             | `5`                              | How often to rescan `/proc`                      |
| `activity_refresh_seconds` | `60`                             | How often to refresh an active presence          |
| `db_url`                   | Discord detectable endpoint      | Source of the executable -> app-id database      |
| `db_ttl_seconds`           | `604800` (7 days)                | How long the cached database is considered fresh |
| `socket_dir`               | `""` (`$XDG_RUNTIME_DIR`)        | Directory used when searching for arRPC sockets  |
| `socket_path`              | `""` (automatic)                 | Exact arRPC socket path; useful with multiple instances |
| `max_socket_attempts`      | `3`                              | Maximum total automatic paths per cycle (1-10) |
| `blocklist`                | Wine service processes           | Executables to never report as games             |
| `overrides`                | `{}`                             | Manual `"exe" -> {"id","name"}` mappings       |

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

The running process for a game looks like one of these (the `.exe` is what
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
- Windows games running via any Wine/Proton runtime only — native Linux binaries are
  not scanned.
- Only games present in Discord's detectable database (or with an override) show up.
- Only one activity is published at a time; shared executable names may require an
  override to identify the correct game.
- Detection depends on the `.exe` appearing in a visible process command line; unusual
  wrappers or isolated PID namespaces may require a launcher-specific adjustment.

## License

[MIT](LICENSE)
