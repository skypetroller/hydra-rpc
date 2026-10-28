import json
import os
import re
import struct
import tempfile
import unittest
from io import BytesIO, StringIO
from pathlib import Path
from unittest.mock import patch


SCRIPT = Path(__file__).parents[1] / "hydra-rpc"
NAMESPACE = {"__name__": "hydra_rpc_test"}
exec(compile(SCRIPT.read_text(), str(SCRIPT), "exec"), NAMESPACE)


class HydraRpcTests(unittest.TestCase):
    def setUp(self):
        # Marker results are cached process-wide in production; keep tests
        # isolated because they reuse fake PIDs with different fake environs.
        NAMESPACE["_MARKER_RESULT_CACHE"].clear()

    def test_ipc_frame_encoding(self):
        frame = NAMESPACE["encode_frame"](1, {"cmd": "SET_ACTIVITY"})
        msg_type, size = struct.unpack("<II", frame[:8])

        self.assertEqual(msg_type, 1)
        self.assertEqual(size, len(frame) - 8)
        self.assertEqual(json.loads(frame[8:]), {"cmd": "SET_ACTIVITY"})

    def test_database_index_normalises_paths_and_skips_launchers(self):
        index = NAMESPACE["build_index"]([
            {
                "id": "123",
                "name": "Example",
                "executables": [
                    {"name": "launcher.exe", "is_launcher": True},
                    {"name": r"Example\\Example.exe", "is_launcher": False},
                ],
            }
        ])

        self.assertEqual(index, {"example.exe": ("123", "Example")})

    def test_active_game_is_preferred(self):
        exes = {"alpha.exe": 10, "beta.exe": 20}
        index = {
            "alpha.exe": ("1", "Alpha"),
            "beta.exe": ("2", "Beta"),
        }

        choose_game = NAMESPACE["choose_game"]
        self.assertEqual(choose_game(exes, None, {}, index)[0], "alpha.exe")
        self.assertEqual(choose_game(exes, "beta.exe", {}, index)[0], "beta.exe")

    def test_choose_games_supports_multiple_activities(self):
        exes = {
            "alpha.exe": {"pid": 10, "path": "Alpha.exe"},
            "beta.exe": {"pid": 20, "path": "Beta.exe"},
        }
        index = {
            "alpha.exe": ("1", "Alpha"),
            "beta.exe": ("2", "Beta"),
        }
        cfg = dict(NAMESPACE["DEFAULT_CONFIG"])
        cfg["blocklist_ids"] = set()
        cfg["blocklist_names"] = set()
        cfg["rich_activity"] = {}

        choose_games = NAMESPACE["choose_games"]
        games = choose_games(exes, [], {}, index, cfg)

        self.assertEqual([game["exe"] for game in games], ["alpha.exe", "beta.exe"])

        cfg["max_activities"] = 1
        self.assertEqual(len(choose_games(exes, [], {}, index, cfg)), 1)

        games_without_closed = choose_games(exes, ["closed.exe"], {}, index, cfg)
        self.assertEqual([game["exe"] for game in games_without_closed], ["alpha.exe"])

    def test_detect_emulators_ignores_plain_iso_mounts(self):
        real_open = open
        procs = {
            "100": {
                "cmdline": b"mount\0/roms/game.iso\0",
                "environ": b"",
            },
            "101": {
                "cmdline": b"/usr/bin/duckstation-qt\0/roms/Game.chd\0",
                "environ": b"",
            },
        }

        def fake_open(path, *args, **kwargs):
            match = re.match(r"/proc/(\d+)/(cmdline|environ)$", str(path))
            if not match:
                return real_open(path, *args, **kwargs)
            return BytesIO(procs.get(match.group(1), {}).get(match.group(2), b""))

        with (
            patch("os.listdir", return_value=["100", "101"]),
            patch("builtins.open", side_effect=fake_open),
        ):
            found = NAMESPACE["detect_emulators"](set())

        self.assertEqual(set(found), {"emulator:duckstation:/roms/game.chd"})
        self.assertEqual(found["emulator:duckstation:/roms/game.chd"]["rom_name"], "Game")

    def test_shared_scan_covers_wine_and_emulators_in_one_pass(self):
        real_open = open
        procs = {
            "100": {
                "cmdline": b"/usr/bin/wine\0/unix\0/mnt/game/Game.exe\0",
                "environ": b"GAMEID=umu-1\0",
            },
            "101": {
                "cmdline": b"/usr/bin/duckstation-qt\0/roms/Game.chd\0",
                "environ": b"",
            },
        }
        cmdline_opens = []

        def fake_open(path, *args, **kwargs):
            match = re.match(r"/proc/(\d+)/(cmdline|environ)$", str(path))
            if not match:
                return real_open(path, *args, **kwargs)
            if match.group(2) == "cmdline":
                cmdline_opens.append(match.group(1))
            return BytesIO(procs.get(match.group(1), {}).get(match.group(2), b""))

        with (
            patch("os.listdir", return_value=["100", "101"]),
            patch("builtins.open", side_effect=fake_open),
        ):
            found = NAMESPACE["scan_processes"](set(), set(), True)

        self.assertEqual(
            set(found), {"game.exe", "emulator:duckstation:/roms/game.chd"}
        )
        # each process cmdline read exactly once despite both detectors running
        self.assertEqual(sorted(cmdline_opens), ["100", "101"])
        self.assertEqual(found["game.exe"]["pid"], 100)

    def test_hydra_marking_skipped_unless_requested(self):
        real_open = open
        procs = {
            "100": {
                "cmdline": b"/usr/bin/wine\0/unix\0/mnt/game/Game.exe\0",
                "environ": b"GAMEID=umu-1\0",
            },
        }
        environ_opens = []

        def fake_open(path, *args, **kwargs):
            match = re.match(r"/proc/(\d+)/(cmdline|environ)$", str(path))
            if not match:
                return real_open(path, *args, **kwargs)
            if match.group(2) == "environ":
                environ_opens.append(match.group(1))
            return BytesIO(procs.get(match.group(1), {}).get(match.group(2), b""))

        markers = {"gameid=umu-1"}
        with (
            patch("os.listdir", return_value=["100"]),
            patch("builtins.open", side_effect=fake_open),
        ):
            unmarked = NAMESPACE["scan_processes"](set(), markers, False, False)

        self.assertEqual(unmarked["game.exe"]["sources"], set())
        self.assertEqual(environ_opens, [])

        with (
            patch("os.listdir", return_value=["100"]),
            patch("builtins.open", side_effect=fake_open),
        ):
            marked = NAMESPACE["scan_processes"](set(), markers, False, True)

        self.assertEqual(marked["game.exe"]["sources"], {"hydra"})

    def test_blocklisted_exe_skips_hydra_environ_read(self):
        # Wine service processes are dropped by the blocklist; marking them
        # first would waste an environ read on every poll.
        real_open = open
        procs = {
            "100": {
                "cmdline": (
                    b"/usr/bin/wine\0/unix\0/mnt/prefix/drive_c/windows/"
                    b"system32/services.exe\0"
                ),
                "environ": b"GAMEID=umu-1\0",
            },
        }
        environ_opens = []

        def fake_open(path, *args, **kwargs):
            match = re.match(r"/proc/(\d+)/(cmdline|environ)$", str(path))
            if not match:
                return real_open(path, *args, **kwargs)
            if match.group(2) == "environ":
                environ_opens.append(match.group(1))
            return BytesIO(procs.get(match.group(1), {}).get(match.group(2), b""))

        stats = {}
        with (
            patch("os.listdir", return_value=["100"]),
            patch("builtins.open", side_effect=fake_open),
        ):
            found = NAMESPACE["scan_processes"](
                {"services.exe"}, {"gameid=umu-1"}, False, True, stats,
            )

        self.assertEqual(found, {})
        self.assertEqual(environ_opens, [])
        self.assertEqual(stats.get("environ_reads", 0), 0)

    def test_unrelated_flatpak_process_skips_argv_decode(self):
        # Only sandbox wrappers whose raw cmdline mentions an emulator token
        # are decoded; other Flatpak apps must be skipped cheaply.
        real_open = open
        procs = {
            "100": {
                "cmdline": b"flatpak\0run\0org.example.Notes\0/roms/notes.iso\0",
                "environ": b"",
            },
            "101": {
                "cmdline": b"flatpak\0run\0org.libretro.RetroArch\0/roms/game.sfc\0",
                "environ": b"",
            },
        }

        def fake_open(path, *args, **kwargs):
            match = re.match(r"/proc/(\d+)/(cmdline|environ)$", str(path))
            if not match:
                return real_open(path, *args, **kwargs)
            return BytesIO(procs.get(match.group(1), {}).get(match.group(2), b""))

        decode_calls = []
        original_decode = NAMESPACE["_decode_parts"]

        def spy(parts):
            decode_calls.append(parts)
            return original_decode(parts)

        with (
            patch("os.listdir", return_value=["100", "101"]),
            patch("builtins.open", side_effect=fake_open),
            patch.dict(NAMESPACE, {"_decode_parts": spy}),
        ):
            found = NAMESPACE["scan_processes"](set(), set(), True, mark_hydra=False)

        self.assertIn("emulator:retroarch:/roms/game.sfc", found)
        self.assertEqual(len(decode_calls), 1)

    def test_rendered_activities_are_cached(self):
        cfg = dict(NAMESPACE["DEFAULT_CONFIG"])
        cfg["blocklist_ids"] = set()
        cfg["blocklist_names"] = set()
        cfg["rich_activity"] = {"details": "Playing {game_name}"}
        NAMESPACE["_ACTIVITY_CACHE"].clear()

        first = NAMESPACE["resolve_game"](
            "example.exe",
            {"pid": 42, "path": "Example.exe"},
            {},
            {"example.exe": ("123", "Example")},
            cfg,
        )
        second = NAMESPACE["resolve_game"](
            "example.exe",
            {"pid": 42, "path": "Example.exe"},
            {},
            {"example.exe": ("123", "Example")},
            cfg,
        )

        self.assertEqual(first["activity"], second["activity"])
        second["activity"]["details"] = "MUTATED"
        second["activity"]["assets"] = {"large_image": "MUTATED"}
        third = NAMESPACE["resolve_game"](
            "example.exe",
            {"pid": 42, "path": "Example.exe"},
            {},
            {"example.exe": ("123", "Example")},
            cfg,
        )
        self.assertEqual(third["activity"]["details"], "Playing Example")
        self.assertNotIn("assets", third["activity"])
        self.assertEqual(len(NAMESPACE["_ACTIVITY_CACHE"]), 1)

    def test_activity_cache_evicts_least_recently_used(self):
        cfg = dict(NAMESPACE["DEFAULT_CONFIG"])
        cfg["blocklist_ids"] = set()
        cfg["blocklist_names"] = set()
        cfg["rich_activity"] = {}
        NAMESPACE["_ACTIVITY_CACHE"].clear()
        with patch.dict(NAMESPACE, {"_ACTIVITY_CACHE_MAX": 2}):
            for name in ("aaa", "bbb", "ccc"):
                NAMESPACE["resolve_game"](
                    f"{name}.exe",
                    {"pid": 42, "path": f"{name}.exe"},
                    {},
                    {f"{name}.exe": ("1", name.upper())},
                    cfg,
                )
        keys = [key[1] for key in NAMESPACE["_ACTIVITY_CACHE"]]
        self.assertEqual(keys, ["bbb.exe", "ccc.exe"])

    def test_poll_seconds_has_a_one_second_floor(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.json"
            config_path.write_text(json.dumps({"poll_seconds": 0.1}))

            old_path = NAMESPACE["CONFIG_PATH"]
            NAMESPACE["CONFIG_PATH"] = str(config_path)
            try:
                config = NAMESPACE["load_config"]()
            finally:
                NAMESPACE["CONFIG_PATH"] = old_path

        self.assertEqual(config["poll_seconds"], 5)

    def test_emulator_fast_path_skips_unrelated_processes(self):
        real_open = open
        procs = {
            "100": {
                "cmdline": b"mount\0/roms/game.iso\0",
                "environ": b"",
            },
            "101": {
                "cmdline": b"/usr/bin/launch-retroarch.sh\0/roms/Game.sfc\0",
                "environ": b"",
            },
            "102": {
                "cmdline": b"flatpak\0run\0org.libretro.RetroArch\0/roms/Other.sfc\0",
                "environ": b"",
            },
        }

        def fake_open(path, *args, **kwargs):
            match = re.match(r"/proc/(\d+)/(cmdline|environ)$", str(path))
            if not match:
                return real_open(path, *args, **kwargs)
            return BytesIO(procs.get(match.group(1), {}).get(match.group(2), b""))

        with (
            patch("os.listdir", return_value=["100", "101", "102"]),
            patch("builtins.open", side_effect=fake_open),
        ):
            found = NAMESPACE["scan_processes"](set(), set(), True)

        keys = sorted(found)
        self.assertNotIn("mount", "".join(keys))
        self.assertIn("emulator:retroarch:/roms/game.sfc", keys)
        self.assertIn("emulator:retroarch:/roms/other.sfc", keys)

    def test_templates_and_rich_activity_are_generic(self):
        cfg = dict(NAMESPACE["DEFAULT_CONFIG"])
        cfg["blocklist_ids"] = set()
        cfg["blocklist_names"] = set()
        cfg["activity_template"] = "{game_name} [{exe}]"
        cfg["rich_activity"] = {
            "details": "Playing {game_name}",
            "state": "Executable: {exe}",
            "assets": {"large_image": "cover"},
        }
        game = NAMESPACE["resolve_game"](
            "example.exe",
            {"pid": 42, "path": "Example.exe"},
            {},
            {"example.exe": ("123", "Example")},
            cfg,
        )

        self.assertEqual(game["display_name"], "Example [example.exe]")
        self.assertEqual(game["activity"]["details"], "Playing Example")
        self.assertEqual(game["activity"]["state"], "Executable: example.exe")
        self.assertEqual(game["activity"]["assets"], {"large_image": "cover"})

    def test_emulator_rom_paths_and_names(self):
        extract = NAMESPACE["extract_emulator_rom"]
        clean = NAMESPACE["clean_rom_name"]

        retroarch_path = extract(
            "retroarch",
            ["retroarch", "-L", "/cores/snes9x_libretro.so", "/roms/Super_Mario_World.sfc"],
        )
        pcsx2_path = extract("pcsx2", ["pcsx2-qt", "/roms/Final Fantasy X.iso"])
        rpcs3_path = extract(
            "rpcs3",
            ["rpcs3", "/games/BLUS12345/PS3_GAME/USRDIR/EBOOT.BIN"],
        )
        dolphin_path = extract(
            "dolphin", ["dolphin-emu", "/roms/Super Mario Sunshine.rvz"]
        )
        ppsspp_path = extract("ppsspp", ["ppsspp", "/roms/Crisis Core.iso"])

        self.assertEqual(retroarch_path, "/roms/Super_Mario_World.sfc")
        self.assertEqual(pcsx2_path, "/roms/Final Fantasy X.iso")
        self.assertEqual(clean("retroarch", retroarch_path), "Super Mario World")
        self.assertEqual(clean("pcsx2", pcsx2_path), "Final Fantasy X")
        self.assertEqual(clean("rpcs3", rpcs3_path), "BLUS12345")
        self.assertEqual(dolphin_path, "/roms/Super Mario Sunshine.rvz")
        self.assertEqual(clean("dolphin", dolphin_path), "Super Mario Sunshine")
        self.assertEqual(ppsspp_path, "/roms/Crisis Core.iso")
        self.assertEqual(clean("ppsspp", ppsspp_path), "Crisis Core")

    def test_emulator_activity_uses_emulator_app_and_rom_details(self):
        cfg = dict(NAMESPACE["DEFAULT_CONFIG"])
        cfg["blocklist_ids"] = set()
        cfg["blocklist_names"] = set()
        cfg["rich_activity"] = {}
        info = {
            "kind": "emulator",
            "emulator": "retroarch",
            "rom_name": "Super Mario World",
            "rom_path": "/roms/Super Mario World.sfc",
            "pid": 42,
            "sources": set(),
        }

        game = NAMESPACE["resolve_game"]("emulator:retroarch:rom", info, {}, {}, cfg)

        self.assertEqual(game["app_id"], "505497615748694018")
        self.assertEqual(game["display_name"], "RetroArch")
        self.assertEqual(game["activity"]["details"], "Super Mario World")

    def test_dolphin_uses_its_discord_app_with_rom_details(self):
        cfg = dict(NAMESPACE["DEFAULT_CONFIG"])
        cfg["blocklist_ids"] = set()
        cfg["blocklist_names"] = set()
        cfg["rich_activity"] = {}
        info = {
            "kind": "emulator",
            "emulator": "dolphin",
            "rom_name": "Super Mario Sunshine",
            "rom_path": "/roms/Super Mario Sunshine.rvz",
            "pid": 42,
            "sources": set(),
        }

        game = NAMESPACE["resolve_game"]("emulator:dolphin:rom", info, {}, {}, cfg)

        self.assertEqual(game["app_id"], "356943187589201930")
        self.assertEqual(game["display_name"], "Dolphin")
        self.assertEqual(game["activity"]["details"], "Super Mario Sunshine")

    def test_ppsspp_without_an_application_id_is_skipped(self):
        cfg = dict(NAMESPACE["DEFAULT_CONFIG"])
        cfg["blocklist_ids"] = set()
        cfg["blocklist_names"] = set()
        cfg["rich_activity"] = {}
        info = {
            "kind": "emulator",
            "emulator": "ppsspp",
            "rom_name": "Crisis Core",
            "rom_path": "/roms/Crisis Core.iso",
            "pid": 42,
            "sources": set(),
        }

        self.assertIsNone(NAMESPACE["resolve_game"]("emulator:ppsspp:rom", info, {}, {}, cfg))

    def test_emulator_override_name_takes_precedence(self):
        cfg = dict(NAMESPACE["DEFAULT_CONFIG"])
        cfg["blocklist_ids"] = set()
        cfg["blocklist_names"] = set()
        cfg["rich_activity"] = {}
        cfg["emulator_overrides"] = {
            "retroarch:super mario world": {"id": "999", "name": "Super Mario World"}
        }
        info = {
            "kind": "emulator",
            "emulator": "retroarch",
            "rom_name": "Super Mario World",
            "rom_path": "/roms/Super Mario World.sfc",
            "pid": 42,
            "sources": set(),
        }

        game = NAMESPACE["resolve_game"]("emulator:retroarch:rom", info, {}, {}, cfg)

        self.assertEqual(game["app_id"], "999")
        self.assertEqual(game["display_name"], "Super Mario World")

    def test_emulators_without_an_application_id_are_skipped(self):
        cfg = dict(NAMESPACE["DEFAULT_CONFIG"])
        cfg["blocklist_ids"] = set()
        cfg["blocklist_names"] = set()
        cfg["rich_activity"] = {}
        info = {
            "kind": "emulator",
            "emulator": "rpcs3",
            "rom_name": "Game",
            "rom_path": "/games/Game",
            "pid": 42,
            "sources": set(),
        }

        self.assertIsNone(NAMESPACE["resolve_game"]("emulator:rpcs3:game", info, {}, {}, cfg))

    def test_emulator_respects_hydra_only_and_blocklists(self):
        cfg = dict(NAMESPACE["DEFAULT_CONFIG"])
        cfg["blocklist_ids"] = set()
        cfg["blocklist_names"] = set()
        cfg["rich_activity"] = {}
        cfg["hydra_only"] = True
        info = {
            "kind": "emulator",
            "emulator": "retroarch",
            "rom_name": "Super Mario World",
            "rom_path": "/roms/Super Mario World.sfc",
            "pid": 42,
            "sources": set(),
        }

        self.assertIsNone(
            NAMESPACE["resolve_game"]("emulator:retroarch:rom", info, {}, {}, cfg)
        )

        info["sources"] = {"hydra"}
        self.assertIsNotNone(
            NAMESPACE["resolve_game"]("emulator:retroarch:rom", info, {}, {}, cfg)
        )

        cfg["hydra_only"] = False
        cfg["blocklist_names"] = {"super mario world"}
        self.assertIsNone(
            NAMESPACE["resolve_game"]("emulator:retroarch:rom", info, {}, {}, cfg)
        )

    def test_game_blocklists_are_applied_after_mapping(self):
        cfg = dict(NAMESPACE["DEFAULT_CONFIG"])
        cfg["blocklist_ids"] = {"123"}
        cfg["blocklist_names"] = set()
        cfg["rich_activity"] = {}
        resolve_game = NAMESPACE["resolve_game"]
        args = ("example.exe", {"pid": 42, "path": "Example.exe"}, {}, {"example.exe": ("123", "Example")}, cfg)

        self.assertIsNone(resolve_game(*args))

        cfg["blocklist_ids"] = set()
        cfg["blocklist_names"] = {"example"}
        self.assertIsNone(resolve_game(*args))

    def test_session_start_reuses_matching_pid(self):
        start_ms = 123456789
        sessions = {"example.exe": {"start_ms": start_ms, "pid": 42, "start_tick": 99}}
        game = {"exe": "example.exe", "pid": 42}

        with patch.dict(NAMESPACE, {"get_process_start_tick": lambda _pid: 99}):
            self.assertEqual(NAMESPACE["session_start"](sessions, game), start_ms)

    def test_session_start_rejects_reused_pid(self):
        old_start_ms = 123456789
        sessions = {"example.exe": {"start_ms": old_start_ms, "pid": 42, "start_tick": 99}}
        game = {"exe": "example.exe", "pid": 42}

        with patch.dict(NAMESPACE, {"get_process_start_tick": lambda _pid: 100}):
            new_start_ms = NAMESPACE["session_start"](sessions, game)

        self.assertGreater(new_start_ms, old_start_ms)

    def test_process_start_tick_is_available_for_current_process(self):
        tick = NAMESPACE["get_process_start_tick"](os.getpid())

        self.assertIsInstance(tick, int)
        self.assertGreater(tick, 0)

    def test_cli_modes_parse(self):
        parse_args = NAMESPACE["parse_args"]
        self.assertTrue(parse_args(["--dry-run"]).dry_run)
        self.assertTrue(parse_args(["--validate-config"]).validate_config)
        self.assertTrue(parse_args(["--check-update"]).check_update)
        self.assertTrue(parse_args(["--update"]).update)

    def test_update_source_validation_and_atomic_install(self):
        validate_update_source = NAMESPACE["validate_update_source"]
        install_update = NAMESPACE["install_update"]
        source = SCRIPT.read_bytes()

        self.assertEqual(validate_update_source(source), source)
        with self.assertRaises(ValueError):
            validate_update_source(b"not a Python script")

        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "hydra-rpc"
            target.write_bytes(b"old")
            target.chmod(0o750)
            install_update(str(target), source)
            self.assertEqual(target.read_bytes(), source)
            self.assertEqual(target.stat().st_mode & 0o777, 0o750)

    def test_file_logging_writes_a_line(self):
        with tempfile.TemporaryDirectory() as directory:
            log_path = Path(directory) / "hydra-rpc.log"
            old_level = NAMESPACE["CURRENT_LOG_LEVEL"]
            try:
                NAMESPACE["configure_logging"]({
                    "log_file": str(log_path),
                    "log_level": "info",
                })
                NAMESPACE["log"]("test log entry")
            finally:
                NAMESPACE["close_logging"]()
                NAMESPACE["CURRENT_LOG_LEVEL"] = old_level

            self.assertIn("test log entry", log_path.read_text())

    def test_config_invalid_values_fall_back_safely(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.json"
            config_path.write_text(json.dumps({
                "poll_seconds": "invalid",
                "activity_refresh_seconds": "invalid",
                "db_ttl_seconds": -1,
                "db_url": 123,
                "socket_dir": 123,
                "socket_path": [],
                "max_socket_attempts": 0,
                "hydra_only": "yes",
                "hydra_markers": "not-a-list",
                "emulators_enabled": "yes",
                "emulator_application_ids": "not-a-map",
                "emulator_activity_template": "",
                "blocklist": "not-a-list",
                "overrides": {"broken.exe": {"id": ""}},
            }))

            old_path = NAMESPACE["CONFIG_PATH"]
            NAMESPACE["CONFIG_PATH"] = str(config_path)
            try:
                config = NAMESPACE["load_config"]()
            finally:
                NAMESPACE["CONFIG_PATH"] = old_path

        self.assertEqual(config["poll_seconds"], 5)
        self.assertEqual(config["activity_refresh_seconds"], 60)
        self.assertEqual(config["db_ttl_seconds"], 604800)
        self.assertEqual(config["db_url"], NAMESPACE["DB_URL"])
        self.assertEqual(config["socket_dir"], "")
        self.assertEqual(config["socket_path"], "")
        self.assertEqual(config["max_socket_attempts"], 3)
        self.assertFalse(config["hydra_only"])
        self.assertEqual(config["hydra_markers"], NAMESPACE["DEFAULT_HYDRA_MARKERS"])
        self.assertFalse(config["emulators_enabled"])
        self.assertEqual(
            config["emulator_application_ids"],
            NAMESPACE["DEFAULT_EMULATOR_APPLICATION_IDS"],
        )
        self.assertEqual(config["emulator_activity_template"], "{emulator_name}")
        self.assertEqual(config["blocklist"], NAMESPACE["DEFAULT_BLOCKLIST"])
        self.assertEqual(config["overrides"], {})

    def test_hydra_only_requires_a_hydra_process_marker(self):
        self.assertNotIn("gameid=umu-", NAMESPACE["DEFAULT_HYDRA_MARKERS"])
        cfg = dict(NAMESPACE["DEFAULT_CONFIG"])
        cfg["hydra_only"] = True
        cfg["blocklist_ids"] = set()
        cfg["blocklist_names"] = set()
        cfg["rich_activity"] = {}
        resolve_game = NAMESPACE["resolve_game"]
        index = {"example.exe": ("123", "Example")}

        self.assertIsNone(resolve_game(
            "example.exe",
            {"pid": 42, "path": "Example.exe", "sources": set()},
            {},
            index,
            cfg,
        ))
        self.assertIsNotNone(resolve_game(
            "example.exe",
            {"pid": 42, "path": "Example.exe", "sources": {"hydra"}},
            {},
            index,
            cfg,
        ))

    def test_socket_path_expands_environment_variables(self):
        with patch.dict(os.environ, {"XDG_RUNTIME_DIR": "/run/user/4242"}):
            client = NAMESPACE["RPCClient"](
                "/run/user/4242/discord-ipc",
                "${XDG_RUNTIME_DIR}/discord-ipc-3",
            )

        self.assertEqual(client.socket_path, "/run/user/4242/discord-ipc-3")

    def test_socket_candidates_prefer_last_working_path(self):
        client = NAMESPACE["RPCClient"]("/run/user/4242/discord-ipc", max_socket_attempts=4)
        client.last_path = "/run/user/4242/discord-ipc-9"

        candidates = client.candidate_paths()

        self.assertEqual(candidates[0], "/run/user/4242/discord-ipc-9")
        self.assertEqual(len(candidates), 4)
        self.assertEqual(len(set(candidates)), 4)

    def test_retry_delay_is_bounded_exponential_backoff(self):
        retry_delay = NAMESPACE["retry_delay"]

        self.assertEqual([retry_delay(i) for i in range(1, 7)], [1, 2, 4, 8, 16, 32])
        self.assertEqual(retry_delay(7), 60)
        self.assertEqual(retry_delay(100), 60)

    def test_ipc_frame_size_limit(self):
        read_frame = NAMESPACE["read_frame"]

        class FakeSocket:
            def settimeout(self, timeout):
                pass

            def recv(self, n):
                raise AssertionError("must not read payload of oversized frame")

        oversized = struct.pack("<II", 1, NAMESPACE["MAX_IPC_FRAME_SIZE"] + 1)

        class HeaderSocket(FakeSocket):
            def __init__(self):
                self.sent = False

            def recv(self, n):
                if not self.sent:
                    self.sent = True
                    return oversized[:n]
                raise AssertionError("must not read payload of oversized frame")

        with self.assertRaises(ValueError):
            read_frame(HeaderSocket(), 1)

    def test_hydra_marker_split_across_environ_chunks(self):
        real_open = open
        marker = b"/opt/hydra/"
        # Split the marker across an 8-byte chunk boundary: b"/opt/hyd" | b"ra/".
        environ = b"HOME=/root\x00PATH=/opt/hyd" + b"ra/bin\x00"

        def fake_open(path, *args, **kwargs):
            match = re.match(r"/proc/(\d+)/(cmdline|environ)$", str(path))
            if not match:
                return real_open(path, *args, **kwargs)
            if match.group(2) == "cmdline":
                return BytesIO(b"/usr/bin/wine\0/unix\0/mnt/game/Game.exe\0")
            return BytesIO(environ)

        with (
            patch("os.listdir", return_value=["100"]),
            patch("builtins.open", side_effect=fake_open),
            patch.dict(NAMESPACE, {"_ENVIRON_CHUNK_BYTES": 8}),
        ):
            self.assertTrue(
                NAMESPACE["has_hydra_marker"]("100", [], {marker.decode()})
            )

    def test_hydra_marker_result_is_cached_for_same_start_tick(self):
        real_open = open
        environ_opens = []

        def fake_open(path, *args, **kwargs):
            if str(path).endswith("/environ"):
                environ_opens.append(str(path))
                return BytesIO(b"HOME=/root\x00GAMEID=umu-1\x00")
            return real_open(path, *args, **kwargs)

        stats = {}
        with (
            patch("builtins.open", side_effect=fake_open),
            patch.dict(NAMESPACE, {"get_process_start_tick": lambda _pid: 424242}),
        ):
            first = NAMESPACE["has_hydra_marker"]("777", [], {"gameid=umu-1"}, stats)
            second = NAMESPACE["has_hydra_marker"]("777", [], {"gameid=umu-1"}, stats)

        self.assertTrue(first)
        self.assertTrue(second)
        self.assertEqual(len(environ_opens), 1)
        self.assertEqual(stats.get("environ_reads"), 1)
        self.assertEqual(stats.get("environ_cache_hits"), 1)

    def test_hydra_marker_unreadable_environ_is_not_cached(self):
        environ_opens = []

        def fake_open(path, *args, **kwargs):
            environ_opens.append(str(path))
            raise OSError("permission denied")

        stats = {}
        with (
            patch("builtins.open", side_effect=fake_open),
            patch.dict(NAMESPACE, {"get_process_start_tick": lambda _pid: 424243}),
        ):
            for _ in range(2):
                self.assertFalse(
                    NAMESPACE["has_hydra_marker"]("778", [], {"gameid=umu-1"}, stats)
                )

        self.assertEqual(len(environ_opens), 2)
        self.assertEqual(stats.get("environ_reads", 0), 0)
        self.assertEqual(stats.get("environ_cache_hits", 0), 0)

    def test_log_enabled_respects_current_level(self):
        levels = NAMESPACE["LOG_LEVELS"]
        with patch.dict(NAMESPACE, {"CURRENT_LOG_LEVEL": levels["info"]}):
            self.assertFalse(NAMESPACE["log_enabled"]("debug"))
            self.assertTrue(NAMESPACE["log_enabled"]("info"))
            self.assertTrue(NAMESPACE["log_enabled"]("error"))

    def test_emulator_fast_path_classification(self):
        real_open = open
        procs = {
            # Wrapper script whose basename contains an emulator token: intended match.
            "100": {
                "cmdline": b"/usr/bin/my-dolphin-wrapper\0/roms/game.iso\0",
                "environ": b"",
            },
            # Unrelated process mentioning retroarch but with no ROM path: skip.
            "101": {
                "cmdline": b"notes-app\0retroarch-settings.json\0",
                "environ": b"",
            },
            # Flatpak wrapper: full scan, emulator token in arguments.
            "102": {
                "cmdline": b"flatpak\0run\0org.libretro.RetroArch\0/roms/game.sfc\0",
                "environ": b"",
            },
            # Plain mount of an ISO: no emulator binary involved.
            "103": {
                "cmdline": b"mount\0/roms/game.iso\0",
                "environ": b"",
            },
        }

        def fake_open(path, *args, **kwargs):
            match = re.match(r"/proc/(\d+)/(cmdline|environ)$", str(path))
            if not match:
                return real_open(path, *args, **kwargs)
            return BytesIO(procs.get(match.group(1), {}).get(match.group(2), b""))

        with (
            patch("os.listdir", return_value=["100", "101", "102", "103"]),
            patch("builtins.open", side_effect=fake_open),
        ):
            found = NAMESPACE["scan_processes"](set(), set(), True, mark_hydra=False)

        keys = sorted(found)
        self.assertIn("emulator:dolphin:/roms/game.iso", keys)
        self.assertIn("emulator:retroarch:/roms/game.sfc", keys)
        self.assertEqual(len(keys), 2)

    def test_cache_key_covers_every_rendered_value(self):
        cfg = dict(NAMESPACE["DEFAULT_CONFIG"])
        cfg["blocklist_ids"] = set()
        cfg["blocklist_names"] = set()
        cfg["rich_activity"] = {}
        NAMESPACE["_ACTIVITY_CACHE"].clear()
        resolve_game = NAMESPACE["resolve_game"]
        args = (
            "example.exe",
            {"pid": 42, "path": "Example.exe"},
            {},
            {"example.exe": ("123", "Example")},
            cfg,
        )

        baseline = resolve_game(*args)["activity"]
        mutations = {
            "other template": dict(cfg, activity_template="{game_name}!"),
            "other rich fields": dict(cfg, rich_activity={"details": "x"}),
            "other app id": None,  # handled via index override below
            "other game name": None,
            "other exe": None,
        }
        # Each mutated config must recompute its fingerprint, not reuse the
        # baseline's cached one (in production the config is fixed per run).
        for mutated_cfg in mutations.values():
            if isinstance(mutated_cfg, dict):
                mutated_cfg.pop("_rich_json", None)
        self.assertEqual(
            resolve_game(*args)["activity"], baseline,
            "identical inputs must hit the cache",
        )

        mutated = resolve_game(
            "example.exe",
            {"pid": 42, "path": "Example.exe"},
            {},
            {"example.exe": ("123", "Example")},
            mutations["other template"],
        )
        self.assertNotEqual(mutated["activity"], baseline)

        mutated = resolve_game(
            "example.exe",
            {"pid": 42, "path": "Example.exe"},
            {},
            {"example.exe": ("123", "Example")},
            mutations["other rich fields"],
        )
        self.assertNotEqual(mutated["activity"], baseline)

        mutated = resolve_game(
            "example.exe",
            {"pid": 42, "path": "Example.exe"},
            {"example.exe": ("999", "Example")},
            {"example.exe": ("123", "Example")},
            cfg,
        )
        self.assertNotEqual(mutated["activity"], baseline)

        mutated = resolve_game(
            "example.exe",
            {"pid": 42, "path": "Example.exe"},
            {},
            {"example.exe": ("123", "Renamed")},
            cfg,
        )
        self.assertNotEqual(mutated["activity"], baseline)

        mutated = resolve_game(
            "other.exe",
            {"pid": 42, "path": "Other.exe"},
            {},
            {"other.exe": ("123", "Example")},
            dict(cfg, activity_template="{game_name} [{exe}]"),
        )
        self.assertNotEqual(mutated["activity"], baseline)

    def test_should_send_activity(self):
        should_send_activity = NAMESPACE["should_send_activity"]
        payload = {"application_id": "1", "name": "Game"}

        self.assertTrue(should_send_activity(None, payload, False))
        self.assertTrue(should_send_activity(payload, payload, True))
        self.assertTrue(should_send_activity({"name": "Other"}, payload, False))
        self.assertFalse(should_send_activity(dict(payload), payload, False))

    def test_ping_probe(self):
        import socket as stdlib_socket
        import threading

        RPCClient = NAMESPACE["RPCClient"]
        encode_frame = NAMESPACE["encode_frame"]
        read_frame = NAMESPACE["read_frame"]

        mine, theirs = stdlib_socket.socketpair()

        def serve_once():
            try:
                header = b""
                while len(header) < 8:
                    chunk = theirs.recv(8 - len(header))
                    if not chunk:
                        return
                    header += chunk
                msg_type, size = struct.unpack("<II", header)
                data = b""
                while len(data) < size:
                    chunk = theirs.recv(size - len(data))
                    if not chunk:
                        return
                    data += chunk
                # Echo back as PONG, like arRPC does.
                theirs.sendall(encode_frame(NAMESPACE["IPC_PONG"], json.loads(data)))
            finally:
                theirs.close()

        server = threading.Thread(target=serve_once)
        server.start()
        try:
            client = RPCClient.__new__(RPCClient)
            client.sock = mine
            client.nonce = 0
            self.assertTrue(client.ping())
        finally:
            mine.close()
            server.join(timeout=5)

        dead_reader, dead_writer = stdlib_socket.socketpair()
        dead_writer.close()
        try:
            dead_client = RPCClient.__new__(RPCClient)
            dead_client.sock = dead_reader
            dead_client.nonce = 0
            self.assertFalse(dead_client.ping())
        finally:
            dead_reader.close()

    def test_poll_seconds_minimum_is_one_second(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.json"
            config_path.write_text(json.dumps({"poll_seconds": 0.1}))

            old_path = NAMESPACE["CONFIG_PATH"]
            NAMESPACE["CONFIG_PATH"] = str(config_path)
            try:
                config = NAMESPACE["load_config"]()
            finally:
                NAMESPACE["CONFIG_PATH"] = old_path

        self.assertEqual(config["poll_seconds"], 5)

    def test_save_prunes_stale_and_caps_entries(self):
        real_open = open
        now_ms = 1_700_000_000_000
        sessions = {
            "old.exe": {"start_ms": now_ms - 8 * 24 * 3600 * 1000, "pid": 1, "start_tick": 1},
            "good.exe": {"start_ms": now_ms, "pid": 2, "start_tick": 2},
            "broken.exe": {"nope": True},
        }
        written = {}

        class FakeFile(StringIO):
            def __init__(self, path):
                self._path = path
                super().__init__()

            def close(self):
                written[self._path] = self.getvalue()
                super().close()

        def fake_open(path, *args, **kwargs):
            if str(path).endswith(".tmp") or "sessions.json" in str(path):
                return FakeFile(str(path))
            return real_open(path, *args, **kwargs)

        with (
            patch("builtins.open", side_effect=fake_open),
            patch("os.makedirs"),
            patch("os.replace"),
            patch.object(NAMESPACE["time"], "time", return_value=now_ms / 1000),
        ):
            NAMESPACE["save_sessions"](sessions)

        saved = json.loads(next(iter(written.values())))
        self.assertEqual(set(saved), {"good.exe"})

    def test_pid_disappearance_and_malformed_cmdlines(self):
        real_open = open

        def fake_open(path, *args, **kwargs):
            match = re.match(r"/proc/(\d+)/(cmdline|environ)$", str(path))
            if not match:
                return real_open(path, *args, **kwargs)
            pid = match.group(1)
            if pid == "101":
                raise OSError("process exited")
            if match.group(2) == "environ":
                raise OSError("unreadable")
            if pid == "102":
                return BytesIO(b"")
            if pid == "103":
                return BytesIO(b"\0\0\0")
            return BytesIO(b"/usr/bin/wine\0/unix\0/mnt/game/Game.exe\0")

        with (
            patch("os.listdir", return_value=["100", "101", "102", "103"]),
            patch("builtins.open", side_effect=fake_open),
        ):
            found = NAMESPACE["scan_processes"](set(), set(), False, False)

        self.assertEqual(set(found), {"game.exe"})

    def test_is_valid_app_id(self):
        is_valid = NAMESPACE["is_valid_app_id"]
        self.assertTrue(is_valid(""))
        self.assertTrue(is_valid("123456789012345678"))
        self.assertTrue(is_valid(123))
        self.assertFalse(is_valid("abc"))
        self.assertFalse(is_valid("123abc"))
        self.assertFalse(is_valid(None))

    def test_unknown_config_keys_are_ignored(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.json"
            config_path.write_text(json.dumps({"unknown_key_xyz": 1, "poll_seconds": 5}))
            old_path = NAMESPACE["CONFIG_PATH"]
            NAMESPACE["CONFIG_PATH"] = str(config_path)
            try:
                config = NAMESPACE["load_config"]()
            finally:
                NAMESPACE["CONFIG_PATH"] = old_path
        self.assertEqual(config["poll_seconds"], 5)
        self.assertNotIn("unknown_key_xyz", config)

    def test_invalid_app_ids_warn_but_load(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.json"
            config_path.write_text(json.dumps({
                "emulator_application_ids": {"retroarch": "not-digits"},
                "overrides": {"bad.exe": {"id": "nope", "name": "Bad"}},
                "blocklist_ids": ["xyz"],
            }))
            old_path = NAMESPACE["CONFIG_PATH"]
            NAMESPACE["CONFIG_PATH"] = str(config_path)
            try:
                config = NAMESPACE["load_config"]()
            finally:
                NAMESPACE["CONFIG_PATH"] = old_path
        self.assertEqual(config["emulator_application_ids"]["retroarch"], "not-digits")
        self.assertIn("bad.exe", config["overrides"])
        self.assertIn("xyz", config["blocklist_ids"])

    def test_missing_socket_path_warns(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.json"
            config_path.write_text(json.dumps({"socket_path": "/nonexistent/discord-ipc-0"}))
            old_path = NAMESPACE["CONFIG_PATH"]
            NAMESPACE["CONFIG_PATH"] = str(config_path)
            try:
                config = NAMESPACE["load_config"]()
            finally:
                NAMESPACE["CONFIG_PATH"] = old_path
        self.assertEqual(config["socket_path"], "/nonexistent/discord-ipc-0")

    def test_cli_status_and_json_parse(self):
        parse_args = NAMESPACE["parse_args"]
        args = parse_args(["--status"])
        self.assertTrue(args.status)
        self.assertFalse(args.json)
        args = parse_args(["--status", "--json"])
        self.assertTrue(args.status)
        self.assertTrue(args.json)

    def test_describe_unmatched_reasons(self):
        describe = NAMESPACE["describe_unmatched"]
        cfg = dict(NAMESPACE["DEFAULT_CONFIG"])
        cfg["blocklist_ids"] = set()
        cfg["blocklist_names"] = set()
        cfg["hydra_only"] = False
        cfg["emulator_application_ids"] = dict(NAMESPACE["DEFAULT_EMULATOR_APPLICATION_IDS"])
        cfg["emulator_overrides"] = {}
        # unknown exe
        reason, app_id = describe("unknown.exe", {"pid": 1, "path": "x", "sources": set()}, {}, {}, cfg)
        self.assertTrue(reason.startswith("unknown"))
        self.assertEqual(app_id, "")
        # blocklisted app id
        cfg2 = dict(cfg, blocklist_ids={"123"})
        reason, app_id = describe(
            "example.exe", {"pid": 1, "path": "x", "sources": set()},
            {}, {"example.exe": ("123", "Example")}, cfg2,
        )
        self.assertEqual(reason, "blocklisted-app-id")
        # hydra filtered
        cfg3 = dict(cfg, hydra_only=True)
        reason, _ = describe(
            "example.exe", {"pid": 1, "path": "x", "sources": set()},
            {}, {"example.exe": ("123", "Example")}, cfg3,
        )
        self.assertTrue(reason.startswith("hydra-filtered"))
        # missing emulator app id
        info = {"kind": "emulator", "emulator": "ppsspp", "rom_name": "Game",
                "rom_path": "/g.iso", "pid": 1, "sources": set()}
        reason, _ = describe("emulator:ppsspp:/g.iso", info, {}, {}, cfg)
        self.assertTrue(reason.startswith("missing-emulator-app-id"))

    def test_collect_status_and_table(self):
        real_open = open
        procs = {
            "100": {
                "cmdline": b"/usr/bin/wine\0/unix\0/mnt/game/Game.exe\0",
                "environ": b"",
            },
            "101": {
                "cmdline": b"/usr/bin/wine\0/unix\0/mnt/other/Other.exe\0",
                "environ": b"",
            },
        }

        def fake_open(path, *args, **kwargs):
            match = re.match(r"/proc/(\d+)/(cmdline|environ)$", str(path))
            if not match:
                return real_open(path, *args, **kwargs)
            return BytesIO(procs.get(match.group(1), {}).get(match.group(2), b""))

        cfg = dict(NAMESPACE["DEFAULT_CONFIG"])
        cfg["blocklist"] = set()
        cfg["blocklist_ids"] = set()
        cfg["blocklist_names"] = set()
        cfg["hydra_only"] = False
        cfg["hydra_markers"] = set()
        cfg["emulators_enabled"] = False
        cfg["overrides"] = {}
        cfg["emulator_overrides"] = {}
        cfg["emulator_application_ids"] = dict(NAMESPACE["DEFAULT_EMULATOR_APPLICATION_IDS"])
        cfg["activity_template"] = "{game_name}"
        cfg["emulator_activity_template"] = "{emulator_name}"
        cfg["rich_activity"] = {}
        index = {"game.exe": ("123", "Game")}
        with (
            patch("os.listdir", return_value=["100", "101"]),
            patch("builtins.open", side_effect=fake_open),
        ):
            entries = NAMESPACE["collect_status"](cfg, index)
        by_exe = {e["exe"]: e for e in entries}
        self.assertTrue(by_exe["game.exe"]["matched"])
        self.assertEqual(by_exe["game.exe"]["app_id"], "123")
        self.assertFalse(by_exe["other.exe"]["matched"])
        self.assertIn("unknown", by_exe["other.exe"]["reason"])
        table = NAMESPACE["format_status_table"](entries)
        self.assertIn("game.exe", table)
        self.assertIn("1/2 matched", table)
        # JSON-serialisable
        json.dumps(entries)
        self.assertEqual(
            NAMESPACE["format_status_table"]([]),
            "status: no wine .exe or emulator ROM processes detected",
        )

    def test_collect_status_surfaces_exe_blocklist(self):
        real_open = open
        procs = {
            "100": {
                "cmdline": b"/usr/bin/wine\0/unix\0/mnt/game/Game.exe\0",
                "environ": b"",
            },
        }

        def fake_open(path, *args, **kwargs):
            match = re.match(r"/proc/(\d+)/(cmdline|environ)$", str(path))
            if not match:
                return real_open(path, *args, **kwargs)
            return BytesIO(procs.get(match.group(1), {}).get(match.group(2), b""))

        cfg = dict(NAMESPACE["DEFAULT_CONFIG"])
        cfg["blocklist"] = {"game.exe"}
        cfg["blocklist_ids"] = set()
        cfg["blocklist_names"] = set()
        cfg["hydra_only"] = False
        cfg["hydra_markers"] = set()
        cfg["emulators_enabled"] = False
        cfg["overrides"] = {}
        cfg["emulator_overrides"] = {}
        cfg["emulator_application_ids"] = dict(NAMESPACE["DEFAULT_EMULATOR_APPLICATION_IDS"])
        cfg["activity_template"] = "{game_name}"
        cfg["emulator_activity_template"] = "{emulator_name}"
        cfg["rich_activity"] = {}
        with (
            patch("os.listdir", return_value=["100"]),
            patch("builtins.open", side_effect=fake_open),
        ):
            entries = NAMESPACE["collect_status"](cfg, {"game.exe": ("123", "Game")})
        self.assertEqual(len(entries), 1)
        self.assertFalse(entries[0]["matched"])
        self.assertEqual(entries[0]["reason"], "blocklisted-exe")

    def test_validate_config_rejects_bad_socket_and_ids(self):
        cfg = dict(NAMESPACE["DEFAULT_CONFIG"])
        cfg["blocklist_ids"] = set()
        cfg["blocklist_names"] = set()
        cfg["hydra_markers"] = set()
        cfg["emulators_enabled"] = False
        cfg["emulator_application_ids"] = dict(NAMESPACE["DEFAULT_EMULATOR_APPLICATION_IDS"])
        cfg["overrides"] = {"bad.exe": ("not-digits", "Bad")}
        cfg["socket_path"] = "/nonexistent/discord-ipc-0"
        cfg["max_socket_attempts"] = 3
        with patch.object(NAMESPACE["RPCClient"], "_connect", side_effect=OSError("no socket")):
            self.assertFalse(NAMESPACE["validate_config"](cfg, {"a.exe": ("1", "A")}))


if __name__ == "__main__":
    unittest.main()
