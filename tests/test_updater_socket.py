"""Edge-case tests for socket discovery, the hardened updater, and the IPC ping."""

import base64
import hashlib
import io
import os
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch


SCRIPT = Path(__file__).parents[1] / "hydra-rpc"
NAMESPACE = {"__name__": "hydra_rpc_test_updater_socket"}
exec(compile(SCRIPT.read_text(), str(SCRIPT), "exec"), NAMESPACE)

SOURCE = NAMESPACE["validate_update_source"](Path(
    __file__).parents[1].joinpath("hydra-rpc").read_bytes())

SIG_404 = urllib.error.HTTPError("url", 404, "not found", {}, io.BytesIO(b""))


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self, n=-1):
        if n < 0 or n >= len(self._payload):
            return self._payload
        return self._payload[:n]


def urlopen_for(source, sha_body=None, sha_error=None, sig_body=None, sig_error=SIG_404):
    def fake_urlopen(req, *args, **kwargs):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        if url.endswith(".sha256.sig"):
            if sig_body is not None:
                return FakeResponse(sig_body)
            if sig_error is not None:
                raise sig_error
            return FakeResponse(sig_body)
        if url.endswith(".sha256"):
            if sha_error is not None:
                raise sha_error
            return FakeResponse(sha_body)
        return FakeResponse(source)

    return fake_urlopen


class UpdaterChecksumTests(unittest.TestCase):
    def test_matching_checksum_parses(self):
        digest = hashlib.sha256(SOURCE).hexdigest()
        self.assertEqual(
            NAMESPACE["checksum_from_body"](f"{digest}  hydra-rpc\n".encode()),
            digest,
        )

    def test_bare_hex_checksum_parses(self):
        digest = hashlib.sha256(SOURCE).hexdigest()
        self.assertEqual(
            NAMESPACE["checksum_from_body"](digest.encode()), digest)

    def test_invalid_checksum_bodies_rejected(self):
        for body in (b"", b"not-a-checksum", b"0" * 63, b"g" * 64):
            self.assertIsNone(NAMESPACE["checksum_from_body"](body))

    def test_read_limited_enforces_ceiling(self):
        read_limited = NAMESPACE["read_limited"]
        self.assertEqual(read_limited(FakeResponse(b"abcde"), 5, "test"), b"abcde")
        with self.assertRaises(ValueError):
            read_limited(FakeResponse(b"abcdef"), 5, "test")

    def test_oversized_update_source_rejected(self):
        with patch.dict(NAMESPACE, {"MAX_UPDATE_BYTES": 16}):
            with patch.object(
                NAMESPACE["urllib"].request, "urlopen",
                side_effect=urlopen_for(SOURCE, b"0" * 64),
            ):
                with self.assertRaises(ValueError) as ctx:
                    NAMESPACE["fetch_update_source"]()
        self.assertIn("16-byte limit", str(ctx.exception))


class UpdaterSignatureTests(unittest.TestCase):
    # Fixed seed (RFC 8032 TEST 2) as a deterministic test key.
    SEED = bytes.fromhex(
        "4ccd089b28ff96da9db6c346ec114e0f5b8a319f35aba624da8cf6ed4fb8a6fb")

    def setUp(self):
        public = NAMESPACE["ed25519_public_key"](self.SEED)
        keys = patch.dict(NAMESPACE, {"UPDATE_PUBLIC_KEYS": (public.hex(),)})
        keys.start()
        self.addCleanup(keys.stop)
        self.checksum_body = f"{hashlib.sha256(SOURCE).hexdigest()}  hydra-rpc\n".encode()
        self.signature_body = self._sign(self.checksum_body, self.SEED)

    def _sign(self, body, seed):
        signature = NAMESPACE["ed25519_sign"](body, seed)
        return b"untrusted comment: test\n" + base64.b64encode(signature) + b"\n"

    def _fetch(self, **kwargs):
        kwargs.setdefault("sha_body", self.checksum_body)
        if "sig_body" not in kwargs and "sig_error" not in kwargs:
            kwargs["sig_body"] = self.signature_body
        with patch.object(
            NAMESPACE["urllib"].request, "urlopen",
            side_effect=urlopen_for(SOURCE, **kwargs),
        ):
            return NAMESPACE["fetch_update_source"]()

    def test_signed_release_accepted(self):
        self.assertEqual(self._fetch(), SOURCE)

    def test_tampered_checksum_rejected(self):
        tampered = f"{'0' * 64}  hydra-rpc\n".encode()
        with self.assertRaises(ValueError) as ctx:
            self._fetch(sha_body=tampered)
        self.assertIn("signature", str(ctx.exception))

    def test_tampered_signature_rejected(self):
        raw = bytearray(base64.b64decode(self.signature_body.splitlines()[1]))
        raw[5] ^= 1
        bad_body = b"untrusted comment: test\n" + base64.b64encode(bytes(raw)) + b"\n"
        with self.assertRaises(ValueError) as ctx:
            self._fetch(sig_body=bad_body)
        self.assertIn("signature", str(ctx.exception))

    def test_wrong_key_rejected(self):
        other_seed = os.urandom(32)
        with self.assertRaises(ValueError):
            self._fetch(sig_body=self._sign(self.checksum_body, other_seed))

    def test_malformed_signature_rejected(self):
        with self.assertRaises(ValueError):
            self._fetch(sig_body=b"untrusted comment: test\nnot base64!!!\n")
        with self.assertRaises(ValueError):
            self._fetch(sig_body=b"untrusted comment: test\n")

    def test_signature_without_checksum_rejected(self):
        with self.assertRaises(ValueError):
            self._fetch(sha_error=SIG_404)

    def test_malformed_checksum_rejected(self):
        body = b"not-a-checksum"
        with self.assertRaises(ValueError) as ctx:
            self._fetch(sha_body=body, sig_body=self._sign(body, self.SEED))
        self.assertIn("malformed", str(ctx.exception))

    def test_signed_but_wrong_checksum_rejected(self):
        body = f"{'0' * 64}  hydra-rpc\n".encode()
        with self.assertRaises(ValueError) as ctx:
            self._fetch(sha_body=body, sig_body=self._sign(body, self.SEED))
        self.assertIn("checksum mismatch", str(ctx.exception))

    def test_unpublished_signature_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            self._fetch(sig_error=SIG_404)
        self.assertIn("not signed", str(ctx.exception))

    def test_unsigned_and_unchecksummed_rejected(self):
        with self.assertRaises(ValueError):
            self._fetch(sha_error=SIG_404, sig_error=SIG_404)


