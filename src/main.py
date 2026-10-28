# hydra-rpc source module: main (bundled into ./hydra-rpc by build.py)
def main(argv=None):
    args = parse_args(argv)
    if args.check_update:
        return update_script()
    if args.update:
        return update_script(install=True)
    global LOG_TO_STDERR
    if getattr(args, "json", False) and getattr(args, "status", False):
        LOG_TO_STDERR = True
    cfg = load_config()
    configure_logging(cfg)
    ensure_config(cfg)
    index = load_db(cfg)
    if not index:
        log("no detectable database available; games will need overrides", "error")
    log(f"loaded {len(index)} executable mappings")

    if args.validate_config:
        valid = validate_config(cfg, index)
        close_logging()
        return 0 if valid else 1

    if args.json and not args.status:
        print("--json requires --status", file=sys.stderr)
        close_logging()
        return 2

    if args.status:
        print_status(cfg, index, as_json=args.json)
        close_logging()
        return 0

    sessions = load_sessions()
    base = runtime_dir(cfg) + "/discord-ipc"
    states = {}
    sessions_dirty = False
    dry_signature = None
    seen_unknown = set()

    def stop(*_args):
        log("shutting down")
        for state in states.values():
            close_state(state)
        save_sessions(sessions)
        close_logging()
        sys.exit(0)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    # Deadline-based scheduling: scans that overrun (e.g. slow IPC) shift the
    # next deadline instead of stacking sleeps, so delays can't accumulate.
    next_scan = 0.0
    while True:
        try:
            now = time.monotonic()
            if now < next_scan:
                time.sleep(next_scan - now)
            scan_start = time.monotonic()
            stats = {}
            exes = scan_processes(
                cfg["blocklist"],
                cfg["hydra_markers"],
                cfg["emulators_enabled"],
                cfg["hydra_only"],
                stats,
            )
            scan_ms = (time.monotonic() - scan_start) * 1000
            if log_enabled("debug"):
                log(
                    f"scan: {stats.get('processes', 0)} procs, "
                    f"{stats.get('cmdlines', 0)} cmdlines, "
                    f"{stats.get('environ_reads', 0)} environs, "
                    f"{stats.get('environ_cache_hits', 0)} cache hits, "
                    f"{stats.get('emulator_candidates', 0)} emu candidates, "
                    f"{stats.get('wine_hits', 0)} wine + "
                    f"{stats.get('emulator_hits', 0)} emu hits "
                    f"in {scan_ms:.1f}ms",
                    "debug",
                )

            for exe in sorted(exes):
                if exe in seen_unknown:
                    continue
                info = exes[exe]
                if isinstance(info, dict) and info.get("kind") == "emulator":
                    configured = bool(emulator_application_id(exe, info, cfg))
                    message = (
                        f"emulator '{info['emulator']}' detected '{info['rom_name']}' "
                        f"but has no Discord application ID; configure "
                        f"emulator_application_ids.{info['emulator']}"
                    )
                else:
                    configured = bool(cfg["overrides"].get(exe) or index.get(exe))
                    message = f"unrecognized executable '{exe}' (add an override in {CONFIG_PATH})"
                if not configured:
                    seen_unknown.add(exe)
                    log(message, "warning")

            games = choose_games(exes, states.keys(), cfg["overrides"], index, cfg)

            if args.dry_run:
                signature = tuple(
                    (game["exe"], game["display_name"])
                    for game in games
                )
                if signature != dry_signature:
                    if games:
                        for game in games:
                            sources = ""
                            info = exes.get(game["exe"], {})
                            if isinstance(info, dict) and info.get("sources"):
                                sources = f" sources={','.join(sorted(info['sources']))}"
                            log(
                                f"dry-run: detected game: {game['display_name']} "
                                f"({game['exe']}) pid={game['pid']} "
                                f"app_id={game['app_id']}{sources} (would report)"
                            )
                    else:
                        log("dry-run: no supported games detected")
                        if exes:
                            # Log one line per unmatched process so users see why
                            # nothing matched instead of only "no supported games".
                            for exe in sorted(exes):
                                reason, app_id = describe_unmatched(
                                    exe, exes[exe], cfg["overrides"], index, cfg
                                )
                                suffix = f" app_id={app_id}" if app_id else ""
                                log(f"dry-run: skipped '{exe}': {reason}{suffix}")
                    dry_signature = signature
                next_scan = scan_start + cfg["poll_seconds"]
                continue

            selected = {game["exe"]: game for game in games}
            for exe in list(states):
                if exe not in selected:
                    log(f"game exited: {states[exe]['display_name']}; clearing presence")
                    close_state(states.pop(exe))
                    if sessions.pop(exe, None) is not None:
                        sessions_dirty = True

            for exe, game in selected.items():
                state = states.get(exe)
                if state is None:
                    start_ms = session_start(sessions, game)
                    previous_session = sessions.get(exe)
                    sessions[exe] = {
                        "start_ms": start_ms,
                        "pid": game["pid"],
                        "start_tick": get_process_start_tick(game["pid"]),
                    }
                    sessions_dirty |= previous_session != sessions[exe]
                    state = {
                        **game,
                        "start_ms": start_ms,
                        "client": RPCClient(
                            base,
                            cfg.get("socket_path", ""),
                            cfg["max_socket_attempts"],
                        ),
                        "last_activity_sent": None,
                        "last_activity_payload": None,
                        "connection_failures": 0,
                        "next_connection_attempt": 0.0,
                    }
                    states[exe] = state
                    log(
                        f"detected game: {game['display_name']} ({exe}) "
                        f"app_id={game['app_id']}"
                    )
                else:
                    metadata_changed = any(
                        state[key] != game[key]
                        for key in ("app_id", "display_name", "activity")
                    )
                    state.update(game)
                    if metadata_changed:
                        state["client"].close()
                        state["last_activity_sent"] = None
                        state["last_activity_payload"] = None
                        state["connection_failures"] = 0
                        state["next_connection_attempt"] = 0.0

                now = time.monotonic()
                if state["client"].sock is None:
                    if now < state["next_connection_attempt"]:
                        continue
                    try:
                        state["client"].connect(state["app_id"])
                    except OSError as e:
                        state["connection_failures"] += 1
                        delay = retry_delay(state["connection_failures"])
                        state["next_connection_attempt"] = time.monotonic() + delay
                        target = state["client"].path or f"{base}-0..{cfg['max_socket_attempts'] - 1}"
                        log(
                            f"could not connect to arRPC at {target} ({e}); "
                            f"retrying in {delay}s",
                            "warning",
                        )
                        continue
                    state["connection_failures"] = 0
                    state["next_connection_attempt"] = 0.0
                    state["last_activity_sent"] = None
                    state["last_activity_payload"] = None

                now = time.monotonic()
                if activity_due(
                    state["last_activity_sent"],
                    now,
                    cfg["activity_refresh_seconds"],
                ):
                    activity = dict(state["activity"])
                    activity["timestamps"] = {"start": state["start_ms"]}
                    if not should_send_activity(
                        state.get("last_activity_payload"),
                        activity,
                        cfg["refresh_unchanged_activities"],
                    ):
                        # Payload unchanged and periodic resends disabled:
                        # verify the connection is still alive with a tiny
                        # ping instead of a full activity resend.
                        if state["client"].ping():
                            state["last_activity_sent"] = now
                        else:
                            log("arRPC connection lost, will reconnect", "warning")
                            state["client"].close()
                            state["last_activity_payload"] = None
                            state["connection_failures"] += 1
                            delay = retry_delay(state["connection_failures"])
                            state["next_connection_attempt"] = time.monotonic() + delay
                    else:
                        try:
                            state["client"].send(
                                "SET_ACTIVITY",
                                {"activity": activity, "pid": state["pid"]},
                            )
                            state["last_activity_sent"] = now
                            state["last_activity_payload"] = activity
                        except OSError:
                            log("arRPC connection lost, will reconnect", "warning")
                            state["client"].close()
                            state["connection_failures"] += 1
                            delay = retry_delay(state["connection_failures"])
                            state["next_connection_attempt"] = time.monotonic() + delay

            if sessions_dirty:
                save_sessions(sessions)
                sessions_dirty = False
            next_scan = scan_start + cfg["poll_seconds"]

        except Exception as e:
            log(f"unexpected error: {e}", "error")
            next_scan = time.monotonic() + cfg["poll_seconds"]


if __name__ == "__main__":
    raise SystemExit(main())
