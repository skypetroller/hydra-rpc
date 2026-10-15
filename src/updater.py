# hydra-rpc source module: updater (bundled into ./hydra-rpc by build.py)
def installed_script_path():
    return os.path.realpath(globals().get("__file__") or sys.argv[0])


def validate_update_source(source):
    try:
        text = source.decode("utf-8")
    except UnicodeDecodeError as e:
        raise ValueError("downloaded update is not valid UTF-8") from e
    if not text.startswith("#!/usr/bin/env python3"):
        raise ValueError("downloaded update is not a hydra-rpc script")
    try:
        compile(text, UPDATE_URL, "exec")
    except SyntaxError as e:
        raise ValueError(f"downloaded update has invalid Python syntax: {e}") from e
    return source


def fetch_update_checksum_body():
    # Best-effort fetch of UPDATE_SHA_URL; returns the raw bytes (needed for
    # signature verification) or None when no checksum is published.
    req = urllib.request.Request(UPDATE_SHA_URL, headers={"User-Agent": "hydra-rpc-updater/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=UPDATE_TIMEOUT) as response:
            return response.read()
    except Exception:
        return None


def checksum_from_body(body):
    # Accepts "<hex>  <name>" or bare hex; returns the lowercase digest or None.
    try:
        text = body.decode("utf-8").strip().split()
    except (AttributeError, UnicodeDecodeError):
        return None
    if not text or len(text[0]) != 64 or any(c not in "0123456789abcdefABCDEF" for c in text[0]):
        return None
    return text[0].lower()


def fetch_update_signature_body():
    # Best-effort fetch of UPDATE_SIG_URL; returns raw bytes or None.
    req = urllib.request.Request(UPDATE_SIG_URL, headers={"User-Agent": "hydra-rpc-updater/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=UPDATE_TIMEOUT) as response:
            return response.read()
    except Exception:
        return None


def parse_update_signature(body):
    # Format: optional "untrusted comment: ..." lines, then one base64 line
    # holding the 64-byte Ed25519 signature. Returns bytes or None.
    try:
        lines = body.decode("utf-8").splitlines()
    except (AttributeError, UnicodeDecodeError):
        return None
    for line in lines:
        line = line.strip()
        if not line or line.startswith("untrusted comment:"):
            continue
        try:
            signature = base64.b64decode(line, validate=True)
        except Exception:
            return None
        return signature if len(signature) == ED25519_SIGNATURE_SIZE else None
    return None


def verify_update_signature(checksum_body, signature):
    # True when `signature` is a valid Ed25519 signature over the exact bytes
    # of `checksum_body` for one of the embedded UPDATE_PUBLIC_KEYS.
    for key_hex in UPDATE_PUBLIC_KEYS:
        try:
            public_key = bytes.fromhex(key_hex)
        except ValueError:
            continue
        if ed25519_verify(signature, checksum_body, public_key):
            return True
    return False


def fetch_update_source():
    req = urllib.request.Request(UPDATE_URL, headers={"User-Agent": "hydra-rpc-updater/1.0"})
    with urllib.request.urlopen(req, timeout=UPDATE_TIMEOUT) as response:
        source = response.read()
    checksum_body = fetch_update_checksum_body()
    signature_body = fetch_update_signature_body()
    if signature_body is not None:
        # A published signature is authoritative: fail closed if anything
        # about it (or the checksum it covers) is off.
        if checksum_body is None:
            raise ValueError(
                "update is signed but its checksum file could not be fetched; refusing update"
            )
        signature = parse_update_signature(signature_body)
        if signature is None or not verify_update_signature(checksum_body, signature):
            raise ValueError(
                "update signature verification failed (tampered release or unknown key); "
                "refusing update"
            )
        log("update signature verified", "debug")
    expected = checksum_from_body(checksum_body) if checksum_body is not None else None
    if expected is not None:
        actual = hashlib.sha256(source).hexdigest()
        if actual != expected:
            raise ValueError(
                "update checksum mismatch (expected "
                f"{expected[:12]}..., got {actual[:12]}...); refusing update"
            )
        log("update checksum verified", "debug")
    else:
        log("no update checksum published; verified Python syntax only", "debug")
    return validate_update_source(source)


def install_update(path, source):
    path = os.path.realpath(path)
    parent = os.path.dirname(path) or "."
    mode = os.stat(path).st_mode & 0o777 if os.path.exists(path) else 0o755
    fd, temporary = tempfile.mkstemp(prefix=".hydra-rpc-update-", dir=parent)
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(source)
            output.flush()
            os.fsync(output.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def update_script(install=False):
    target = installed_script_path()
    try:
        with open(target, "rb") as current_file:
            current = current_file.read()
        latest = fetch_update_source()
    except (OSError, urllib.error.URLError, ValueError) as e:
        print(f"update failed: {e}", file=sys.stderr)
        return 1

    if current == latest:
        print("hydra-rpc is already up to date")
        return 0
    if not install:
        print(f"update available for {target}")
        print(f"latest source: {hashlib.sha256(latest).hexdigest()}")
        return 0

    try:
        install_update(target, latest)
    except OSError as e:
        print(f"update failed while replacing {target}: {e}", file=sys.stderr)
        return 1
    print(f"updated {target}; restart hydra-rpc to use the new version")
    return 0
