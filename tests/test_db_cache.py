"""Edge-case tests for DB cache age diagnostics and stale fallback."""

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch


SCRIPT = Path(__file__).parents[1] / "hydra-rpc"
NAMESPACE = {"__name__": "hydra_rpc_test_db_cache"}
exec(compile(SCRIPT.read_text(), str(SCRIPT), "exec"), NAMESPACE)

SAMPLE_DB = [
    {"id": "123", "name": "Game", "executables": [{"name": "Game.exe"}]},
]


def make_cfg(**overrides):
    cfg = dict(NAMESPACE["DEFAULT_CONFIG"])
    cfg["db_url"] = "https://example.invalid/detectable"
    cfg["db_ttl_seconds"] = 7 * 24 * 3600
    cfg.update(overrides)
    return cfg


class DbCacheTests(unittest.TestCase):
    def test_fresh_cache_avoids_download(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory) / "detectable.json"
            cache.write_text(json.dumps(SAMPLE_DB))
            with patch.dict(NAMESPACE, {"DB_CACHE": str(cache), "CACHE_DIR": directory}):
                with patch.dict(NAMESPACE, {
                    "download_db": lambda _url: (_ for _ in ()).throw(AssertionError("must not download")),
                }):
                    index = NAMESPACE["load_db"](make_cfg())
        self.assertEqual(index, {"game.exe": ("123", "Game")})

    def test_expired_cache_triggers_download(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory) / "detectable.json"
            cache.write_text(json.dumps([]))
            old = time.time() - 8 * 24 * 3600
            os.utime(cache, (old, old))
            with patch.dict(NAMESPACE, {"DB_CACHE": str(cache), "CACHE_DIR": directory}):
                with patch.dict(NAMESPACE, {"download_db": lambda _url: SAMPLE_DB}):
                    index = NAMESPACE["load_db"](make_cfg())
        self.assertEqual(index, {"game.exe": ("123", "Game")})

    def test_download_failure_uses_stale_cache(self):
        import urllib.error

        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory) / "detectable.json"
            cache.write_text(json.dumps(SAMPLE_DB))
            old = time.time() - 8 * 24 * 3600
            os.utime(cache, (old, old))
            with patch.dict(NAMESPACE, {"DB_CACHE": str(cache), "CACHE_DIR": directory}):
                with patch.dict(NAMESPACE, {
                    "download_db": lambda _url: (_ for _ in ()).throw(
                        urllib.error.URLError("offline")),
                }):
                    index = NAMESPACE["load_db"](make_cfg())
                    # NOTE: capture inside the block — patch.dict restores the
                    # whole namespace dict on exit, discarding global rebinding.
                    stale = NAMESPACE["_DB_INFO"]["stale_fallback"]
        self.assertEqual(index, {"game.exe": ("123", "Game")})
        self.assertTrue(stale)

    def test_format_age(self):
        fmt = NAMESPACE["format_age"]
        self.assertEqual(fmt(5), "5s")
        self.assertEqual(fmt(90), "1m")
        self.assertEqual(fmt(7200), "2h")
        self.assertEqual(fmt(3 * 24 * 3600), "3d")

    def test_prune_sessions_with_mixed_types_and_over_cap(self):
        # Regression: sort key must not compare str/int when a legacy or
        # hand-edited cache mixes types and exceeds MAX_SESSION_ENTRIES.
        now_ms = 1_700_000_000_000
        sessions = {
            f"a{i}.exe": {"start_ms": now_ms - 1000, "pid": i, "start_tick": i}
            for i in range(300)
        }
        sessions["str.exe"] = {
            "start_ms": str(now_ms - 500), "pid": 9, "start_tick": 9,
        }
        pruned = NAMESPACE["prune_sessions"](sessions, now_ms)
        self.assertEqual(len(pruned), NAMESPACE["MAX_SESSION_ENTRIES"])
        self.assertIn("str.exe", pruned)

    def test_max_activities_limits_reported_games(self):
        cfg = make_cfg(max_activities=1)
        cfg["blocklist_ids"] = set()
        cfg["blocklist_names"] = set()
        cfg["rich_activity"] = {}
        exes = {
            "alpha.exe": {"pid": 10, "path": "Alpha.exe", "sources": set()},
            "beta.exe": {"pid": 20, "path": "Beta.exe", "sources": set()},
        }
        index = {"alpha.exe": ("1", "Alpha"), "beta.exe": ("2", "Beta")}
        games = NAMESPACE["choose_games"](exes, [], {}, index, cfg)
        self.assertEqual(len(games), 1)
        cfg["max_activities"] = 0
        self.assertEqual(len(NAMESPACE["choose_games"](exes, [], {}, index, cfg)), 2)


if __name__ == "__main__":
    unittest.main()
