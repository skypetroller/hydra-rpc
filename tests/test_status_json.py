"""Edge-case tests for structured --status JSON reports."""

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch


SCRIPT = Path(__file__).parents[1] / "hydra-rpc"
NAMESPACE = {"__name__": "hydra_rpc_test_status_json"}
exec(compile(SCRIPT.read_text(), str(SCRIPT), "exec"), NAMESPACE)


def make_cfg(**overrides):
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
    cfg["max_socket_attempts"] = 3
    cfg.update(overrides)
    return cfg


CANNED_ENTRIES = [
    {
        "exe": "game.exe",
        "pid": 100,
        "path": "/mnt/game/Game.exe",
        "kind": "wine",
        "matched": True,
        "reason": "matched",
        "app_id": "123",
        "game_name": "Game",
        "display_name": "Game",
        "sources": [],
    },
    {
        "exe": "other.exe",
        "pid": 101,
        "path": "/mnt/other/Other.exe",
        "kind": "wine",
        "matched": False,
        "reason": "unknown: not in database (add an override)",
        "app_id": "",
        "display_name": "",
        "sources": [],
    },
]


class StatusJsonTests(unittest.TestCase):
    def test_report_has_stable_top_level_shape(self):
        cfg = make_cfg(overrides={"game.exe": ("123", "Game")})
        index = {"game.exe": ("123", "Game")}
        with tempfile.TemporaryDirectory() as directory:
            db_cache = Path(directory) / "detectable.json"
            db_cache.write_text("[]")
            past = int(time.time()) - 3600
            os.utime(db_cache, (past, past))
            with (
                patch.dict(NAMESPACE, {
                    "collect_status": lambda _cfg, _index: [dict(e) for e in CANNED_ENTRIES],
                    "probe_sockets": lambda _cfg: {
                        "found": True,
                        "path": "/run/user/1000/discord-ipc-0",
                        "attempts_tried": ["/run/user/1000/discord-ipc-0"],
                        "last_error": "",
                    },
                    "DB_CACHE": str(db_cache),
                }),
            ):
                report = NAMESPACE["build_status_report"](cfg, index)

        self.assertEqual(report["version"], 1)
        self.assertRegex(report["timestamp"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
        for key in ("script", "config_version", "db", "socket", "detected", "skipped", "notes"):
            self.assertIn(key, report)
        self.assertEqual(report["db"]["entries"], 1)
        self.assertEqual(report["db"]["cached_at"], past)
        self.assertGreaterEqual(report["db"]["cached_age_seconds"], 3600)
        self.assertTrue(report["socket"]["found"])
        self.assertEqual(len(report["detected"]), 1)
        self.assertEqual(len(report["skipped"]), 1)
        detected = report["detected"][0]
        self.assertEqual(detected["exe"], "game.exe")
        self.assertEqual(detected["matched_by"], "override")
        self.assertEqual(detected["game_name"], "Game")
        skipped = report["skipped"][0]
        self.assertIn("unknown", skipped["reason"])
        # Fully JSON-serialisable for scripts and bug reports.
        json.dumps(report)

    def test_matched_by_database_vs_override_vs_emulator(self):
        cfg = make_cfg(overrides={"game.exe": ("123", "Game")})
        entries = [
            dict(CANNED_ENTRIES[0], exe="db-game.exe", kind="wine"),
            dict(CANNED_ENTRIES[0], exe="game.exe", kind="wine"),
            dict(CANNED_ENTRIES[0], exe="emulator:retroarch:rom", kind="emulator"),
        ]
        with patch.dict(NAMESPACE, {
            "collect_status": lambda _cfg, _index: entries,
            "probe_sockets": lambda _cfg: {"found": False, "path": "", "attempts_tried": [], "last_error": "no socket"},
        }):
            report = NAMESPACE["build_status_report"](cfg, {})
        by_exe = {d["exe"]: d["matched_by"] for d in report["detected"]}
        self.assertEqual(by_exe["db-game.exe"], "database")
        self.assertEqual(by_exe["game.exe"], "override")
        self.assertEqual(by_exe["emulator:retroarch:rom"], "emulator")

    def test_stale_fallback_adds_note(self):
        cfg = make_cfg()
        with (
            patch.dict(NAMESPACE, {
                "collect_status": lambda _cfg, _index: [],
                "probe_sockets": lambda _cfg: {"found": False, "path": "", "attempts_tried": [], "last_error": ""},
                "_DB_INFO": {"cached_at": None, "fresh": None, "stale_fallback": True},
            }),
        ):
            report = NAMESPACE["build_status_report"](cfg, {})
        self.assertTrue(any("stale" in note for note in report["notes"]))

    def test_print_status_json_is_pure_json_on_stdout(self):
        import io
        cfg = make_cfg()
        with (
            patch.dict(NAMESPACE, {
                "collect_status": lambda _cfg, _index: [],
                "probe_sockets": lambda _cfg: {"found": False, "path": "", "attempts_tried": [], "last_error": ""},
            }),
            patch("sys.stdout", new_callable=io.StringIO) as stdout,
        ):
            NAMESPACE["print_status"](cfg, {}, as_json=True)
        parsed = json.loads(stdout.getvalue())
        self.assertEqual(parsed["version"], 1)


if __name__ == "__main__":
    unittest.main()
