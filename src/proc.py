# hydra-rpc source module: proc (bundled into ./hydra-rpc by build.py)
def close_state(state):
    client = state["client"]
    if client.sock is not None:
        try:
            client.send("SET_ACTIVITY", {"activity": None, "pid": 0})
        except OSError:
            pass
    client.close()


def get_process_start_tick(pid):
    try:
        with open(f"/proc/{pid}/stat", "r") as stat_file:
            stat = stat_file.read()
        fields = stat.rsplit(")", 1)[1].split()
        return int(fields[19])
    except (OSError, ValueError, IndexError):
        return None


def session_start(sessions, game):
    record = sessions.get(game["exe"])
    now_ms = int(time.time() * 1000)
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

_MARKER_BYTES_CACHE = {}
_MARKER_BYTES_CACHE_MAX = 64


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


# Lowercase pattern tokens for every supported emulator. A Flatpak/bwrap
# process only needs full argv decoding when one of these appears in its raw
# cmdline (the Flatpak app id or wrapped binary always contains it); this
# skips the decode/join/casefold work for every unrelated sandboxed process.
_EMULATOR_TOKEN_BYTES = tuple(sorted({
    pattern.encode("utf-8")
    for settings in EMULATOR_PROFILES.values()
    for pattern in settings["patterns"]
}))


def has_hydra_marker(pid, raw_parts, markers, stats=None):
    byte_markers = _marker_bytes(markers)
    if not byte_markers:
        return False
    if _contains_any(b"\0".join(p for p in raw_parts if p).lower(), byte_markers):
        return True
    # Stream the environment in chunks instead of loading/decoding it whole.
    # Keep an overlap so a marker split across a chunk boundary is still found.
    overlap = max(len(marker) for marker in byte_markers)
    try:
        with open(f"/proc/{pid}/environ", "rb") as environment_file:
            if stats is not None:
                stats["environ_reads"] = stats.get("environ_reads", 0) + 1
            tail = b""
            while True:
                chunk = environment_file.read(_ENVIRON_CHUNK_BYTES)
                if not chunk:
                    return False
                if _contains_any((tail + chunk).lower(), byte_markers):
                    return True
                tail = chunk[-(overlap - 1):] if overlap > 1 else b""
    except OSError:
        return False


def _iter_cmdlines(stats=None):
    try:
        pids = os.listdir("/proc")
    except OSError:
        return
    for pid in pids:
        if not pid.isdigit():
            continue
        if stats is not None:
            stats["processes"] = stats.get("processes", 0) + 1
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                raw = f.read()
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
        parts = raw.split(b"\0")
        decoded = None
        hydra_state = None

        for part in parts:
            piece = part.rstrip()
            if len(piece) < 4 or piece[-4:].lower() != b".exe":
                continue
            arg = piece.decode("utf-8", "ignore")
            base = arg.replace("\\", "/").rsplit("/", 1)[-1].lower()
            if not base or base in blocklist:
                continue
            # Check the blocklist first: Wine service processes (services.exe,
            # explorer.exe, ...) are discarded either way, so don't pay for an
            # environ read just to mark them as Hydra-launched.
            if mark_hydra and hydra_state is None:
                hydra_state = has_hydra_marker(pid, parts, markers, stats)
            info = found.setdefault(
                base,
                {"pid": pid, "path": arg, "sources": set()},
            )
            if hydra_state:
                info["sources"].add("hydra")

        if not emulators_enabled:
            continue
        # Fast path: only decode/scan processes whose executable looks like a
        # known emulator (or a flatpak/bwrap wrapper, which needs a full scan).
        first = parts[0].decode("utf-8", "ignore").strip() if parts and parts[0] else ""
        base0 = first.replace("\\", "/").rsplit("/", 1)[-1].lower()
        profile = _EMULATOR_BINARY_MAP.get(base0)
        if profile is None:
            for token, candidate in _EMULATOR_BINARY_MAP.items():
                if token in base0:
                    profile = candidate
                    break
        if profile is None:
            if base0 not in ("flatpak", "bwrap"):
                continue
            # Only decode sandbox wrappers that could actually wrap an
            # emulator; unrelated Flatpak apps are the common case.
            if not _contains_any(raw.lower(), _EMULATOR_TOKEN_BYTES):
                continue
        if decoded is None:
            decoded = _decode_parts(parts)
        text = " ".join(decoded).casefold()
        profiles = (profile,) if profile is not None else tuple(EMULATOR_PROFILES)
        if stats is not None:
            stats["emulator_candidates"] = stats.get("emulator_candidates", 0) + 1
        for profile in profiles:
            settings = EMULATOR_PROFILES[profile]
            if not any(pattern in text for pattern in settings["patterns"]):
                continue
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
