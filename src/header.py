# hydra-rpc source module: header (bundled into ./hydra-rpc by build.py)
#!/usr/bin/env python3
"""hydra-rpc: report Wine/Proton and emulator games to Discord via arRPC.

Scans /proc for Windows executables and supported native emulators, maps games to
Discord application ids, and pushes Rich Presence through arRPC so it shows up in
Vesktop or any other arRPC client.
"""

import argparse
import base64
import copy
import fcntl
import hashlib
import json
import math
import os
import re
import signal
import socket
import struct
import sys
import tempfile
import time
import urllib.error
import urllib.request

CONFIG_DIR = os.path.join(
    os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")), "hydra-rpc"
)
CACHE_DIR = os.path.join(
    os.environ.get("XDG_CACHE_HOME", os.path.expanduser("~/.cache")), "hydra-rpc"
)
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")
DB_CACHE = os.path.join(CACHE_DIR, "detectable.json")
SESSION_CACHE = os.path.join(CACHE_DIR, "sessions.json")
LOCK_PATH = os.path.join(CACHE_DIR, "instance.lock")

DB_URL = "https://discord.com/api/v9/applications/detectable"
UPDATE_URL = "https://raw.githubusercontent.com/skypetroller/hydra-rpc/main/hydra-rpc"
UPDATE_SHA_URL = UPDATE_URL + ".sha256"
UPDATE_SIG_URL = UPDATE_SHA_URL + ".sig"
# Ed25519 public keys trusted to sign hydra-rpc.sha256.sig (hex). The updater
# refuses an update whose signature does not verify against one of these; when
# rotating the key, ship a release listing both old and new, then switch.
# Fingerprint of the first key (sha256, first 16 hex chars): 1215387a4796f96d
UPDATE_PUBLIC_KEYS = (
    "0b7a4375377afe532cad965d6cee8d4e7e51bd6b93a8b7780d8ce69b254bf1b0",
)
CONFIG_VERSION = 1
SCRIPT_VERSION = "1.2.2"
UPDATE_TIMEOUT = 30
UPDATE_ATTEMPTS = 2
UPDATE_RETRY_DELAY = 1
# Response-size ceilings: the bundle is ~80 KiB, checksum/signature files are
# tiny, and the detectable-applications database is ~13 MiB.
MAX_UPDATE_BYTES = 1 << 20
MAX_UPDATE_META_BYTES = 16 << 10
MAX_DB_BYTES = 64 << 20
# Long-running watchers refresh the game database in the background-ish loop;
# keep the floor high enough that db_ttl_seconds=0 cannot hammer the endpoint.
DB_REFRESH_MIN_SECONDS = 3600
SOCKET_CONNECT_TIMEOUT = 1
HANDSHAKE_READ_TIMEOUT = 1
ACTIVITY_ACK_TIMEOUT = 0.5
PING_PROBE_TIMEOUT = 1
# Largest IPC payload we will accept; arRPC frames are small JSON blobs, so
# anything bigger indicates a malformed or misbehaving server.
MAX_IPC_FRAME_SIZE = 1 * 1024 * 1024
RETRY_INITIAL_SECONDS = 1
RETRY_MAX_SECONDS = 60
DEFAULT_SOCKET_ATTEMPTS = 3
MAX_SOCKET_PATHS = 10
DEFAULT_MAX_ACTIVITIES = 0
MAX_ACTIVITIES = 10
DEFAULT_HYDRA_MARKERS = {"hydralauncher", "/opt/hydra/"}
DEFAULT_EMULATOR_APPLICATION_IDS = {
    "retroarch": "505497615748694018",
    "pcsx2": "505494914872770583",
    "duckstation": "",
    "rpcs3": "",
    "dolphin": "356943187589201930",
    "ppsspp": "",
}
DEFAULT_EMULATOR_NAMES = {
    "retroarch": "RetroArch",
    "duckstation": "DuckStation",
    "pcsx2": "PCSX2",
    "rpcs3": "RPCS3",
    "dolphin": "Dolphin",
    "ppsspp": "PPSSPP",
}
EMULATOR_PROFILES = {
    "retroarch": {
        "binaries": ("retroarch",),
        "flatpak_ids": ("org.libretro.RetroArch",),
        "extensions": {
            ".7z", ".bin", ".cdi", ".chd", ".cia", ".cue", ".fds", ".gb", ".gba",
            ".gbc", ".gdi", ".gen", ".iso", ".m3u", ".md", ".n64", ".nds", ".nes",
            ".pce", ".pbp", ".rom", ".rvz", ".sfc", ".smc", ".sms", ".srm", ".v64",
            ".wad", ".wbfs", ".xci", ".z64", ".zip",
        },
    },
    "duckstation": {
        "binaries": ("duckstation", "duckstation-qt", "duckstation-nogui", "duckstation-sdl"),
        "flatpak_ids": ("org.duckstation.DuckStation",),
        "extensions": {".bin", ".chd", ".cue", ".ecm", ".img", ".iso", ".m3u", ".pbp"},
    },
    "pcsx2": {
        "binaries": ("pcsx2", "pcsx2-qt"),
        "flatpak_ids": ("net.pcsx2.PCSX2",),
        "extensions": {".bin", ".chd", ".cso", ".cue", ".gz", ".iso", ".m3u"},
    },
    "rpcs3": {
        "binaries": ("rpcs3",),
        "flatpak_ids": ("net.rpcs3.RPCS3",),
        "extensions": {".bin", ".elf", ".iso", ".pkg", ".self"},
    },
    "dolphin": {
        # KDE's file manager is also called "dolphin"; only the emulator's
        # actual binary names are accepted here.
        "binaries": (
            "dolphin-emu", "dolphin-emu-nogui", "dolphin-emu-qt",
            "dolphin-emu-qt2",
        ),
        "flatpak_ids": ("org.DolphinEmu.dolphin-emu",),
        "extensions": {
            ".ciso", ".dol", ".elf", ".gcm", ".iso", ".rvz", ".wad", ".wbfs", ".wia",
        },
    },
    "ppsspp": {
        "binaries": ("ppsspp", "ppsspp-qt", "ppsspp-sdl"),
        "flatpak_ids": ("org.ppsspp.PPSSPP",),
        "extensions": {".cso", ".elf", ".iso", ".pbp", ".prx"},
    },
}
# Fast path: exact executable basenames and Flatpak app ids. Generic strings
# such as "dolphin" are deliberately absent so unrelated desktop apps cannot
# be mistaken for emulators.
_EMULATOR_BINARY_MAP = {}
_EMULATOR_FLATPAK_MAP = {}
for _profile, _settings in EMULATOR_PROFILES.items():
    for _binary in _settings["binaries"]:
        _EMULATOR_BINARY_MAP.setdefault(_binary.casefold(), _profile)
    for _app_id in _settings["flatpak_ids"]:
        _EMULATOR_FLATPAK_MAP.setdefault(_app_id.casefold(), _profile)
