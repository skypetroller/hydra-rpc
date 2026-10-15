"""Edge-case tests for socket discovery and the hardened updater."""

import hashlib
import io
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch


SCRIPT = Path(__file__).parents[1] / "hydra-rpc"
NAMESPACE = {"__name__": "hydra_rpc_test_updater_socket"}
exec(compile(SCRIPT.read_text(), str(SCRIPT), "exec"), NAMESPACE)

SOURCE = NAMESPACE["validate_update_source"](Path(
    __file__).parents[1].joinpath("hydra-rpc").read_bytes())


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._payload


def urlopen_for(source, sha_body=None, sha_error=None):
    def fake_urlopen(req, *args, **kwargs):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        if url.endswith(".sha256"):
            if sha_error is not None:
                raise sha_error
            return FakeResponse(sha_body)
        return FakeResponse(source)

    return fake_urlopen


class UpdaterChecksumTests(unittest.TestCase):
    def test_matching_checksum_passes(self):
        digest = hashlib.sha256(SOURCE).hexdigest()
        with patch.object(
            NAMESPACE["urllib"].request, "urlopen",
            side_effect=urlopen_for(SOURCE, f"{digest}  hydra-rpc\n".encode()),
        ):
            self.assertEqual(NAMESPACE["fetch_update_source"](), SOURCE)

    def test_bare_hex_checksum_passes(self):
        digest = hashlib.sha256(SOURCE).hexdigest()
        with patch.object(
            NAMESPACE["urllib"].request, "urlopen",
            side_effect=urlopen_for(SOURCE, digest.encode()),
        ):
            self.assertEqual(NAMESPACE["fetch_update_source"](), SOURCE)

    def test_mismatch_raises(self):
        with patch.object(
            NAMESPACE["urllib"].request, "urlopen",
            side_effect=urlopen_for(SOURCE, ("0" * 64).encode()),
        ):
            with self.assertRaises(ValueError):
                NAMESPACE["fetch_update_source"]()

    def test_missing_checksum_falls_back_to_syntax_check(self):
        with patch.object(
            NAMESPACE["urllib"].request, "urlopen",
            side_effect=urlopen_for(
                SOURCE,
                sha_error=urllib.error.HTTPError(
                    "url", 404, "not found", {}, io.BytesIO(b"")),
            ),
        ):
            self.assertEqual(NAMESPACE["fetch_update_source"](), SOURCE)

    def test_malformed_checksum_body_is_ignored(self):
        with patch.object(
            NAMESPACE["urllib"].request, "urlopen",
            side_effect=urlopen_for(SOURCE, b"not-a-checksum"),
        ):
            self.assertEqual(NAMESPACE["fetch_update_source"](), SOURCE)


class SocketDiscoveryTests(unittest.TestCase):
    def test_candidate_paths_with_explicit_socket(self):
        client = NAMESPACE["RPCClient"]("/base/discord-ipc", "/tmp/custom-ipc-0")
        self.assertEqual(client.candidate_paths(), ["/tmp/custom-ipc-0"])

    def test_candidate_paths_multiple_attempts(self):
        client = NAMESPACE["RPCClient"]("/base/discord-ipc", max_socket_attempts=3)
        self.assertEqual(client.candidate_paths(), [
            "/base/discord-ipc-0",
            "/base/discord-ipc-1",
            "/base/discord-ipc-2",
        ])

    def test_connect_failure_raises(self):
        client = NAMESPACE["RPCClient"]("/base/discord-ipc", max_socket_attempts=1)
        with patch("socket.socket") as mock_socket:
            mock_socket.return_value.connect.side_effect = OSError("no socket")
            with self.assertRaises(OSError):
                client.connect("123")

    def test_probe_sockets_reports_failure(self):
        cfg = dict(NAMESPACE["DEFAULT_CONFIG"])
        cfg["socket_path"] = ""
        cfg["socket_dir"] = "/nonexistent-dir-xyz"
        cfg["max_socket_attempts"] = 2
        with patch.object(NAMESPACE["RPCClient"], "_connect", side_effect=OSError("refused")):
            info = NAMESPACE["probe_sockets"](cfg)
        self.assertFalse(info["found"])
        self.assertEqual(info["path"], "")
        self.assertEqual(len(info["attempts_tried"]), 2)
        self.assertIn("refused", info["last_error"])

    def test_probe_sockets_reports_success(self):
        cfg = dict(NAMESPACE["DEFAULT_CONFIG"])
        cfg["socket_path"] = ""
        cfg["socket_dir"] = "/run/user/1000"
        cfg["max_socket_attempts"] = 3

        class Dummy:
            def close(self):
                pass

        with patch.object(NAMESPACE["RPCClient"], "_connect", return_value=Dummy()):
            info = NAMESPACE["probe_sockets"](cfg)
        self.assertTrue(info["found"])
        self.assertTrue(info["path"].endswith("discord-ipc-0"))


if __name__ == "__main__":
    unittest.main()
