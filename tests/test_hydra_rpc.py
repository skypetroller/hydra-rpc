import json
import os
import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


SCRIPT = Path(__file__).parents[1] / "hydra-rpc"
NAMESPACE = {"__name__": "hydra_rpc_test"}
exec(compile(SCRIPT.read_text(), str(SCRIPT), "exec"), NAMESPACE)


class HydraRpcTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
