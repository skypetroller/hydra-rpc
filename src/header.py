# hydra-rpc source module: header (bundled into ./hydra-rpc by build.py)
#!/usr/bin/env python3
"""hydra-rpc: report Wine/Proton and emulator games to Discord via arRPC.

Scans /proc for Windows executables and supported native emulators, maps games to
Discord application ids, and pushes Rich Presence through arRPC so it shows up in
Vesktop or any other arRPC client.
"""

import argparse
import copy
import hashlib
import json
import math
import os
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

DB_URL = "https://discord.com/api/v9/applications/detectable"
UPDATE_URL = "https://raw.githubusercontent.com/skypetroller/hydra-rpc/main/hydra-rpc"
UPDATE_SHA_URL = UPDATE_URL + ".sha256"
CONFIG_VERSION = 1
SCRIPT_VERSION = "1.0.1"
UPDATE_TIMEOUT = 30
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
        "patterns": ("retroarch", "org.libretro.retroarch"),
        "extensions": {
            ".7z", ".bin", ".cdi", ".chd", ".cia", ".cue", ".fds", ".gb", ".gba",
            ".gbc", ".gdi", ".gen", ".iso", ".m3u", ".md", ".n64", ".nds", ".nes",
            ".pce", ".pbp", ".rom", ".rvz", ".sfc", ".smc", ".sms", ".srm", ".v64",
            ".wad", ".wbfs", ".xci", ".z64", ".zip",
        },
    },
    "duckstation": {
        "patterns": ("duckstation", "org.duckstation.duckstation"),
        "extensions": {".bin", ".chd", ".cue", ".ecm", ".img", ".iso", ".m3u", ".pbp"},
    },
    "pcsx2": {
        "patterns": ("pcsx2", "net.pcsx2.pcsx2"),
        "extensions": {".bin", ".chd", ".cso", ".cue", ".gz", ".iso", ".m3u"},
    },
    "rpcs3": {
        "patterns": ("rpcs3", "net.rpcs3.rpcs3"),
        "extensions": {".bin", ".elf", ".iso", ".pkg", ".self"},
    },
    "dolphin": {
        "patterns": ("dolphin", "dolphin-emu", "org.dolphinemu.dolphin-emu"),
        "extensions": {
            ".ciso", ".dol", ".elf", ".gcm", ".iso", ".rvz", ".wad", ".wbfs", ".wia",
        },
    },
    "ppsspp": {
        "patterns": ("ppsspp", "ppsspp-qt", "ppsspp-sdl", "org.ppsspp.ppsspp"),
        "extensions": {".cso", ".elf", ".iso", ".pbp", ".prx"},
    },
}
# Fast path: map an executable basename (or Flatpak app id token) to its
# emulator profile, so non-emulator processes skip full pattern scanning.
_EMULATOR_BINARY_MAP = {}
for _profile, _settings in EMULATOR_PROFILES.items():
    for _pattern in _settings["patterns"]:
        _EMULATOR_BINARY_MAP.setdefault(_pattern, _profile)
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
    "wineserver",
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
