# hydra-rpc source module: db (bundled into ./hydra-rpc by build.py)
def download_db(url):
    req = urllib.request.Request(url, headers={"User-Agent": "hydra-rpc/1.0"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode("utf-8"))


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
    cached = None
    if os.path.exists(DB_CACHE):
        try:
            age = time.time() - os.path.getmtime(DB_CACHE)
            if age < cfg["db_ttl_seconds"]:
                with open(DB_CACHE, "r") as f:
                    cached = json.load(f)
                log(f"using cached game database from {format_age(age)} ago", "debug")
        except Exception as e:
            log(f"failed to load cached db ({e})", "warning")

    if cached is None:
        try:
            log("downloading detectable applications database...")
            db = download_db(cfg["db_url"])
            os.makedirs(CACHE_DIR, exist_ok=True)
            tmp = DB_CACHE + ".tmp"
            with open(tmp, "w") as f:
                json.dump(db, f)
            os.replace(tmp, DB_CACHE)
            log(f"cached {len(db)} detectable applications")
            try:
                _DB_INFO["cached_at"] = int(os.path.getmtime(DB_CACHE))
            except OSError:
                pass
            _DB_INFO["fresh"] = True
            cached = db
        except Exception as e:
            if os.path.exists(DB_CACHE):
                try:
                    stale_age = time.time() - os.path.getmtime(DB_CACHE)
                except OSError:
                    stale_age = None
                age_text = format_age(stale_age) if stale_age is not None else "unknown age"
                log(
                    f"could not download db ({e}); using stale cache from "
                    f"{age_text} ago with limited detection",
                    "warning",
                )
                _DB_INFO["stale_fallback"] = True
                with open(DB_CACHE, "r") as f:
                    cached = json.load(f)
            else:
                log(f"could not download db ({e}); no cache available", "error")

    return build_index(cached) if cached else {}


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
        os.makedirs(CACHE_DIR, exist_ok=True)
        tmp = SESSION_CACHE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(prune_sessions(sessions), f, indent=2, sort_keys=True)
        os.replace(tmp, SESSION_CACHE)
    except OSError as e:
        log(f"could not save session cache ({e})", "warning")
