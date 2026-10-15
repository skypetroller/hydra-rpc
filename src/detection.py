# hydra-rpc source module: detection (bundled into ./hydra-rpc by build.py)
from collections import OrderedDict

RESERVED_ACTIVITY_FIELDS = {"application_id", "name", "timestamps", "type"}

_ACTIVITY_CACHE = OrderedDict()
_ACTIVITY_CACHE_MAX = 512


def cached_activity(cache_key, make):
    try:
        activity = _ACTIVITY_CACHE.pop(cache_key)
    except KeyError:
        pass
    except TypeError:
        return make()
    else:
        # Return an independent copy so callers can never mutate the cached entry.
        _ACTIVITY_CACHE[cache_key] = activity
        return copy.deepcopy(activity)
    activity = make()
    while len(_ACTIVITY_CACHE) >= _ACTIVITY_CACHE_MAX:
        _ACTIVITY_CACHE.popitem(last=False)
    _ACTIVITY_CACHE[cache_key] = activity
    return copy.deepcopy(activity)


def rich_json(rich_activity):
    try:
        return json.dumps(rich_activity, sort_keys=True, default=str)
    except (TypeError, ValueError):
        return ""


def rich_fingerprint(cfg):
    # The rich-activity config is fixed for the life of the process, so
    # serialize it once instead of on every candidate, every poll.
    fingerprint = cfg.get("_rich_json")
    if fingerprint is None:
        fingerprint = rich_json(cfg["rich_activity"])
        try:
            cfg["_rich_json"] = fingerprint
        except TypeError:
            pass
    return fingerprint


class TemplateValues(dict):
    def __missing__(self, key):
        return "{" + key + "}"


def render_text(value, values):
    if not isinstance(value, str):
        return value
    try:
        return value.format_map(TemplateValues(values))
    except (KeyError, ValueError):
        return value


def render_activity_value(value, values):
    if isinstance(value, str):
        return render_text(value, values)
    if isinstance(value, list):
        return [render_activity_value(item, values) for item in value]
    if isinstance(value, dict):
        return {key: render_activity_value(item, values) for key, item in value.items()}
    return value


def resolve_game(exe, info, overrides, index, cfg):
    if isinstance(info, dict) and info.get("kind") == "emulator":
        return resolve_emulator_game(exe, info, cfg)

    app = overrides.get(exe) or index.get(exe)
    if not app:
        return None

    app_id, game_name = app
    game_name_key = game_name.casefold()
    if app_id in cfg["blocklist_ids"] or game_name_key in cfg["blocklist_names"]:
        return None

    if isinstance(info, dict):
        pid = info["pid"]
        if cfg["hydra_only"] and "hydra" not in info.get("sources", set()):
            return None
    else:
        pid = info
        if cfg["hydra_only"]:
            return None
    values = {
        "game_name": game_name,
        "exe": exe,
        "app_id": app_id,
    }
    template = cfg["activity_template"]
    cache_key = ("wine", exe, app_id, game_name, template, rich_fingerprint(cfg))

    def build():
        activity = {
            "application_id": app_id,
            "name": render_text(template, values),
        }
        rich_activity = render_activity_value(cfg["rich_activity"], values)
        for key, value in rich_activity.items():
            if key not in RESERVED_ACTIVITY_FIELDS:
                activity[key] = value
        return activity

    activity = cached_activity(cache_key, build)

    return {
        "exe": exe,
        "app_id": app_id,
        "game_name": game_name,
        "display_name": activity["name"],
        "pid": pid,
        "activity": activity,
    }


def emulator_override(exe, info, cfg):
    emulator = info["emulator"]
    rom_name = info["rom_name"].casefold()
    overrides = cfg["emulator_overrides"]
    return (
        overrides.get(f"{emulator}:{rom_name}")
        or overrides.get(exe.casefold())
    )


def emulator_application_id(exe, info, cfg):
    override = emulator_override(exe, info, cfg)
    return (override or {}).get("id") or cfg["emulator_application_ids"].get(
        info["emulator"], ""
    )


def resolve_emulator_game(exe, info, cfg):
    app_id = emulator_application_id(exe, info, cfg)
    if not app_id:
        return None

    game_name_key = info["rom_name"].casefold()
    if app_id in cfg["blocklist_ids"] or game_name_key in cfg["blocklist_names"]:
        return None

    if cfg["hydra_only"] and "hydra" not in info.get("sources", set()):
        return None

    emulator_name = DEFAULT_EMULATOR_NAMES[info["emulator"]]
    values = {
        "game_name": info["rom_name"],
        "rom_name": info["rom_name"],
        "emulator": info["emulator"],
        "emulator_name": emulator_name,
        "exe": exe,
        "app_id": app_id,
    }
    override_name = (emulator_override(exe, info, cfg) or {}).get("name")
    template = cfg["emulator_activity_template"]
    cache_key = (
        "emulator", exe, app_id, info["rom_name"], template,
        rich_fingerprint(cfg), override_name or "",
    )

    def build():
        activity = {
            "application_id": app_id,
            "name": override_name or render_text(template, values),
            "details": info["rom_name"],
        }
        rich_activity = render_activity_value(cfg["rich_activity"], values)
        for key, value in rich_activity.items():
            if key not in RESERVED_ACTIVITY_FIELDS:
                activity[key] = value
        return activity

    activity = cached_activity(cache_key, build)

    return {
        "exe": exe,
        "app_id": app_id,
        "game_name": info["rom_name"],
        "display_name": activity["name"],
        "pid": info["pid"],
        "activity": activity,
        "kind": "emulator",
        "emulator": info["emulator"],
        "rom_name": info["rom_name"],
        "rom_path": info["rom_path"],
    }


def choose_games(exes, active, overrides, index, cfg):
    ordered = [exe for exe in active if exe in exes]
    seen = set(ordered)
    ordered.extend(exe for exe in sorted(exes) if exe not in seen)
    games = []
    for exe in ordered:
        game = resolve_game(exe, exes[exe], overrides, index, cfg)
        if game:
            games.append(game)
            if cfg["max_activities"] and len(games) >= cfg["max_activities"]:
                break
    return games


def choose_game(exes, active, overrides, index):
    cfg = dict(DEFAULT_CONFIG)
    cfg["blocklist_ids"] = set()
    cfg["blocklist_names"] = set()
    cfg["rich_activity"] = {}
    game = choose_games(exes, [active] if active else [], overrides, index, cfg)
    if not game:
        return None
    selected = game[0]
    return selected["exe"], selected["app_id"], selected["display_name"], selected["pid"]


def activity_due(last_sent, now, interval):
    return last_sent is None or now - last_sent >= interval


def retry_delay(failures):
    if failures <= 0:
        return 0
    return min(RETRY_MAX_SECONDS, RETRY_INITIAL_SECONDS * 2 ** (failures - 1))


def should_send_activity(last_payload, payload, refresh_unchanged):
    # The periodic refresh doubles as a connection-liveness probe, so it stays
    # on by default; disabling it skips IPC traffic for byte-identical payloads.
    if refresh_unchanged:
        return True
    return last_payload is None or last_payload != payload
