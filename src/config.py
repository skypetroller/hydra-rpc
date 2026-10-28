# hydra-rpc source module: config (bundled into ./hydra-rpc by build.py)
def log_enabled(level="info"):
    """True when a log call at this level would be emitted."""
    return LOG_LEVELS.get(level, LOG_LEVELS["info"]) >= CURRENT_LOG_LEVEL


def log(msg, level="info"):
    if not log_enabled(level):
        return
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    # --status --json keeps stdout as pure JSON; diagnostics go to stderr there.
    output = sys.stderr if LOG_TO_STDERR else sys.stdout
    print(line, flush=True, file=output)
    if LOG_FILE_HANDLE is not None:
        try:
            LOG_FILE_HANDLE.write(line + "\n")
            LOG_FILE_HANDLE.flush()
        except OSError:
            pass


def configure_logging(cfg):
    global CURRENT_LOG_LEVEL, LOG_FILE_HANDLE
    CURRENT_LOG_LEVEL = LOG_LEVELS[cfg["log_level"]]
    path = cfg.get("log_file", "")
    if not path:
        return
    path = os.path.expanduser(os.path.expandvars(path))
    try:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        LOG_FILE_HANDLE = open(path, "a", buffering=1, encoding="utf-8")
    except OSError as e:
        LOG_FILE_HANDLE = None
        log(f"could not open log_file '{path}': {e}", "warning")


def close_logging():
    global LOG_FILE_HANDLE
    if LOG_FILE_HANDLE is not None:
        try:
            LOG_FILE_HANDLE.close()
        except OSError:
            pass
        LOG_FILE_HANDLE = None


def is_valid_app_id(value):
    # Discord application IDs are numeric snowflakes. Empty means "unconfigured"
    # (allowed for emulator_application_ids); anything non-empty must be digits.
    if isinstance(value, int):
        return value >= 0
    if not isinstance(value, str):
        return False
    text = value.strip()
    return text == "" or text.isdigit()


