# hydra-rpc source module: status (bundled into ./hydra-rpc by build.py)
def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Report Wine/Proton and emulator games to Discord through arRPC."
    )
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument(
        "--dry-run",
        action="store_true",
        help="detect and log games without sending Discord activity",
    )
    modes.add_argument(
        "--status",
        action="store_true",
        help="one-shot diagnostic: list detected processes and why each "
        "was or was not matched (no Discord traffic)",
    )
    modes.add_argument(
        "--validate-config",
        action="store_true",
        help="validate settings, load the game database, and check arRPC",
    )
    modes.add_argument(
        "--check-update",
        action="store_true",
        help="check GitHub for a newer hydra-rpc script",
    )
    modes.add_argument(
        "--update",
        action="store_true",
        help="download and atomically install the latest GitHub script",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="with --status: emit machine-readable JSON instead of a table",
    )
    return parser.parse_args(argv)


def validate_config(cfg, index):
    ok = True
    if not index:
        log("warning: game database is empty; games will need overrides", "warning")
    for emulator in sorted(cfg.get("emulator_application_ids", {})):
        app_id = cfg["emulator_application_ids"][emulator]
        if cfg.get("emulators_enabled") and not app_id:
            log(
                f"emulator '{emulator}' has no Discord application ID; it will be "
                f"skipped until emulator_application_ids.{emulator} is set "
                "(see https://discord.com/developers/applications)",
                "warning",
            )
        elif app_id and not is_valid_app_id(app_id):
            log(
                f"invalid application ID for emulator '{emulator}' ('{app_id}')",
                "warning",
            )
            ok = False
    for exe, (app_id, _name) in sorted(cfg.get("overrides", {}).items()):
        if not is_valid_app_id(app_id) or not str(app_id).strip().isdigit():
            log(f"invalid application ID in override for '{exe}' ('{app_id}')", "warning")
            ok = False
    socket_path = str(cfg.get("socket_path", "") or "")
    if socket_path:
        expanded = os.path.expanduser(os.path.expandvars(socket_path))
        if not os.path.exists(expanded):
            log(f"configured socket_path does not exist: {expanded}", "warning")
            ok = False
    base = runtime_dir(cfg) + "/discord-ipc"
    client = RPCClient(
        base,
        cfg.get("socket_path", ""),
        cfg["max_socket_attempts"],
    )
    socket_ok = False
    for path in client.candidate_paths():
        client.path = path
        try:
            probe = client._connect()
            probe.close()
            socket_ok = True
            socket_path_found = path
            break
        except OSError:
            continue
    if socket_ok:
        log(f"config valid; loaded {len(index)} executable mappings")
        log(f"arRPC socket ready: {socket_path_found}")
    else:
        log(
            f"config valid; loaded {len(index)} executable mappings, "
            "but no arRPC socket was found",
            "warning",
        )
        return False
    return ok


def describe_unmatched(exe, info, overrides, index, cfg):
    """Return (reason, app_id) explaining why a scanned process was not reported."""
    sources = info.get("sources", set()) if isinstance(info, dict) else set()
    if isinstance(info, dict) and info.get("kind") == "emulator":
        app_id = emulator_application_id(exe, info, cfg) or ""
        if not app_id:
            return ("missing-emulator-app-id: set emulator_application_ids."
                    + info["emulator"], "")
        if app_id in cfg["blocklist_ids"]:
            return ("blocklisted-app-id", app_id)
        if info["rom_name"].casefold() in cfg["blocklist_names"]:
            return ("blocklisted-name", app_id)
        if cfg["hydra_only"] and "hydra" not in sources:
            return ("hydra-filtered: no Hydra marker", app_id)
        return ("unknown", app_id)
    app = overrides.get(exe) or index.get(exe)
    if not app:
        return ("unknown: not in database (add an override)", "")
    app_id, game_name = app
    if app_id in cfg["blocklist_ids"]:
        return ("blocklisted-app-id", app_id)
    if game_name.casefold() in cfg["blocklist_names"]:
        return ("blocklisted-name", app_id)
    if cfg["hydra_only"] and "hydra" not in sources:
        return ("hydra-filtered: no Hydra marker", app_id)
    return ("unknown", app_id)


