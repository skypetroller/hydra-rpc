# hydra-rpc source module: ipc (bundled into ./hydra-rpc by build.py)
def runtime_dir(cfg):
    d = cfg.get("socket_dir") or os.environ.get("XDG_RUNTIME_DIR")
    if d:
        return os.path.expanduser(os.path.expandvars(d))
    return f"/run/user/{os.getuid()}"


def encode_frame(msg_type, obj):
    data = json.dumps(obj).encode("utf-8")
    return struct.pack("<II", msg_type, len(data)) + data


def read_exact(sock, n):
    chunks = []
    got = 0
    while got < n:
        chunk = sock.recv(n - got)
        if not chunk:
            raise ConnectionError("socket closed")
        chunks.append(chunk)
        got += len(chunk)
    return b"".join(chunks)


def read_frame(sock, timeout):
    sock.settimeout(timeout)
    try:
        header = read_exact(sock, 8)
    except (socket.timeout, ConnectionError, OSError):
        return None
    msg_type, size = struct.unpack("<II", header)
    if size > MAX_IPC_FRAME_SIZE:
        raise ValueError(f"IPC frame too large: {size}")
    try:
        data = read_exact(sock, size)
    except (socket.timeout, ConnectionError, OSError):
        return None
    try:
        return msg_type, json.loads(data.decode("utf-8"))
    except Exception:
        return msg_type, None


# Process-wide memory of the last socket path that accepted a connection.
# New RPCClient instances (one per detected game) start with it, so the
# common -0/-1 probes are skipped in steady state.
_LAST_GOOD_SOCKET_PATH = ""


class RPCClient:
    def __init__(self, base, socket_path="", max_socket_attempts=DEFAULT_SOCKET_ATTEMPTS):
        self.base = base
        socket_path = str(socket_path or "")
        self.socket_path = os.path.expanduser(os.path.expandvars(socket_path))
        self.max_socket_attempts = max(
            1, min(MAX_SOCKET_PATHS, int(max_socket_attempts))
        )
        self.path = None
        self.last_path = None
        self.sock = None
        self.client_id = None
        self.nonce = 0

    def _connect(self):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(SOCKET_CONNECT_TIMEOUT)
        try:
            s.connect(self.path)
            return s
        except OSError:
            s.close()
            raise

    def candidate_paths(self):
        if self.socket_path:
            return [self.socket_path]

        paths = []
        if self.last_path:
            paths.append(self.last_path)
        if (
            _LAST_GOOD_SOCKET_PATH
            and _LAST_GOOD_SOCKET_PATH.startswith(self.base + "-")
            and _LAST_GOOD_SOCKET_PATH not in paths
        ):
            paths.append(_LAST_GOOD_SOCKET_PATH)
        for i in range(self.max_socket_attempts):
            if len(paths) >= self.max_socket_attempts:
                break
            path = f"{self.base}-{i}"
            if path not in paths:
                paths.append(path)
        return paths

    def connect(self, client_id):
        global _LAST_GOOD_SOCKET_PATH
        if self.sock is not None:
            self.close()
        last_error = None
        candidates = self.candidate_paths()
        for path in candidates:
            self.path = path
            try:
                self.sock = self._connect()
                self.last_path = path
                _LAST_GOOD_SOCKET_PATH = path
                break
            except OSError as e:
                last_error = e
                self.sock = None
        if self.sock is None:
            if _LAST_GOOD_SOCKET_PATH in candidates:
                # Do not let a dead path consume candidate slots next time.
                _LAST_GOOD_SOCKET_PATH = ""
            self.path = self.socket_path or None
            raise last_error or OSError(f"no arRPC socket found under {self.base}")
        try:
            self.sock.sendall(encode_frame(IPC_HANDSHAKE, {"v": 1, "client_id": client_id}))
            read_frame(self.sock, HANDSHAKE_READ_TIMEOUT)  # drain READY
        except (OSError, ValueError):
            self.close()
            raise
        self.client_id = client_id

    def send(self, cmd, args):
        self.nonce += 1
        self.sock.sendall(
            encode_frame(IPC_FRAME, {"cmd": cmd, "args": args, "nonce": str(self.nonce)})
        )
        read_frame(self.sock, ACTIVITY_ACK_TIMEOUT)  # drain ack (content ignored)

    def ping(self):
        # Cheap liveness probe: a few bytes round-trip instead of a full
        # activity resend. Returns True only on a matching PONG echo, False on
        # timeout/closed socket/oversized frame (ValueError), so the loop
        # treats protocol-level breakage as a dead connection and reconnects.
        try:
            self.nonce += 1
            payload = {"nonce": str(self.nonce)}
            self.sock.sendall(encode_frame(IPC_PING, payload))
            reply = read_frame(self.sock, PING_PROBE_TIMEOUT)
        except (OSError, ValueError):
            return False
        return bool(reply) and reply[0] == IPC_PONG and reply[1] == payload

    def close(self):
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
        self.sock = None
        self.client_id = None
