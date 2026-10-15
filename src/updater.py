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


def fetch_update_checksum():
    # Best-effort SHA256 check against UPDATE_URL.sha256 ("<hex>  <name>" or bare hex).
    # Returns the expected hex digest, or None when no checksum is published.
    req = urllib.request.Request(UPDATE_SHA_URL, headers={"User-Agent": "hydra-rpc-updater/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=UPDATE_TIMEOUT) as response:
            text = response.read().decode("utf-8").strip().split()
    except Exception:
        return None
    if not text or any(c not in "0123456789abcdefABCDEF" for c in text[0]) or len(text[0]) != 64:
        return None
    return text[0].lower()


def fetch_update_source():
    req = urllib.request.Request(UPDATE_URL, headers={"User-Agent": "hydra-rpc-updater/1.0"})
    with urllib.request.urlopen(req, timeout=UPDATE_TIMEOUT) as response:
        source = response.read()
    expected = fetch_update_checksum()
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
