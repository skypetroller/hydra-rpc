# hydra-rpc source module: proc (bundled into ./hydra-rpc by build.py)
def close_state(state):
    client = state["client"]
    if client.sock is not None:
        try:
            client.send("SET_ACTIVITY", {"activity": None, "pid": 0})
        except (OSError, ValueError):
            pass
    client.close()


def get_process_start_tick(pid):
    try:
        # Raw reads avoid buffering overhead; stat is a single short line.
        with open(f"/proc/{pid}/stat", "rb", buffering=0) as stat_file:
            stat = stat_file.read(4096)
        fields = stat.rsplit(b")", 1)[1].split()
        return int(fields[19])
    except (OSError, ValueError, IndexError):
        return None


def session_start(sessions, game, current_tick=None):
    record = sessions.get(game["exe"])
    now_ms = int(time.time() * 1000)
    if current_tick is None:
        current_tick = get_process_start_tick(game["pid"])
    if isinstance(record, dict):
        try:
            start_ms = int(record["start_ms"])
            start_tick = int(record["start_tick"])
            if (
                current_tick is not None
                and int(record["pid"]) == game["pid"]
                and start_tick == current_tick
                and 0 < start_ms <= now_ms
            ):
                return start_ms
        except (KeyError, TypeError, ValueError):
            pass
    return now_ms


def _decode_parts(raw_parts):
    return [item.decode("utf-8", "ignore").strip() for item in raw_parts if item]


_ENVIRON_CHUNK_BYTES = 65536
_WINE_LOADER_NAMES = {
    "wine", "wine64", "wine-preloader", "wine64-preloader", "wineserver",
    "proton", "umu-run", "pressure-vessel", "steam-launch-wrapper",
}
_WINE_ENV_NEEDLES = (
    b"wineprefix=",
    b"steam_compat_data_path=",
    b"protonpath=",
    b"umu_id=",
    b"gameid=umu-",
)

_MARKER_BYTES_CACHE = {}
_MARKER_BYTES_CACHE_MAX = 64

# Marker lookups are repeated every poll for the same long-lived Wine PIDs.
# Remember the answer per (pid, start tick, marker set) so steady-state scans
# skip the environ read entirely. A changed start tick means PID reuse, which
# invalidates the entry. Unreadable environs are never cached.
_MARKER_RESULT_CACHE = {}
_MARKER_RESULT_CACHE_MAX = 512


def _marker_bytes(markers):
    # Marker sets are fixed per run, so encode them once instead of per process.
    key = frozenset(markers)
    cached = _MARKER_BYTES_CACHE.get(key)
    if cached is None:
        cached = tuple(m.encode("utf-8", "ignore").lower() for m in markers if m)
        if len(_MARKER_BYTES_CACHE) >= _MARKER_BYTES_CACHE_MAX:
            _MARKER_BYTES_CACHE.clear()
        _MARKER_BYTES_CACHE[key] = cached
    return cached


def _contains_any(blob, needles):
    return any(needle in blob for needle in needles)


def _basename(value):
    return value.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1].casefold()


def process_uid(pid):
    """Owner of /proc/<pid>.

    None means the process disappeared between listdir() and here; -1 means
    /proc could not be inspected (for example hidepid), which is treated as
    "do not trust this process".
    """
    try:
        return os.stat(f"/proc/{pid}").st_uid
    except FileNotFoundError:
        return None
    except OSError:
        return -1


def process_exe_basename(pid):
    try:
        return _basename(os.readlink(f"/proc/{pid}/exe"))
    except OSError:
        return ""


