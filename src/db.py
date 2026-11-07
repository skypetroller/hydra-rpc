# hydra-rpc source module: db (bundled into ./hydra-rpc by build.py)
def download_db(url):
    req = urllib.request.Request(url, headers={"User-Agent": "hydra-rpc/1.0"})
    with urllib.request.urlopen(req, timeout=60) as r:
        payload = r.read(MAX_DB_BYTES + 1)
    if len(payload) > MAX_DB_BYTES:
        raise ValueError(f"database response exceeds {MAX_DB_BYTES} bytes")
    return json.loads(payload.decode("utf-8"))


def validate_db(db):
    """Reject JSON-valid responses that do not have the expected shape."""
    if not isinstance(db, list):
        raise ValueError("expected a JSON list of games")
    if not db:
        # Discord's endpoint is never legitimately empty. Accepting [] would
        # replace a good cache and silently disable detection for a whole TTL.
        raise ValueError("expected at least one game")
    for game in db:
        if not isinstance(game, dict):
            raise ValueError("expected game objects")
        executables = game.get("executables", [])
        if not isinstance(executables, list):
            raise ValueError("expected an executable list per game")
        for executable in executables:
            if not isinstance(executable, dict):
                raise ValueError("expected executable objects")
    return db


def _write_private_json(path, data, **json_kwargs):
    """Atomically write JSON with owner-only permissions."""
    os.makedirs(CACHE_DIR, mode=0o700, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, **json_kwargs)
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


# Metadata about the last load_db() call, used by build_status_report().
_DB_INFO = {"cached_at": None, "fresh": None, "stale_fallback": False}


def format_age(seconds):
    try:
        seconds = max(0, int(seconds))
    except (TypeError, ValueError):
        return "unknown age"
    if seconds < 60:
        return f"{seconds}s"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m"
    hours = minutes // 60
    if hours < 48:
        return f"{hours}h"
    return f"{hours // 24}d"


def build_index(db):
    index = {}
    for game in db:
        gid = str(game.get("id", ""))
        gname = game.get("name", "Unknown Game")
        for exe in game.get("executables", []):
            if exe.get("is_launcher"):
                continue
            name = exe.get("name", "")
            if not name:
                continue
            name = name.replace("\\", "/").lstrip(">").strip()
            base = name.rsplit("/", 1)[-1].lower()
            if base and base not in index:
                index[base] = (gid, gname)
    return index


def load_db(cfg):
    global _DB_INFO
    _DB_INFO = {"cached_at": None, "fresh": None, "stale_fallback": False}
    try:
        if os.path.exists(DB_CACHE):
            _DB_INFO["cached_at"] = int(os.path.getmtime(DB_CACHE))
    except OSError:
        pass

    if os.path.exists(DB_CACHE):
        try:
            os.chmod(DB_CACHE, 0o600)
        except OSError:
            pass
        try:
            age = time.time() - os.path.getmtime(DB_CACHE)
            if age < cfg["db_ttl_seconds"]:
                with open(DB_CACHE, "r") as f:
                    cached = validate_db(json.load(f))
                index = build_index(cached)
                log(f"using cached game database from {format_age(age)} ago", "debug")
                return index
        except Exception as e:
            log(f"failed to load cached db ({e})", "warning")

    try:
        log("downloading detectable applications database...")
        db = validate_db(download_db(cfg["db_url"]))
        # Build before persisting: a malformed but JSON-valid response must not
        # replace a good cache and then crash the watcher.
        index = build_index(db)
        _write_private_json(DB_CACHE, db)
        log(f"cached {len(db)} detectable applications")
        try:
            _DB_INFO["cached_at"] = int(os.path.getmtime(DB_CACHE))
        except OSError:
            pass
        _DB_INFO["fresh"] = True
        return index
    except Exception as e:
        if os.path.exists(DB_CACHE):
            try:
                stale_age = time.time() - os.path.getmtime(DB_CACHE)
            except OSError:
                stale_age = None
            age_text = format_age(stale_age) if stale_age is not None else "unknown age"
            log(
                f"could not refresh db ({e}); using stale cache from "
                f"{age_text} ago with limited detection",
                "warning",
            )
            try:
                with open(DB_CACHE, "r") as f:
                    cached = validate_db(json.load(f))
                index = build_index(cached)
                _DB_INFO["stale_fallback"] = True
                return index
            except Exception as stale_error:
                log(
                    f"stale db cache is unusable ({stale_error}); "
                    f"continuing without it",
                    "warning",
                )
        else:
            log(f"could not download db ({e}); no cache available", "error")

    return {}


def refresh_db_index(cfg, current_index):
    """Refresh the database, keeping current_index when the refresh fails.

    load_db() falls back to a stale cache after a failed download, so its
    return value alone cannot distinguish success from failure. A periodic
    refresh must not report a stale fallback as a successful update.
    """
    refreshed = load_db(cfg)
    if not refreshed or _DB_INFO.get("stale_fallback"):
        log("database refresh failed; keeping the current database", "warning")
        return current_index
    log(f"refreshed game database: {len(refreshed)} executable mappings")
    return refreshed


def db_refresh_interval(cfg, stale_fallback=False):
    """Seconds until the next periodic database refresh.

    db_ttl_seconds controls cache freshness; the floor keeps a TTL of zero
    from downloading the multi-megabyte database on every poll. A stale
    fallback retries hourly instead of waiting for the full TTL.
    """
    try:
        ttl = int(cfg.get("db_ttl_seconds", 0))
    except (TypeError, ValueError):
        ttl = 0
    interval = max(ttl, DB_REFRESH_MIN_SECONDS)
    if stale_fallback:
        interval = min(interval, DB_REFRESH_MIN_SECONDS)
    return interval


STALE_SESSION_MAX_AGE_MS = 7 * 24 * 3600 * 1000
MAX_SESSION_ENTRIES = 256


def prune_sessions(sessions, now_ms=None):
    # Drop malformed or ancient entries and cap the total count so the cache
    # file stays small even across crashes. Keeps the newest entries.
    if now_ms is None:
        now_ms = int(time.time() * 1000)
    valid = {}
    for exe, record in sessions.items():
        try:
            start_ms = int(record["start_ms"])
            int(record["pid"])
            int(record["start_tick"])
        except (KeyError, TypeError, ValueError, AttributeError):
            continue
        if 0 < start_ms <= now_ms and now_ms - start_ms <= STALE_SESSION_MAX_AGE_MS:
            valid[exe] = record
    if len(valid) > MAX_SESSION_ENTRIES:
        # int() is safe here: every entry above passed int(record["start_ms"]).
        newest = sorted(
            valid, key=lambda exe: int(valid[exe]["start_ms"])
        )[-MAX_SESSION_ENTRIES:]
        valid = {exe: valid[exe] for exe in newest}
    return valid


def load_sessions():
    if not os.path.exists(SESSION_CACHE):
        return {}
    try:
        os.chmod(SESSION_CACHE, 0o600)
    except OSError:
        pass
    try:
        with open(SESSION_CACHE, "r") as f:
            sessions = json.load(f)
        if not isinstance(sessions, dict):
            return {}
    except (OSError, ValueError) as e:
        log(f"could not read session cache ({e})", "warning")
        return {}
    return prune_sessions(sessions)


def save_sessions(sessions):
    try:
        _write_private_json(
            SESSION_CACHE,
            prune_sessions(sessions),
            indent=2,
            sort_keys=True,
        )
    except OSError as e:
        log(f"could not save session cache ({e})", "warning")
