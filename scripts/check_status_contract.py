#!/usr/bin/env python3
"""Hermetic contract test for `hydra-rpc --status --json`.

Runs the real artifact against a throwaway config/cache directory containing a
small stub detectable database, so the check never touches the network and
works offline. Asserts the stable top-level shape so refactors cannot silently
regress it.
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).parents[1]

TOP_LEVEL = {"version", "timestamp", "script", "config_version", "db", "socket",
             "detected", "skipped", "notes"}
DB_KEYS = {"url", "cached_at", "cached_age_seconds", "entries"}
SOCKET_KEYS = {"found", "path", "attempts_tried", "last_error"}
DETECTED_KEYS = {"exe", "app_id", "game_name", "display_name", "pid", "kind",
                 "matched_by", "sources"}
SKIPPED_KEYS = {"exe", "pid", "kind", "reason", "app_id", "sources"}

STUB_DB = [{
    "id": "123456789012345678",
    "name": "Contract Stub Game",
    "executables": [{"name": "stub-game.exe", "os": "win32", "is_launcher": False}],
}]


def fail(message):
    print(f"status-contract FAILED: {message}", file=sys.stderr)
    return 1


def main():
    with tempfile.TemporaryDirectory(prefix="hydra-rpc-contract-") as tmp:
        cache_dir = Path(tmp) / "cache" / "hydra-rpc"
        config_dir = Path(tmp) / "config" / "hydra-rpc"
        cache_dir.mkdir(parents=True)
        config_dir.mkdir(parents=True)
        (cache_dir / "detectable.json").write_text(json.dumps(STUB_DB))
        env = dict(
            os.environ,
            XDG_CACHE_HOME=str(Path(tmp) / "cache"),
            XDG_CONFIG_HOME=str(Path(tmp) / "config"),
        )
        proc = subprocess.run(
            [sys.executable, str(ROOT / "hydra-rpc"), "--status", "--json"],
            capture_output=True, text=True, timeout=120, env=env,
        )
    if proc.returncode != 0:
        return fail(f"exit={proc.returncode} stderr={proc.stderr.strip()[-500:]}")
    try:
        report = json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        return fail(f"stdout is not pure JSON: {e}")
    if set(report) != TOP_LEVEL:
        return fail(f"top-level keys {sorted(report)} != {sorted(TOP_LEVEL)}")
    if report["version"] != 1:
        return fail(f"version={report['version']} != 1")
    if set(report["db"]) != DB_KEYS:
        return fail(f"db keys {sorted(report['db'])}")
    if report["db"]["entries"] != len(STUB_DB):
        return fail(f"stub database not used: entries={report['db']['entries']}")
    if set(report["socket"]) != SOCKET_KEYS:
        return fail("socket keys mismatch")
    for entry in report["detected"]:
        if set(entry) != DETECTED_KEYS:
            return fail(f"detected entry keys {sorted(entry)}")
        if entry["matched_by"] not in ("database", "override", "emulator"):
            return fail(f"bad matched_by {entry['matched_by']!r}")
    for entry in report["skipped"]:
        if set(entry) != SKIPPED_KEYS:
            return fail(f"skipped entry keys {sorted(entry)}")
    print(
        f"status-contract OK: version=1 db_entries={report['db']['entries']} "
        f"detected={len(report['detected'])} skipped={len(report['skipped'])} "
        f"socket_found={report['socket']['found']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
