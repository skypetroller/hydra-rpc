"""Edge-case tests for config migration and versioning."""

import json
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "hydra-rpc"
NAMESPACE = {"__name__": "hydra_rpc_test_migration"}
exec(compile(SCRIPT.read_text(), str(SCRIPT), "exec"), NAMESPACE)


def load_from(user_dict):
    with tempfile.TemporaryDirectory() as directory:
        config_path = Path(directory) / "config.json"
        config_path.write_text(json.dumps(user_dict))
        old_path = NAMESPACE["CONFIG_PATH"]
        NAMESPACE["CONFIG_PATH"] = str(config_path)
        try:
            return NAMESPACE["load_config"]()
        finally:
            NAMESPACE["CONFIG_PATH"] = old_path


class ConfigMigrationTests(unittest.TestCase):
    def test_legacy_config_without_version_gets_current(self):
        cfg = load_from({"poll_seconds": 5})
        self.assertEqual(cfg["config_version"], NAMESPACE["CONFIG_VERSION"])
        # New keys fall back to defaults.
        self.assertEqual(cfg["max_activities"], 0)

    def test_current_config_keeps_version(self):
        cfg = load_from({"config_version": NAMESPACE["CONFIG_VERSION"]})
        self.assertEqual(cfg["config_version"], NAMESPACE["CONFIG_VERSION"])

    def test_invalid_version_is_repaired(self):
        cfg = load_from({"config_version": "not-a-number"})
        self.assertEqual(cfg["config_version"], NAMESPACE["CONFIG_VERSION"])

    def test_migrate_config_is_callable_stub(self):
        cfg = {"config_version": 0}
        result = NAMESPACE["migrate_config"](cfg, 0, 1)
        self.assertIs(result, cfg)

    def test_ensure_config_writes_version(self):
        import os as real_os

        written = {}
        real_open = open

        def fake_open(path, *args, **kwargs):
            if str(path).endswith("config.json"):
                class Capture:
                    def __enter__(self):
                        return self

                    def __exit__(self, *exc):
                        return False

                    def write(self, text):
                        written["text"] = written.get("text", "") + text

                return Capture()
            return real_open(path, *args, **kwargs)

        from unittest.mock import patch

        with (
            patch.dict(NAMESPACE, {"CONFIG_PATH": "/nonexistent-dir-xyz/config.json"}),
            patch("os.path.exists", return_value=False),
            patch("os.makedirs"),
            patch("builtins.open", side_effect=fake_open),
        ):
            NAMESPACE["ensure_config"](dict(NAMESPACE["DEFAULT_CONFIG"]))
        self.assertIn('"config_version"', written.get("text", ""))


if __name__ == "__main__":
    unittest.main()
