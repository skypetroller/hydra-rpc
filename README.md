# hydra-rpc

Show `Playing <Game>` in Discord for Windows games running on Linux through Hydra,
Proton, Wine, or another compatible launcher.

## Features

- Detects Windows games running through Proton, Proton GE, UMU-Proton, and Wine.
- Works with Hydra, Heroic, Lutris, Bottles, Steam, and similar launchers.
- Can publish multiple detected game activities at once.
- Saves session start times and process identity so a watcher restart can preserve elapsed
  time without confusing a reused PID with the old game.
- Supports custom activity names and optional Discord fields such as `details`, `state`,
  `assets`, and `buttons`.
- Supports blocklists by executable, Discord application ID, or game name.
- Includes `--dry-run` and `--validate-config` troubleshooting commands.
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

The easiest installation uses `/usr/local/bin`, which matches the included startup files:

```sh
git clone https://github.com/skypetroller/hydra-rpc.git
cd hydra-rpc
sudo install -Dm755 hydra-rpc /usr/local/bin/hydra-rpc
```

If you do not want to use `sudo`, install it in your home directory instead:

```sh
mkdir -p ~/.local/bin
install -Dm755 hydra-rpc ~/.local/bin/hydra-rpc
```

The included `.desktop` and systemd files assume `/usr/local/bin/hydra-rpc`. If you use
`~/.local/bin`, change their `Exec=` or `ExecStart=` line to
`/home/your-user/.local/bin/hydra-rpc`.

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

Preview detection without sending anything to Discord:

```sh
hydra-rpc --dry-run
```

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
| `poll_seconds`             | `5`                             | How often to scan running processes              |
| `activity_refresh_seconds` | `60`                            | How often to refresh active activities            |
| `max_activities`           | `0` (all)                       | Use `1`-`10` to limit reported games              |
| `db_url`                   | Discord endpoint                | Source of the executable database                 |
| `db_ttl_seconds`           | `604800` (7 days)               | How long the database cache stays fresh           |
| `socket_dir`               | `$XDG_RUNTIME_DIR`              | Directory used for automatic socket discovery     |
| `socket_path`              | Automatic                       | Exact arRPC socket path                           |
| `max_socket_attempts`      | `3`                             | Maximum automatic socket paths tried (1-10)       |
| `hydra_only`               | `false`                         | Enable best-effort Hydra-marker filtering        |
| `hydra_markers`            | Hydra path markers              | Markers used by Hydra-marker mode                |
| `blocklist`                | Wine service processes          | Executable names never reported                   |
| `blocklist_ids`            | `[]`                            | Discord application IDs never reported            |
| `blocklist_names`          | `[]`                            | Case-insensitive game names never reported        |
| `activity_template`        | `"{game_name}"`                | Template for the activity name                   |
| `rich_activity`             | `{}`                            | Additional Discord activity fields               |
| `log_file`                 | `""`                            | Optional log file                                |
| `log_level`                | `"info"`                        | `debug`, `info`, `warning`, or `error`            |
| `overrides`                | `{}`                            | Manual executable-to-application mappings         |

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
  scanned.
- A game must be in Discord's detectable database or have an override.
- The game process must expose its `.exe` in a visible command line; unusual wrappers,
  sandboxes, or isolated PID namespaces may not work.
- Discord or the client may choose how many simultaneous activities to display.
- The tool does not add launcher-specific labels or provide true launcher-origin detection.
- Hydra-marker mode can miss games or match another launcher if its process information
  contains the same configured marker.

## Development

Run the dependency-free test suite:

```sh
python3 -m unittest discover --start-directory tests --verbose
```

Tests run automatically for pushes and pull requests through GitHub Actions.

## License

[MIT](LICENSE)