class PingProbeTests(unittest.TestCase):
    class FakeSocket:
        def __init__(self):
            self.sent = []

        def sendall(self, data):
            self.sent.append(data)

    def test_ping_returns_false_on_dead_socket(self):
        client = NAMESPACE["RPCClient"]("/base/discord-ipc")
        client.sock = self.FakeSocket()
        client.sock.sendall = lambda data: (_ for _ in ()).throw(OSError("broken pipe"))
        self.assertFalse(client.ping())

    def test_ping_returns_false_on_oversized_frame(self):
        client = NAMESPACE["RPCClient"]("/base/discord-ipc")
        client.sock = self.FakeSocket()

        def oversized(sock, timeout):
            raise ValueError("IPC frame too large: 999999999")

        with patch.dict(NAMESPACE, {"read_frame": oversized}):
            self.assertFalse(client.ping())

    def test_ping_returns_true_on_pong(self):
        client = NAMESPACE["RPCClient"]("/base/discord-ipc")
        client.sock = self.FakeSocket()

        def pong(sock, timeout):
            return (NAMESPACE["IPC_PONG"], {"nonce": "1"})

        with patch.dict(NAMESPACE, {"read_frame": pong}):
            self.assertTrue(client.ping())


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

    def test_candidate_paths_prefer_process_wide_last_good_path(self):
        with patch.dict(
            NAMESPACE, {"_LAST_GOOD_SOCKET_PATH": "/base/discord-ipc-2"}
        ):
            client = NAMESPACE["RPCClient"]("/base/discord-ipc", max_socket_attempts=4)
            self.assertEqual(client.candidate_paths(), [
                "/base/discord-ipc-2",
                "/base/discord-ipc-0",
                "/base/discord-ipc-1",
                "/base/discord-ipc-3",
            ])

    def test_candidate_paths_ignore_last_good_from_another_base(self):
        with patch.dict(
            NAMESPACE, {"_LAST_GOOD_SOCKET_PATH": "/other/discord-ipc-2"}
        ):
            client = NAMESPACE["RPCClient"]("/base/discord-ipc", max_socket_attempts=2)
            self.assertEqual(client.candidate_paths(), [
                "/base/discord-ipc-0",
                "/base/discord-ipc-1",
            ])

    def test_failed_connect_clears_stale_last_good_path(self):
        client = NAMESPACE["RPCClient"]("/base/discord-ipc", max_socket_attempts=2)
        with (
            patch.dict(NAMESPACE, {"_LAST_GOOD_SOCKET_PATH": "/base/discord-ipc-0"}),
            patch.object(NAMESPACE["RPCClient"], "_connect", side_effect=OSError("refused")),
        ):
            with self.assertRaises(OSError):
                client.connect("123")
            self.assertEqual(NAMESPACE["_LAST_GOOD_SOCKET_PATH"], "")

    def test_successful_connect_records_process_wide_path(self):
        client = NAMESPACE["RPCClient"]("/base/discord-ipc", max_socket_attempts=3)

        class Dummy:
            def sendall(self, data):
                pass

        with (
            patch.dict(NAMESPACE, {"_LAST_GOOD_SOCKET_PATH": ""}),
            patch.object(NAMESPACE["RPCClient"], "_connect", return_value=Dummy()),
            patch.dict(NAMESPACE, {"read_frame": lambda _sock, _timeout: None}),
        ):
            client.connect("123")
            self.assertEqual(
                NAMESPACE["_LAST_GOOD_SOCKET_PATH"], "/base/discord-ipc-0"
            )

    def test_connect_failure_raises(self):
        client = NAMESPACE["RPCClient"]("/base/discord-ipc", max_socket_attempts=1)
        with patch("socket.socket") as mock_socket:
            mock_socket.return_value.connect.side_effect = OSError("no socket")
            with self.assertRaises(OSError):
                client.connect("123")

    def test_handshake_oversized_frame_closes_socket(self):
        client = NAMESPACE["RPCClient"]("/base/discord-ipc", max_socket_attempts=1)
        closed = []

        class Dummy:
            def sendall(self, data):
                pass

            def close(self):
                closed.append(True)

        with (
            patch.object(NAMESPACE["RPCClient"], "_connect", return_value=Dummy()),
            patch.dict(NAMESPACE, {
                "read_frame": lambda _sock, _timeout: (_ for _ in ()).throw(
                    ValueError("IPC frame too large")),
            }),
        ):
            with self.assertRaises(ValueError):
                client.connect("123")
        self.assertEqual(closed, [True])
        self.assertIsNone(client.sock)

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
