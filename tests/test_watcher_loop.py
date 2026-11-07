"""The running watcher must only adopt a database that was really refreshed."""

import contextlib
import io
import signal
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch


SCRIPT = Path(__file__).parents[1] / "hydra-rpc"
NAMESPACE = {"__name__": "hydra_rpc_test_watcher_loop"}
exec(compile(SCRIPT.read_text(), str(SCRIPT), "exec"), NAMESPACE)

OLD_INDEX = {"old.exe": ("1", "Old")}
NEW_INDEX = {"new.exe": ("2", "New")}


class Stop(BaseException):
    """Escapes the loop's `except Exception` once the fake clock runs out."""


class FakeClock:
    def __init__(self, run_for):
        self.now = 1000.0
        self.end = self.now + run_for

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds
        if self.now > self.end:
            raise Stop

    def __getattr__(self, name):
        return getattr(time, name)


class WatcherDatabaseRefreshTests(unittest.TestCase):
    def run_watcher(self, refresh_result):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        refresh_calls = []
        indexes_in_use = []

        def fake_refresh(_cfg, index):
            refresh_calls.append(index)
            return refresh_result

        def fake_choose_games(_exes, _active, _overrides, index, _cfg):
            indexes_in_use.append(index)
            return []

        overrides = {
            "time": FakeClock(run_for=150),
            "CONFIG_PATH": str(Path(directory.name) / "config.json"),
            "SESSION_CACHE": str(Path(directory.name) / "sessions.json"),
            "load_db": lambda _cfg: OLD_INDEX,
            "db_refresh_interval": lambda _cfg, _stale=None: 100,
            "refresh_db_index": fake_refresh,
            "scan_processes": lambda *_args, **_kwargs: {},
            "choose_games": fake_choose_games,
        }
        saved = {s: signal.getsignal(s) for s in (signal.SIGINT, signal.SIGTERM)}
        output = io.StringIO()
        try:
            with patch.dict(NAMESPACE, overrides), contextlib.redirect_stdout(output):
                with self.assertRaises(Stop):
                    NAMESPACE["main"](["--dry-run"])
        finally:
            for number, handler in saved.items():
                signal.signal(number, handler)
        return refresh_calls, indexes_in_use, output.getvalue()

    def test_refreshed_database_replaces_the_index(self):
        calls, indexes, output = self.run_watcher(NEW_INDEX)
        self.assertEqual(len(calls), 1)
        self.assertIs(calls[0], OLD_INDEX)
        self.assertIs(indexes[0], OLD_INDEX)
        self.assertIs(indexes[-1], NEW_INDEX)
        self.assertIn("refreshed game database", output)

    def test_failed_refresh_keeps_the_index_and_says_so(self):
        calls, indexes, output = self.run_watcher(None)
        self.assertEqual(len(calls), 1)
        self.assertTrue(all(index is OLD_INDEX for index in indexes))
        self.assertIn("database refresh failed", output)
        self.assertNotIn("refreshed game database", output)


if __name__ == "__main__":
    unittest.main()
