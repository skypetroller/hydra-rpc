# hydra-rpc

[![Tests](https://github.com/skypetroller/hydra-rpc/actions/workflows/test.yml/badge.svg)](https://github.com/skypetroller/hydra-rpc/actions/workflows/test.yml)

Show `Playing <Game>` in Discord for Windows games running on Linux through Hydra,
Proton, Wine, or another compatible launcher.

See [CHANGELOG.md](CHANGELOG.md) for release notes and
[config.example.json](config.example.json) for a fully commented-by-example config.
`hydra-rpc.schema.json` provides JSON Schema validation for `config.json`.

## Features

- Detects Windows games running through Proton, Proton GE, UMU-Proton, and Wine.
- Detects native emulators RetroArch, DuckStation, PCSX2, RPCS3, Dolphin, and PPSSPP
  when enabled,
  showing the emulator as the activity with the ROM title in the details.
- Works with Hydra, Heroic, Lutris, Bottles, Steam, and similar launchers.
- Can publish multiple detected game activities at once.
- Saves session start times and process identity so a watcher restart can preserve elapsed
  time without confusing a reused PID with the old game.
- Supports custom activity names and optional Discord fields such as `details`, `state`,
  `assets`, and `buttons`.
- Supports blocklists by executable, Discord application ID, or game name.
- Includes `--dry-run`, `--status` / `--status --json`, and `--validate-config`
  troubleshooting commands (detection table with match reasons, no Discord traffic).
- Includes explicit `--check-update` and `--update` commands.
- Supports optional file logging and automatic arRPC socket recovery.
- Includes an optional Hydra-marker mode that can reduce conflicts with other launchers' RPC.

The tool intentionally reports games generically. It does not add launcher names to your
Discord activity. Hydra-marker mode is a best-effort process filter, not a direct
integration with Hydra or other launchers.

## How It Works

```text
Game.exe running through Wine/Proton
              |
       hydra-rpc scans /proc
              |
  Matches the executable to Discord's game database
              |
   Sends Rich Presence to arRPC over its IPC socket
              |
       Vesktop or another arRPC client displays it
```

The script checks for running `.exe` arguments every 5 seconds. The Discord game database
is cached locally and refreshed every 7 days. Activities are refreshed periodically so a
lost arRPC connection can be detected and restored.

When emulator support is enabled, native emulator processes are detected the same way:
the ROM or game path is read from the emulator's command line and the ROM filename is
shown in the activity details.

## Requirements

- Linux
- Python 3 (no extra Python packages are required)
- A running [arRPC](https://github.com/OpenAsar/arrpc) server, either standalone or
  provided by your Discord client
- A Discord client that consumes arRPC, such as [Vesktop](https://github.com/Vencord/Vesktop)
  with its **WebRichPresence (arRPC)** support enabled, ArmCord, or browser Discord with
  the arRPC extension/userscript

The official native Discord desktop client is not supported because it does not consume
arRPC. It has its own process detection, which commonly misses Wine/Proton games.

## Quick Start

### 1. Install

Copy-paste defaults (paths used below):

```text
script:  /usr/local/bin/hydra-rpc   (system) or ~/.local/bin/hydra-rpc (user)
config:  ~/.config/hydra-rpc/config.json
cache:   ~/.cache/hydra-rpc/detectable.json
service: ~/.config/systemd/user/hydra-rpc.service
```

The easiest installation uses `/usr/local/bin`, which matches the included startup files:

```sh
git clone https://github.com/skypetroller/hydra-rpc.git
cd hydra-rpc
sudo make install
# or without make:
# sudo install -Dm755 hydra-rpc /usr/local/bin/hydra-rpc
```

If you do not want to use `sudo`, install it in your home directory instead:

```sh
make install-user
# or without make:
# mkdir -p ~/.local/bin
# install -Dm755 hydra-rpc ~/.local/bin/hydra-rpc
```

The included `.desktop` and systemd files assume `/usr/local/bin/hydra-rpc`. If you use
`~/.local/bin`, change their `Exec=` or `ExecStart=` line to
`/home/your-user/.local/bin/hydra-rpc`.

#### Distro notes

- **Ubuntu / Debian / Mint / Pop!_OS**: `sudo apt install python3 git`; no extra
  Python packages needed. Use `sudo make install`, then the systemd user service.
- **Fedora / Nobara / Bazzite**: `sudo dnf install python3 git`; SELinux does not
  block `/proc` scanning. Same `sudo make install` path works.
- **Arch / CachyOS / EndeavourOS / Manjaro**: `sudo pacman -S python git`; use
  `sudo make install`. Keep `~/.local/bin` on `PATH` if you use `make install-user`.
- **NixOS / Home Manager**: the script is dependency-free Python, so wrap it as a
  package or run it from `~/.local/bin` via `make install-user`; point
  `ExecStart=` at your Nix store path or `~/.local/bin/hydra-rpc`.
- **Flatpak Discord / Vesktop**: the watcher itself runs on the host (not in the
  Flatpak). The Discord *client* sandbox must be able to see the arRPC socket —
  see "Known issues: Flatpak" below. Prefer a native/host Discord client when
  possible.

### 2. Check the connection

If you use Vesktop, open **Vesktop Settings** and enable **Rich Presence via arRPC**.
If you use a separate arRPC server, also enable Vencord's **WebRichPresence (arRPC)**
plugin and start arRPC first. Other Discord clients may have a similarly named setting.

With arRPC and your Discord client running, check the connection:

```sh
hydra-rpc --validate-config
```

You should see that the game database loaded and an arRPC socket was found.

### 3. Start the watcher

```sh
hydra-rpc
```

Leave it running, launch a supported game, and check your Discord profile. Stop a
foreground watcher with `Ctrl-C`.

### 4. Start it automatically

Use the included systemd user service:

```sh
mkdir -p ~/.config/systemd/user
cp hydra-rpc.service ~/.config/systemd/user/
systemctl --user enable --now hydra-rpc.service
```

Or use the included desktop autostart entry:

```sh
mkdir -p ~/.config/autostart
cp hydra-rpc.desktop ~/.config/autostart/
```

Use only one startup method so you do not start two copies of the watcher.

## Choose A Mode

### Generic mode

Generic mode reports supported games regardless of which launcher started them. It is the
default when `hydra_only` is `false` or missing.

To enable it explicitly:

1. Open `~/.config/hydra-rpc/config.json` in a text editor. You can use
   `xdg-open ~/.config/hydra-rpc/config.json`.
2. Add or change this setting inside the existing JSON object:

   ```json
   "hydra_only": false
   ```

3. Restart the watcher:

   ```sh
   systemctl --user restart hydra-rpc.service
   ```

   If you use autostart or run it manually, stop and start the watcher again instead.

The `hydra_markers` setting has no effect in generic mode.

### Hydra-marker mode

This mode does not inspect which launcher truly started a game, and it does not disable or
monitor another launcher's RPC. It reports a game only when its visible process command
line or environment contains a Hydra marker. Enable it when another launcher has its own
Rich Presence and you want to reduce duplicate reporting:

```json
"hydra_only": true
```

The default markers are `hydralauncher` and `/opt/hydra/`. If Hydra is installed elsewhere,
add a distinctive path to `hydra_markers`:

```json
{
  "hydra_only": true,
  "hydra_markers": ["/path/to/hydra"]
}
```

Do not use a generic `gameid=umu-` marker unless it is unique to your installation, since
other launchers can also use UMU.

## Emulated games

ROM support is off by default. To turn it on:

1. Open `~/.config/hydra-rpc/config.json` in a text editor.
2. Add this setting inside the existing JSON object:

   ```json
   "emulators_enabled": true
   ```

3. Save the file and restart the watcher.

What happens next:

1. Load a game in RetroArch, DuckStation, PCSX2, RPCS3, Dolphin, or PPSSPP.
2. The watcher reads the ROM or game path from the emulator's command line. An
   emulator sitting idle in its menu, with no game loaded, is not reported.
3. Discord shows the emulator as the activity, with the ROM title underneath — for
   example, `Playing RetroArch` with `Super Mario World` below it.

RetroArch, PCSX2, and Dolphin already have Discord application IDs, so they work as
soon as you enable this. DuckStation, RPCS3, and PPSSPP do not, so they are skipped
until you add an ID. Create one free at
<https://discord.com/developers/applications>, then add it like this:

```json
{
  "emulators_enabled": true,
  "emulator_application_ids": {
    "duckstation": "YOUR_DUCKSTATION_APP_ID",
    "rpcs3": "YOUR_RPCS3_APP_ID",
    "ppsspp": "YOUR_PPSSPP_APP_ID"
  }
}
```

To check what the watcher sees without sending anything to Discord, run:

```sh
hydra-rpc --dry-run
```

For control over one specific ROM, add an override. The key is the emulator name, a
colon, and the lowercase ROM title:

```json
{
  "emulator_overrides": {
    "retroarch:super mario world": {
      "id": "123456789012345678",
      "name": "Super Mario World"
    }
  }
}
```

Emulator games obey `hydra_only` like everything else: with it enabled, only emulators
launched with visible Hydra markers are reported. They also obey `max_activities`,
`blocklist_ids`, `blocklist_names`, templates, and `rich_activity`.

## Updating

Check for an update without changing anything:

```sh
hydra-rpc --check-update
```

Install the latest script from this repository:

```sh
hydra-rpc --update
```

If it is installed in `/usr/local/bin`, use:

```sh
sudo /usr/local/bin/hydra-rpc --update
```

The updater uses HTTPS, checks the downloaded file's Python syntax, preserves executable
permissions, and replaces the file atomically. Restart the watcher after updating. It is
explicit and does not update silently at startup.

## Troubleshooting

Preview detection without sending anything to Discord (`--dry-run` now shows
PID, application ID, Hydra sources, and skip reasons):

```sh
hydra-rpc --dry-run
```

One-shot status table — which processes were seen and why each was or was not
matched (no Discord traffic). Ideal for bug reports:

```sh
hydra-rpc --status
hydra-rpc --status --json > status.json
```

Validate config, database, socket, and application-ID formats:

```sh
hydra-rpc --validate-config
```

### How to debug a missing game

```text
1. Is the game running?
     no  -> launch it, then re-run hydra-rpc --status
     yes -> 2. Is its .exe visible in /proc?
2. grep -a -o -E '[^ ]+\.exe' /proc/*/cmdline | sort -u
     no  -> wrapper/sandbox hides the command line (Proton/UMU quirk,
            isolated PID namespace); try the launcher's native mode or an override
            with the visible exe name
     yes -> 3. Is it in Discord's detectable database?
3. hydra-rpc --status  (look for "unknown: not in database")
     no  -> add an "overrides" entry with the exe + Discord application ID
     yes -> 4. Is arRPC connected?
4. hydra-rpc --validate-config; ls -l "${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"/discord-ipc-*
     no socket -> start arRPC / enable WebRichPresence (arRPC) in Vesktop,
                  check Flatpak overrides and multiple-server conflicts below
     socket ok but still missing -> check blocklists, hydra_only filtering,
                  and emulator app IDs in --status reasons
```

### Launcher examples

- **Hydra**: launch the game from Hydra, run `hydra-rpc --status`, confirm the
  `.exe` row says `matched` (or `hydra-filtered` if `hydra_only` is on without a
  marker). Default `hydra_only: false` reports Hydra games like any other launcher.
- **Heroic (Wine/Proton)**: launch via Heroic, check `--status` for
  `Game.exe matched`. UMU-based runs may show long `umu` wrapper lines — the
  underlying `.exe` is still detected.
- **Lutris / Bottles**: same flow; if two launchers wrap the same exe, generic mode
  reports it once. Use `hydra_only: true` only to suppress non-Hydra duplicates.
- **Steam (Proton)**: Steam games appear as `Game.exe` under `steamapps/common`;
  `--status` confirms the mapping. No Steam-specific config needed.
- **RetroArch (emulator)**: enable `emulators_enabled: true`, load a ROM, check
  `--status` for `emulator:retroarch:<rom> matched`. Idle emulator menus (no ROM)
  are intentionally not reported.

Check the watcher service and its logs:

```sh
systemctl --user status hydra-rpc.service
journalctl --user -u hydra-rpc.service -f
```

Check that arRPC created an IPC socket:

```sh
ls -l "${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"/discord-ipc-*
```

If multiple arRPC servers are running, set an exact socket path in
`~/.config/hydra-rpc/config.json`:

```json
{
  "socket_path": "${XDG_RUNTIME_DIR}/discord-ipc-0"
}
```

If a game is not recognized, add an override as described in the configuration section.
To download a fresh game database, remove the cache and restart:

```sh
rm -f ~/.cache/hydra-rpc/detectable.json
```

For a Flatpak Discord client, sandbox permissions may hide the socket. You may need an
override similar to:

```sh
flatpak override --user --filesystem=xdg-run/discord-ipc-0 <app-id>
```

Optional file logging can be enabled with the `log_file` setting. For example:

```json
{
  "log_file": "~/.cache/hydra-rpc/hydra-rpc.log",
  "log_level": "info"
}
```

## Configuration

The configuration file is `~/.config/hydra-rpc/config.json`. It is created automatically
the first time the watcher runs. Existing files are not overwritten when new options are
added, so add new settings manually when needed.

| Setting                    | Default                         | Description                                      |
| -------------------------- | ------------------------------- | ------------------------------------------------ |
| `poll_seconds`             | `5`                             | How often to scan running processes (minimum 1)  |
| `activity_refresh_seconds` | `60`                            | How often to refresh active activities            |
| `refresh_unchanged_activities` | `true`                      | Resend identical activities as a liveness probe; when `false`, unchanged payloads are skipped and the connection is verified with a tiny IPC ping instead |
| `max_activities`           | `0` (all)                       | Use `1`-`10` to limit reported games              |
| `db_url`                   | Discord endpoint                | Source of the executable database                 |
| `db_ttl_seconds`           | `604800` (7 days)               | How long the database cache stays fresh           |
| `socket_dir`               | `$XDG_RUNTIME_DIR`              | Directory used for automatic socket discovery     |
| `socket_path`              | Automatic                       | Exact arRPC socket path (warns if missing)        |
| `max_socket_attempts`      | `3`                             | Maximum automatic socket paths tried (1-10)       |
| `hydra_only`               | `false`                         | Enable best-effort Hydra-marker filtering        |
| `hydra_markers`            | Hydra path markers              | Markers used by Hydra-marker mode                |
| `emulators_enabled`        | `false`                         | Detect 6 supported emulators                    |
| `emulator_application_ids` | RetroArch/PCSX2/Dolphin preset | Per-emulator Discord application IDs (all-digit; empty = unconfigured) |
| `emulator_activity_template` | `"{emulator_name}"`           | Template for the emulator activity name          |
| `emulator_overrides`       | `{}`                            | Per-emulator or per-ROM application mappings     |
| `blocklist`                | Wine service processes          | Executable names never reported                   |
| `blocklist_ids`            | `[]`                            | Discord application IDs never reported (all-digit) |
| `blocklist_names`          | `[]`                            | Case-insensitive game names never reported        |
| `activity_template`        | `"{game_name}"`                | Template for the activity name                   |
| `rich_activity`             | `{}`                            | Additional Discord activity fields               |
| `log_file`                 | `""`                            | Optional log file                                |
| `log_level`                | `"info"`                        | `debug`, `info`, `warning`, or `error`            |
| `overrides`                | `{}`                            | Manual executable-to-application mappings (all-digit IDs) |

Copy [config.example.json](config.example.json) to
`~/.config/hydra-rpc/config.json` as a starting point. Unknown top-level keys
and non-digit application IDs produce startup warnings; run
`hydra-rpc --validate-config` to check. Editors can use
[hydra-rpc.schema.json](hydra-rpc.schema.json) for autocompletion and validation.

### Activity customization

`activity_template` supports `{game_name}`, `{exe}`, and `{app_id}`. The same placeholders
work inside `rich_activity`:

```json
{
  "activity_template": "{game_name} [custom]",
  "rich_activity": {
    "details": "Playing {game_name}",
    "state": "Executable: {exe}",
    "assets": { "large_image": "cover" }
  }
}
```

The application ID remains the game's Discord application, so a client may use the
registered application name when displaying the activity.

### Ignoring games

Use application IDs or case-insensitive names when an executable blocklist is not
specific enough:

```json
{
  "blocklist_ids": ["123456789012345678"],
  "blocklist_names": ["Demo Game"]
}
```

For a game missing from Discord's database, add a manual mapping:

```json
{
  "overrides": {
    "somegame.exe": {
      "id": "123456789012345678",
      "name": "Some Game"
    }
  }
}
```

## Limitations

- Linux only; the watcher uses `/proc`.
- Windows games running through Wine/Proton are supported; native Linux binaries are not
  scanned, except for the six supported emulators when `emulators_enabled` is true.
- A game must be in Discord's detectable database or have an override. Emulated games
  additionally need a configured Discord application ID for their emulator.
- The game process must expose its `.exe` in a visible command line; unusual wrappers,
  sandboxes, or isolated PID namespaces may not work. Emulators must expose the ROM or
  game path in their command line; an emulator idling in its menu is not reported.
- Discord or the client may choose how many simultaneous activities to display.
- The tool does not add launcher-specific labels or provide true launcher-origin detection.
- Hydra-marker mode can miss games or match another launcher if its process information
  contains the same configured marker.

### Known issues

- **Flatpak Discord / Vesktop**: the sandbox hides `xdg-run/discord-ipc-*` by
  default, so the host watcher finds no socket. Allowlist it, e.g.
  `flatpak override --user --filesystem=xdg-run/discord-ipc-0 <app-id>`, then
  re-run `hydra-rpc --validate-config`. Prefer host-native clients when possible.
- **Multiple arRPC instances**: two servers (e.g. Vesktop + standalone arRPC, or
  two Discord clients) create `discord-ipc-0`, `discord-ipc-1`, ... The watcher
  tries the first `max_socket_attempts` in order; activity may appear in only one
  client. Pin `socket_path` to the client you actually use.
- **Proton / UMU path quirks**: UMU/Proton wrappers expose long `umu-run` or
  `pressure-vessel` command lines; detection still works as long as the trailing
  `Game.exe` is visible. If your launcher isolates the PID namespace or rewrites
  argv, `--status` shows no row — use native launcher mode or check for a wrapper
  update.
- **Hydra-marker false positives/negatives**: markers are substring matches on
  cmdline + environment. A generic marker like `gameid=umu-` matches other
  launchers; a too-specific marker misses Hydra moved to another path. Keep the
  defaults plus one install-unique path, and verify with `--status` (`sources`
  column shows `hydra` when a marker hit).

## Development

Run the dependency-free test suite:

```sh
python3 -m unittest discover --start-directory tests --verbose
# or:
make test
make lint   # syntax + JSON schema/example validation
make check  # test + lint
```

Tests run automatically for pushes and pull requests through GitHub Actions.

## License

[MIT](LICENSE)