def migrate_config(cfg, from_version, to_version):
    # Stub for future renames/removals: today it only explains what happened.
    # New settings already merged from DEFAULT_CONFIG; nothing else to rewrite.
    if from_version < 1:
        log(
            "older config format detected (no config_version); "
            "new settings use defaults (run hydra-rpc --validate-config to check)",
            "warning",
        )
    elif from_version < to_version:
        log(
            f"config version {from_version} is older than {to_version}; "
            "new settings use defaults",
            "warning",
        )
    return cfg


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    user_keys = set()
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r") as f:
                user = json.load(f)
            if not isinstance(user, dict):
                raise ValueError("top-level config must be an object")
            user_keys = set(user)
            for key in sorted(user_keys - set(DEFAULT_CONFIG)):
                log(f"unknown config key '{key}'; ignoring it", "warning")
            cfg.update({k: v for k, v in user.items() if k in DEFAULT_CONFIG})
        except Exception as e:
            log(f"could not read config ({e}); using defaults", "warning")

    def normalise_number(key, minimum):
        default = DEFAULT_CONFIG[key]
        try:
            value = float(cfg.get(key, default))
            if not math.isfinite(value) or value < minimum:
                raise ValueError
            cfg[key] = int(value) if value.is_integer() else value
        except (TypeError, ValueError):
            log(f"invalid {key}; using {default}", "warning")
            cfg[key] = default

    def normalise_integer(key, minimum, maximum):
        default = DEFAULT_CONFIG[key]
        try:
            raw = cfg.get(key, default)
            if isinstance(raw, bool):
                raise ValueError
            value = float(raw)
            if not math.isfinite(value) or value != int(value):
                raise ValueError
            value = int(value)
            if value < minimum or value > maximum:
                raise ValueError
            cfg[key] = value
        except (TypeError, ValueError, OverflowError):
            log(f"invalid {key}; using {default}", "warning")
            cfg[key] = default

    normalise_number("poll_seconds", 1)
    normalise_number("activity_refresh_seconds", 1)
    normalise_number("db_ttl_seconds", 0)
    normalise_integer("max_socket_attempts", 1, MAX_SOCKET_PATHS)
    normalise_integer("max_activities", 0, MAX_ACTIVITIES)

    if not isinstance(cfg.get("hydra_only"), bool):
        log("invalid hydra_only; using false", "warning")
        cfg["hydra_only"] = False

    if not isinstance(cfg.get("refresh_unchanged_activities"), bool):
        log("invalid refresh_unchanged_activities; using true", "warning")
        cfg["refresh_unchanged_activities"] = True

    if not isinstance(cfg.get("emulators_enabled"), bool):
        log("invalid emulators_enabled; using false", "warning")
        cfg["emulators_enabled"] = False

    emulator_ids = dict(DEFAULT_EMULATOR_APPLICATION_IDS)
    raw_emulator_ids = cfg.get("emulator_application_ids", {})
    if isinstance(raw_emulator_ids, dict):
        for emulator in emulator_ids:
            value = raw_emulator_ids.get(emulator, emulator_ids[emulator])
            if isinstance(value, (str, int)):
                text = str(value).strip()
                emulator_ids[emulator] = text
                if text and not text.isdigit():
                    log(
                        f"invalid application ID for {emulator} ('{text}'); "
                        "Discord application IDs are all-digit snowflakes",
                        "warning",
                    )
            else:
                log(f"invalid application ID for {emulator}; using the default", "warning")
    else:
        log("invalid emulator_application_ids; using defaults", "warning")
    cfg["emulator_application_ids"] = emulator_ids

    if not isinstance(cfg.get("db_url"), str) or not cfg["db_url"].strip():
        log("invalid db_url; using the Discord detectable-applications endpoint", "warning")
        cfg["db_url"] = DB_URL

    for key in ("socket_dir", "socket_path", "log_file"):
        if not isinstance(cfg.get(key), str):
            log(f"invalid {key}; using the default", "warning")
            cfg[key] = ""

    if not isinstance(cfg.get("activity_template"), str) or not cfg["activity_template"]:
        log("invalid activity_template; using the game name", "warning")
        cfg["activity_template"] = DEFAULT_CONFIG["activity_template"]

    if not isinstance(cfg.get("emulator_activity_template"), str) or not cfg["emulator_activity_template"]:
        log("invalid emulator_activity_template; using the emulator name", "warning")
        cfg["emulator_activity_template"] = DEFAULT_CONFIG["emulator_activity_template"]

    if not isinstance(cfg.get("rich_activity"), dict):
        log("invalid rich_activity; ignoring it", "warning")
        cfg["rich_activity"] = {}

    log_level = str(cfg.get("log_level", "info")).lower()
    if log_level not in LOG_LEVELS:
        log("invalid log_level; using info", "warning")
        log_level = "info"
    cfg["log_level"] = log_level

    def normalise_string_set(key, casefold=False):
        raw = cfg.get(key, [])
        if not isinstance(raw, (list, tuple, set)):
            log(f"invalid {key}; using an empty list", "warning")
            cfg[key] = set()
            return
        values = {str(x).strip() for x in raw if str(x).strip()}
        cfg[key] = {x.casefold() for x in values} if casefold else values

    normalise_string_set("blocklist_ids")
    for blocked_id in sorted(cfg["blocklist_ids"]):
        if not blocked_id.isdigit():
            log(
                f"invalid blocklist_ids entry '{blocked_id}'; "
                "Discord application IDs are all-digit snowflakes",
                "warning",
            )
    normalise_string_set("blocklist_names", casefold=True)
    normalise_string_set("hydra_markers", casefold=True)
    if not cfg["hydra_markers"]:
        cfg["hydra_markers"] = set(DEFAULT_HYDRA_MARKERS)

    raw_blocklist = cfg.get("blocklist", [])
    if isinstance(raw_blocklist, (list, tuple, set)):
        cfg["blocklist"] = {str(x).lower() for x in raw_blocklist if str(x)}
    else:
        log("invalid blocklist; using defaults", "warning")
        cfg["blocklist"] = set(DEFAULT_BLOCKLIST)

    overrides = {}
    raw_overrides = cfg.get("overrides") or {}
    if not isinstance(raw_overrides, dict):
        log("invalid overrides; ignoring them", "warning")
        raw_overrides = {}
    for exe, info in raw_overrides.items():
        if not isinstance(exe, str) or not exe.strip() or not isinstance(info, dict):
            log("ignoring malformed game override", "warning")
            continue
        app_id = info.get("id")
        name = info.get("name")
        if not app_id or not name:
            log(f"ignoring incomplete override for '{exe}'", "warning")
            continue
        app_text = str(app_id).strip()
        if not app_text.isdigit():
            log(
                f"invalid application ID in override for '{exe}' ('{app_text}'); "
                "Discord application IDs are all-digit snowflakes",
                "warning",
            )
        overrides[exe.lower()] = (str(app_id), str(name))
    cfg["overrides"] = overrides

    emulator_overrides = {}
    raw_emulator_overrides = cfg.get("emulator_overrides") or {}
    if not isinstance(raw_emulator_overrides, dict):
        log("invalid emulator_overrides; ignoring them", "warning")
        raw_emulator_overrides = {}
    for key, info in raw_emulator_overrides.items():
        if not isinstance(key, str) or not key.strip() or not isinstance(info, dict):
            log("ignoring malformed emulator override", "warning")
            continue
        app_id = info.get("id")
        name = info.get("name")
        if not app_id or not name:
            log(f"ignoring incomplete emulator override for '{key}'", "warning")
            continue
        app_text = str(app_id).strip()
        if not app_text.isdigit():
            log(
                f"invalid application ID in emulator override for '{key}' "
                f"('{app_text}'); Discord application IDs are all-digit snowflakes",
                "warning",
            )
        emulator_overrides[key.casefold()] = {"id": str(app_id), "name": str(name)}
    cfg["emulator_overrides"] = emulator_overrides
    socket_path = cfg.get("socket_path", "")
    if isinstance(socket_path, str) and socket_path.strip():
        expanded = os.path.expanduser(os.path.expandvars(socket_path.strip()))
        if not os.path.exists(expanded):
            log(
                f"configured socket_path '{socket_path}' does not exist "
                f"(expanded: '{expanded}'); automatic socket discovery is disabled "
                "while socket_path is set",
                "warning",
            )
    try:
        config_version = int(cfg.get("config_version", 0))
    except (TypeError, ValueError):
        config_version = 0
        log("invalid config_version; assuming an older config format", "warning")
    if config_version < CONFIG_VERSION:
        migrate_config(cfg, config_version, CONFIG_VERSION)
    cfg["config_version"] = CONFIG_VERSION
    return cfg


def ensure_config(cfg):
    if not os.path.exists(CONFIG_PATH):
        try:
            os.makedirs(CONFIG_DIR, exist_ok=True)
            with open(CONFIG_PATH, "w") as f:
                json.dump(DEFAULT_CONFIG, f, indent=2)
        except Exception as e:
            log(f"could not write default config ({e})", "warning")