def wine_process_evidence(pid, parts, stats=None):
    """True when a .exe argument belongs to a Wine/Proton/UMU process.

    The check is deliberately conservative: the game process usually exposes
    its Windows executable as argv[0], while a loader (wine, proton, umu-run,
    pressure-vessel) appears in the first few arguments. If neither is true,
    look for a Wine/Proton environment before falling back to "not a game".
    """
    first = parts[0].decode("utf-8", "ignore").strip() if parts else ""
    if _basename(first).endswith(".exe"):
        return True
    for part in parts[:3]:
        base = _basename(part.decode("utf-8", "ignore").strip())
        if base in _WINE_LOADER_NAMES or base.startswith("wine") or base.startswith("proton"):
            return True
    exe_base = process_exe_basename(pid)
    if exe_base and ("wine" in exe_base or "proton" in exe_base):
        return True
    if stats is not None:
        stats["wine_env_checks"] = stats.get("wine_env_checks", 0) + 1
    try:
        with open(f"/proc/{pid}/environ", "rb", buffering=0) as environment_file:
            if stats is not None:
                stats["environ_reads"] = stats.get("environ_reads", 0) + 1
            remaining = 1 << 20
            tail = b""
            while remaining > 0:
                chunk = environment_file.read(min(_ENVIRON_CHUNK_BYTES, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                blob = (tail + chunk).lower()
                if _contains_any(blob, _WINE_ENV_NEEDLES):
                    return True
                tail = chunk[-64:]
    except OSError:
        pass
    return False


def has_hydra_marker(pid, raw_parts, markers, stats=None):
    byte_markers = _marker_bytes(markers)
    if not byte_markers:
        return False
    if _contains_any(b"\0".join(p for p in raw_parts if p).lower(), byte_markers):
        return True
    marker_key = frozenset(markers)
    start_tick = get_process_start_tick(pid)
    cache_key = (pid, start_tick, marker_key) if start_tick is not None else None
    if cache_key is not None:
        cached = _MARKER_RESULT_CACHE.get(cache_key)
        if cached is not None:
            if stats is not None:
                stats["environ_cache_hits"] = stats.get("environ_cache_hits", 0) + 1
            return cached
    # Stream the environment in chunks instead of loading/decoding it whole.
    # Keep an overlap so a marker split across a chunk boundary is still found.
    overlap = max(len(marker) for marker in byte_markers)
    found = False
    try:
        with open(f"/proc/{pid}/environ", "rb", buffering=0) as environment_file:
            if stats is not None:
                stats["environ_reads"] = stats.get("environ_reads", 0) + 1
            tail = b""
            while True:
                chunk = environment_file.read(_ENVIRON_CHUNK_BYTES)
                if not chunk:
                    break
                if _contains_any((tail + chunk).lower(), byte_markers):
                    found = True
                    break
                tail = chunk[-(overlap - 1):] if overlap > 1 else b""
    except OSError:
        return False
    if cache_key is not None:
        if len(_MARKER_RESULT_CACHE) >= _MARKER_RESULT_CACHE_MAX:
            _MARKER_RESULT_CACHE.clear()
        _MARKER_RESULT_CACHE[cache_key] = found
    return found


def _iter_cmdlines(stats=None):
    try:
        pids = os.listdir("/proc")
    except OSError:
        return
    my_uid = os.getuid()
    for pid in pids:
        if not pid.isdigit():
            continue
        if stats is not None:
            stats["processes"] = stats.get("processes", 0) + 1
        uid = process_uid(pid)
        if uid == -1:
            if stats is not None:
                stats["uid_unreadable"] = stats.get("uid_unreadable", 0) + 1
            continue
        if uid is not None and uid != my_uid:
            if stats is not None:
                stats["uid_skipped"] = stats.get("uid_skipped", 0) + 1
            continue
        try:
            # Raw, chunked read: cmdline is usually well under one chunk, and
            # unbuffered file objects are measurably cheaper than buffered ones.
            with open(f"/proc/{pid}/cmdline", "rb", buffering=0) as f:
                raw = f.read(_ENVIRON_CHUNK_BYTES)
                if len(raw) == _ENVIRON_CHUNK_BYTES:
                    chunks = [raw]
                    while True:
                        chunk = f.read(_ENVIRON_CHUNK_BYTES)
                        if not chunk:
                            break
                        chunks.append(chunk)
                    raw = b"".join(chunks)
        except OSError:
            continue
        if not raw:
            continue
        if stats is not None:
            stats["cmdlines"] = stats.get("cmdlines", 0) + 1
        yield int(pid), raw


def scan_processes(blocklist, hydra_markers=None, emulators_enabled=False, mark_hydra=True, stats=None):
    markers = set(hydra_markers or DEFAULT_HYDRA_MARKERS)
    found = {}
    for pid, raw in _iter_cmdlines(stats):
        raw_lower = raw.lower()
        has_exe = b".exe" in raw_lower
        # Without emulator detection a cmdline with no ".exe" token cannot
        # contribute anything, so skip splitting it into arguments.
        if not emulators_enabled and not has_exe:
            continue
        parts = raw.split(b"\0")
        decoded = None
        hydra_state = None

        if has_exe:
            candidates = []
            for part in parts:
                piece = part.rstrip()
                if len(piece) < 4 or piece[-4:].lower() != b".exe":
                    continue
                arg = piece.decode("utf-8", "ignore")
                for prefix in ("--exec=", "-exec="):
                    if arg.lower().startswith(prefix):
                        arg = arg[len(prefix):]
                        break
                base = _basename(arg)
                if not base or base in blocklist:
                    continue
                candidates.append((base, arg))
            if candidates:
                # Other users' processes are filtered in _iter_cmdlines. Here
                # require positive Wine/Proton evidence so an editor opening
                # "Game.exe" cannot masquerade as the game.
                if wine_process_evidence(pid, parts, stats):
                    # Check the blocklist first: Wine service processes
                    # (services.exe, explorer.exe, ...) are discarded either
                    # way, so don't pay for an environ read just to mark them
                    # as Hydra-launched.
                    if mark_hydra:
                        hydra_state = has_hydra_marker(pid, parts, markers, stats)
                    for base, arg in candidates:
                        info = found.setdefault(
                            base,
                            {"pid": pid, "path": arg, "sources": set()},
                        )
                        if hydra_state:
                            info["sources"].add("hydra")
                elif stats is not None:
                    stats["wine_rejected"] = stats.get("wine_rejected", 0) + 1

        if not emulators_enabled:
            continue
        # Fast path: only decode/scan processes whose executable looks like a
        # known emulator (or a Flatpak/bwrap wrapper containing an exact
        # emulator binary/app-id token). Generic words like "dolphin" are
        # deliberately not enough: KDE's file manager is also named dolphin.
        first = parts[0].decode("utf-8", "ignore").strip() if parts and parts[0] else ""
        base0 = _basename(first)
        exe_base = process_exe_basename(pid)
        profile = (
            _EMULATOR_BINARY_MAP.get(exe_base)
            or _EMULATOR_BINARY_MAP.get(base0)
        )
        if profile is None:
            for candidate in (exe_base, base0):
                if not candidate:
                    continue
                for token, candidate_profile in _EMULATOR_BINARY_MAP.items():
                    if token in candidate:
                        profile = candidate_profile
                        break
                if profile is not None:
                    break
        if profile is None and not _contains_any(raw_lower, _EMULATOR_TOKEN_BYTES):
            continue
        if decoded is None:
            decoded = _decode_parts(parts)
        if profile is None:
            # A launcher/sandbox in argv[0] (or a wrapper script) may carry
            # the real emulator later in argv. Match only exact binary names
            # or exact Flatpak app ids there.
            for value in decoded:
                token = normalise_argument(value)
                base = _basename(token)
                if base in _EMULATOR_BINARY_MAP:
                    profile = _EMULATOR_BINARY_MAP[base]
                    break
                for app_id, candidate_profile in _EMULATOR_FLATPAK_MAP.items():
                    if app_id in token.casefold():
                        profile = candidate_profile
                        break
                if profile is not None:
                    break
        if profile is None:
            for value in decoded:
                base = _basename(normalise_argument(value))
                for binary, candidate_profile in _EMULATOR_BINARY_MAP.items():
                    if binary in base:
                        profile = candidate_profile
                        break
                if profile is not None:
                    break
        if profile is None:
            continue
        settings = EMULATOR_PROFILES[profile]
        text = " ".join(decoded).casefold()
        tokens = (*settings["binaries"], *settings["flatpak_ids"])
        if not any(token.casefold() in text for token in tokens):
            continue
        if stats is not None:
            stats["emulator_candidates"] = stats.get("emulator_candidates", 0) + 1
        rom_path = extract_emulator_rom(profile, decoded)
        if not rom_path:
            continue
        if mark_hydra and hydra_state is None:
            hydra_state = has_hydra_marker(pid, parts, markers, stats)
        key = f"emulator:{profile}:{rom_path.casefold()}"
        info = found.setdefault(
            key,
            {
                "kind": "emulator",
                "emulator": profile,
                "rom_path": rom_path,
                "rom_name": clean_rom_name(profile, rom_path),
                "pid": pid,
                "path": rom_path,
                "sources": set(),
            },
        )
        if hydra_state:
            info["sources"].add("hydra")
    if stats is not None:
        stats["wine_hits"] = sum(1 for v in found.values() if v.get("kind") != "emulator")
        stats["emulator_hits"] = sum(1 for v in found.values() if v.get("kind") == "emulator")
    return found


def normalise_argument(value):
    return value.strip().strip('"').strip("'").replace("\\", "/")


def extract_emulator_rom(profile, parts):
    extensions = EMULATOR_PROFILES[profile]["extensions"]
    for value in reversed(parts[1:]):
        path = normalise_argument(value)
        if not path:
            continue
        lowered = path.casefold()
        for prefix in ("--exec=", "-exec=", "--rom=", "-rom="):
            if lowered.startswith(prefix):
                path = path[len(prefix):]
                break
        if not path or path.startswith("-"):
            continue
        upper = path.upper()
        if profile == "rpcs3" and (
            upper.endswith("/EBOOT.BIN")
            or "/PS3_GAME/" in upper
            or "/DEV_HDD0/GAME/" in upper
        ):
            return path
        extension = os.path.splitext(path)[1].casefold()
        if extension in extensions:
            return path
    return None


def clean_rom_name(profile, rom_path):
    path = normalise_argument(rom_path).rstrip("/")
    upper = path.upper()
    marker = "/PS3_GAME/"
    if profile == "rpcs3" and marker in upper:
        path = path[:upper.index(marker)].rstrip("/")
    name = os.path.basename(path) or DEFAULT_EMULATOR_NAMES[profile]
    stem, extension = os.path.splitext(name)
    if extension:
        name = stem
    name = name.replace("_", " ").strip()
    return name[:128] or DEFAULT_EMULATOR_NAMES[profile]


def detect_emulators(hydra_markers=None):
    found = scan_processes(set(), hydra_markers, emulators_enabled=True)
    return {key: info for key, info in found.items() if info.get("kind") == "emulator"}


def detect_exes(blocklist, hydra_markers=None):
    found = scan_processes(blocklist, hydra_markers)
    return {key: info for key, info in found.items() if info.get("kind") != "emulator"}
