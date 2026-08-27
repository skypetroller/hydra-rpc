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

    def test_activity_refresh_is_throttled(self):
        activity_due = NAMESPACE["activity_due"]

        self.assertTrue(activity_due(None, 100, 60))
        self.assertFalse(activity_due(100, 159, 60))
        self.assertTrue(activity_due(100, 160, 60))

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
        self.assertEqual(config["blocklist"], NAMESPACE["DEFAULT_BLOCKLIST"])
        self.assertEqual(config["overrides"], {})

    def test_socket_path_expands_environment_variables(self):
        with patch.dict(os.environ, {"XDG_RUNTIME_DIR": "/run/user/4242"}):
            client = NAMESPACE["RPCClient"](
                "/run/user/4242/discord-ipc",
                "${XDG_RUNTIME_DIR}/discord-ipc-3",
            )

        self.assertEqual(client.socket_path, "/run/user/4242/discord-ipc-3")

    def test_socket_candidates_prefer_last_working_path(self):
        client = NAMESPACE["RPCClient"]("/run/user/4242/discord-ipc")
        client.last_path = "/run/user/4242/discord-ipc-3"

        candidates = client.candidate_paths()

        self.assertEqual(candidates[0], "/run/user/4242/discord-ipc-3")
        self.assertEqual(len(candidates), 10)
        self.assertEqual(len(set(candidates)), 10)

    def test_retry_delay_is_bounded_exponential_backoff(self):
        retry_delay = NAMESPACE["retry_delay"]

        self.assertEqual([retry_delay(i) for i in range(1, 7)], [1, 2, 4, 8, 16, 32])
        self.assertEqual(retry_delay(7), 60)
        self.assertEqual(retry_delay(100), 60)


if __name__ == "__main__":
    unittest.main()