_EMULATOR_TOKEN_BYTES = tuple(sorted({
    token.casefold().encode("utf-8")
    for settings in EMULATOR_PROFILES.values()
    for token in (*settings["binaries"], *settings["flatpak_ids"])
}))
LOG_LEVELS = {"debug": 10, "info": 20, "warning": 30, "error": 40}
CURRENT_LOG_LEVEL = LOG_LEVELS["info"]
LOG_FILE_HANDLE = None
LOG_TO_STDERR = False

# Discords IPC framing: [type:int32][length:int32][json bytes]
IPC_HANDSHAKE = 0
IPC_FRAME = 1
IPC_CLOSE = 2
IPC_PING = 3
IPC_PONG = 4

# Windows processes we should never report as "games".
DEFAULT_BLOCKLIST = {
    "winedevice.exe",
    "services.exe",
    "plugplay.exe",
    "explorer.exe",
    "svchost.exe",
    "rundll32.exe",
    "start.exe",
    "wineboot.exe",
    "winecfg.exe",
    "control.exe",
    "taskmgr.exe",
    "regedit.exe",
    "uninstaller.exe",
    "iexplore.exe",
    "notepad.exe",
    "cmd.exe",
    "conhost.exe",
    "rpcss.exe",
    "wininit.exe",
    "winlogon.exe",
    "csrss.exe",
    "umu.exe",
}

DEFAULT_CONFIG = {
    "config_version": CONFIG_VERSION,
    "poll_seconds": 5,
    "activity_refresh_seconds": 60,
    "refresh_unchanged_activities": True,
    "db_url": DB_URL,
    "db_ttl_seconds": 7 * 24 * 3600,
    "socket_dir": "",
    "socket_path": "",
    "max_socket_attempts": DEFAULT_SOCKET_ATTEMPTS,
    "max_activities": DEFAULT_MAX_ACTIVITIES,
    "hydra_only": False,
    "hydra_markers": sorted(DEFAULT_HYDRA_MARKERS),
    "emulators_enabled": False,
    "emulator_application_ids": dict(DEFAULT_EMULATOR_APPLICATION_IDS),
    "emulator_overrides": {},
    "blocklist_ids": [],
    "blocklist_names": [],
    "activity_template": "{game_name}",
    "emulator_activity_template": "{emulator_name}",
    "rich_activity": {},
    "log_file": "",
    "log_level": "info",
    "blocklist": sorted(DEFAULT_BLOCKLIST),
    "overrides": {},
}