def collect_status(cfg, index, include_exe_blocklisted=True):
    """One-shot snapshot of what the watcher sees and why each entry matches."""
    exes = scan_processes(
        cfg["blocklist"],
        cfg["hydra_markers"],
        cfg["emulators_enabled"],
        cfg["hydra_only"],
    )
    exe_blocklisted = {}
    if include_exe_blocklisted:
        try:
            unfiltered = scan_processes(
                set(),
                cfg["hydra_markers"],
                cfg["emulators_enabled"],
                cfg["hydra_only"],
            )
            for exe, info in unfiltered.items():
                if exe not in exes and exe.lower() in cfg["blocklist"]:
                    exe_blocklisted[exe] = info
        except Exception:
            exe_blocklisted = {}
    entries = []
    for exe in sorted(set(exes) | set(exe_blocklisted)):
        if exe in exe_blocklisted:
            info = exe_blocklisted[exe]
            entries.append({
                "exe": exe,
                "pid": info.get("pid") if isinstance(info, dict) else info,
                "path": info.get("path", "") if isinstance(info, dict) else "",
                "kind": "emulator" if isinstance(info, dict) and info.get("kind") == "emulator" else "wine",
                "matched": False,
                "reason": "blocklisted-exe",
                "app_id": "",
                "display_name": "",
                "sources": sorted(info.get("sources", set())) if isinstance(info, dict) else [],
            })
            continue
        info = exes[exe]
        game = resolve_game(exe, info, cfg["overrides"], index, cfg)
        if game is not None:
            entries.append({
                "exe": exe,
                "pid": game["pid"],
                "path": info.get("path", "") if isinstance(info, dict) else "",
                "kind": game.get("kind", "wine"),
                "matched": True,
                "reason": "matched",
                "app_id": game["app_id"],
                "game_name": game["game_name"],
                "display_name": game["display_name"],
                "sources": sorted(info.get("sources", set())) if isinstance(info, dict) else [],
            })
        else:
            reason, app_id = describe_unmatched(exe, info, cfg["overrides"], index, cfg)
            kind = "emulator" if isinstance(info, dict) and info.get("kind") == "emulator" else "wine"
            display = ""
            if kind == "emulator" and isinstance(info, dict):
                display = f"{DEFAULT_EMULATOR_NAMES.get(info.get('emulator'), info.get('emulator', ''))}: {info.get('rom_name', '')}"
            entries.append({
                "exe": exe,
                "pid": info.get("pid") if isinstance(info, dict) else info,
                "path": info.get("path", "") if isinstance(info, dict) else "",
                "kind": kind,
                "matched": False,
                "reason": reason,
                "app_id": app_id,
                "display_name": display,
                "sources": sorted(info.get("sources", set())) if isinstance(info, dict) else [],
            })
    return entries


def format_status_table(entries):
    if not entries:
        return "status: no wine .exe or emulator ROM processes detected"
    lines = []
    header = f"{'EXE':<32} {'PID':>6}  {'MATCH':<7} REASON"
    lines.append(header)
    lines.append("-" * len(header))
    for entry in entries:
        exe = entry["exe"]
        if len(exe) > 32:
            exe = exe[:29] + "..."
        match = "yes" if entry["matched"] else "no"
        extra = ""
        if entry["matched"]:
            extra = f" app_id={entry['app_id']} '{entry['display_name']}'"
        elif entry["app_id"]:
            extra = f" app_id={entry['app_id']}"
        if entry["sources"]:
            extra += f" sources={','.join(entry['sources'])}"
        lines.append(f"{exe:<32} {entry['pid']:>6}  {match:<7} {entry['reason']}{extra}")
    matched = sum(1 for e in entries if e["matched"])
    lines.append(f"status: {matched}/{len(entries)} matched")
    return "\n".join(lines)


def print_status(cfg, index, as_json=False):
    if as_json:
        report = build_status_report(cfg, index)
        print(json.dumps(report, indent=2, sort_keys=True))
        return report
    entries = collect_status(cfg, index)
    print(format_status_table(entries))
    return entries


def probe_sockets(cfg):
    """Check arRPC socket candidates without sending any activity."""
    base = runtime_dir(cfg) + "/discord-ipc"
    client = RPCClient(
        base,
        cfg.get("socket_path", ""),
        cfg["max_socket_attempts"],
    )
    tried = []
    found = ""
    last_error = ""
    for path in client.candidate_paths():
        tried.append(path)
        client.path = path
        try:
            probe = client._connect()
            probe.close()
            found = path
            break
        except OSError as e:
            last_error = str(e)
    return {
        "found": bool(found),
        "path": found,
        "attempts_tried": tried,
        "last_error": last_error,
    }


STATUS_REPORT_VERSION = 1


def build_status_report(cfg, index):
    """Structured diagnostics: versioned top-level object for scripts/bug reports."""
    entries = collect_status(cfg, index)
    detected = []
    skipped = []
    for entry in entries:
        if entry["matched"]:
            if entry["kind"] == "emulator":
                matched_by = "emulator"
            elif entry["exe"] in cfg.get("overrides", {}):
                matched_by = "override"
            else:
                matched_by = "database"
            detected.append({
                "exe": entry["exe"],
                "app_id": entry["app_id"],
                "game_name": entry.get("game_name", ""),
                "display_name": entry.get("display_name", ""),
                "pid": entry["pid"],
                "kind": entry["kind"],
                "matched_by": matched_by,
                "sources": entry["sources"],
            })
        else:
            skipped.append({
                "exe": entry["exe"],
                "pid": entry["pid"],
                "kind": entry["kind"],
                "reason": entry["reason"],
                "app_id": entry["app_id"],
                "sources": entry["sources"],
            })
    cached_at = None
    cached_age_seconds = None
    try:
        if os.path.exists(DB_CACHE):
            cached_at = int(os.path.getmtime(DB_CACHE))
            cached_age_seconds = max(0, int(time.time()) - cached_at)
    except OSError:
        cached_at = None
        cached_age_seconds = None
    notes = []
    if _DB_INFO.get("stale_fallback"):
        notes.append(
            "database download failed; using stale cache from "
            f"{format_age(cached_age_seconds) if cached_age_seconds is not None else 'unknown age'} ago"
        )
    return {
        "version": STATUS_REPORT_VERSION,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "script": {"path": installed_script_path(), "version": SCRIPT_VERSION},
        "config_version": cfg.get("config_version", CONFIG_VERSION),
        "db": {
            "url": cfg.get("db_url", DB_URL),
            "cached_at": cached_at,
            "cached_age_seconds": cached_age_seconds,
            "entries": len(index),
        },
        "socket": probe_sockets(cfg),
        "detected": detected,
        "skipped": skipped,
        "notes": notes,
    }
