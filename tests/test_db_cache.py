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

    def test_corrupt_stale_cache_does_not_crash(self):
        import urllib.error

        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory) / "detectable.json"
            cache.write_text("{not json")
            old = time.time() - 8 * 24 * 3600
            os.utime(cache, (old, old))
            with patch.dict(NAMESPACE, {"DB_CACHE": str(cache), "CACHE_DIR": directory}):
                with patch.dict(NAMESPACE, {
                    "download_db": lambda _url: (_ for _ in ()).throw(
                        urllib.error.URLError("offline")),
                }):
                    index = NAMESPACE["load_db"](make_cfg())
        self.assertEqual(index, {})

    def test_malformed_download_does_not_replace_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory) / "detectable.json"
            cache.write_text(json.dumps(SAMPLE_DB))
            old = time.time() - 8 * 24 * 3600
            os.utime(cache, (old, old))
            with patch.dict(NAMESPACE, {"DB_CACHE": str(cache), "CACHE_DIR": directory}):
                with patch.dict(NAMESPACE, {"download_db": lambda _url: [1, 2, 3]}):
                    index = NAMESPACE["load_db"](make_cfg())
            cached_after = json.loads(cache.read_text())
        self.assertEqual(index, {"game.exe": ("123", "Game")})
        self.assertEqual(cached_after, SAMPLE_DB)

    def test_empty_download_does_not_replace_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory) / "detectable.json"
            cache.write_text(json.dumps(SAMPLE_DB))
            old = time.time() - 8 * 24 * 3600
            os.utime(cache, (old, old))
            with patch.dict(NAMESPACE, {"DB_CACHE": str(cache), "CACHE_DIR": directory}):
                with patch.dict(NAMESPACE, {"download_db": lambda _url: []}):
                    index = NAMESPACE["load_db"](make_cfg())
                    stale = NAMESPACE["_DB_INFO"]["stale_fallback"]
            cached_after = json.loads(cache.read_text())
        self.assertEqual(index, {"game.exe": ("123", "Game")})
        self.assertEqual(cached_after, SAMPLE_DB)
        self.assertTrue(stale)

    def test_empty_fresh_cache_is_refetched(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory) / "detectable.json"
            cache.write_text(json.dumps([]))
            with patch.dict(NAMESPACE, {"DB_CACHE": str(cache), "CACHE_DIR": directory}):
                with patch.dict(NAMESPACE, {"download_db": lambda _url: SAMPLE_DB}):
                    index = NAMESPACE["load_db"](make_cfg())
            cached_after = json.loads(cache.read_text())
        self.assertEqual(index, {"game.exe": ("123", "Game")})
        self.assertEqual(cached_after, SAMPLE_DB)

    def test_db_refresh_interval_floor_and_stale_retry(self):
        interval = NAMESPACE["db_refresh_interval"]
        self.assertEqual(interval(make_cfg()), 7 * 24 * 3600)
        self.assertEqual(
            interval(make_cfg(db_ttl_seconds=0)),
            NAMESPACE["DB_REFRESH_MIN_SECONDS"],
        )
        self.assertEqual(
            interval(make_cfg(), stale_fallback=True),
            NAMESPACE["DB_REFRESH_MIN_SECONDS"],
        )

    def test_refresh_db_index_keeps_current_on_stale_fallback(self):
        current = {"current.exe": ("1", "Current")}
        messages = []
        previous_info = NAMESPACE["_DB_INFO"]

        def stale_load(_cfg):
            NAMESPACE["_DB_INFO"] = {
                "cached_at": None, "fresh": None, "stale_fallback": True,
            }
            return {"stale.exe": ("2", "Stale")}

        try:
            with patch.dict(NAMESPACE, {
                "load_db": stale_load,
                "log": lambda msg, level="info": messages.append((level, msg)),
            }):
                index = NAMESPACE["refresh_db_index"](make_cfg(), current)
        finally:
            NAMESPACE["_DB_INFO"] = previous_info
        self.assertIs(index, current)
        self.assertTrue(any("refresh failed" in msg for _, msg in messages))
        self.assertFalse(any("refreshed game database" in msg for _, msg in messages))

    def test_refresh_db_index_keeps_current_on_empty_result(self):
        current = {"current.exe": ("1", "Current")}
        messages = []
        previous_info = NAMESPACE["_DB_INFO"]

        def empty_load(_cfg):
            NAMESPACE["_DB_INFO"] = {
                "cached_at": None, "fresh": None, "stale_fallback": False,
            }
            return {}

        try:
            with patch.dict(NAMESPACE, {
                "load_db": empty_load,
                "log": lambda msg, level="info": messages.append((level, msg)),
            }):
                index = NAMESPACE["refresh_db_index"](make_cfg(), current)
        finally:
            NAMESPACE["_DB_INFO"] = previous_info
        self.assertIs(index, current)
        self.assertTrue(any("refresh failed" in msg for _, msg in messages))

    def test_refresh_db_index_accepts_fresh_result(self):
        current = {"current.exe": ("1", "Current")}
        fresh = {"fresh.exe": ("2", "Fresh")}
        messages = []
        previous_info = NAMESPACE["_DB_INFO"]

        def fresh_load(_cfg):
            NAMESPACE["_DB_INFO"] = {
                "cached_at": None, "fresh": True, "stale_fallback": False,
            }
            return fresh

        try:
            with patch.dict(NAMESPACE, {
                "load_db": fresh_load,
                "log": lambda msg, level="info": messages.append((level, msg)),
            }):
                index = NAMESPACE["refresh_db_index"](make_cfg(), current)
        finally:
            NAMESPACE["_DB_INFO"] = previous_info
        self.assertIs(index, fresh)
        self.assertTrue(any("refreshed game database" in msg for _, msg in messages))

    def test_cache_files_are_owner_only(self):
        import stat

        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory) / "detectable.json"
            session = Path(directory) / "sessions.json"
            with patch.dict(NAMESPACE, {
                "DB_CACHE": str(cache),
                "SESSION_CACHE": str(session),
                "CACHE_DIR": directory,
            }):
                with patch.dict(NAMESPACE, {"download_db": lambda _url: SAMPLE_DB}):
                    NAMESPACE["load_db"](make_cfg())
                NAMESPACE["save_sessions"]({
                    "game.exe": {
                        "start_ms": int(time.time() * 1000),
                        "pid": 1,
                        "start_tick": 1,
                    },
                })
            self.assertEqual(stat.S_IMODE(cache.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(session.stat().st_mode), 0o600)

    def test_oversized_download_rejected(self):
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self, n=-1):
                return b"x" * 10

        with patch.dict(NAMESPACE, {"MAX_DB_BYTES": 5}):
            with patch.object(
                NAMESPACE["urllib"].request, "urlopen", return_value=Response()
            ):
                with self.assertRaises(ValueError):
                    NAMESPACE["download_db"]("https://example.invalid/db")

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
