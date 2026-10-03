import argparse
import atexit
import base64
import importlib.util
import json
import math
import os
import queue
import random
import re
import secrets
import shutil
import socket
import string
import struct
import subprocess
import sys
import threading
import time
import warnings
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from functools import lru_cache
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

import requests
from requests.adapters import HTTPAdapter


_CHILD_PROCESS_LOCK = threading.Lock()
_CHILD_PROCESSES: dict[int, subprocess.Popen] = {}


def _child_creationflags() -> int:
    flags = int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if os.name == "nt":
        flags |= int(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
    return flags


def _track_child_process(proc: subprocess.Popen) -> None:
    if proc.pid is None:
        return
    with _CHILD_PROCESS_LOCK:
        _CHILD_PROCESSES[int(proc.pid)] = proc


def _untrack_child_process(proc: subprocess.Popen | None) -> None:
    if proc is None or proc.pid is None:
        return
    with _CHILD_PROCESS_LOCK:
        _CHILD_PROCESSES.pop(int(proc.pid), None)


def _kill_process_tree(proc: subprocess.Popen, timeout: float = 2.0) -> None:
    if proc.poll() is not None:
        _untrack_child_process(proc)
        return
    if os.name == "nt" and proc.pid is not None:
        try:
            subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=max(0.5, timeout),
                check=False,
                creationflags=int(getattr(subprocess, "CREATE_NO_WINDOW", 0)),
            )
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
    else:
        try:
            proc.kill()
        except Exception:
            pass
    try:
        proc.wait(timeout=max(0.2, timeout))
    except Exception:
        pass
    _untrack_child_process(proc)


def shutdown_tracked_child_processes() -> None:
    with _CHILD_PROCESS_LOCK:
        procs = list(_CHILD_PROCESSES.values())
        _CHILD_PROCESSES.clear()
    for proc in procs:
        _kill_process_tree(proc)


CAPTCHA_ID = "36c22ce4f03142f6b3e325c7da3db0e3"
REFERER = "https://dun.163.com/trial/jigsaw"
ZONE_ID = "CN31"
DT = "XhH8/oRhFwtBAhUQEAOSud0wyGi6Meq6"
VERSION = "2.28.5"
LOAD_VERSION = "2.5.4"
RUN_ENV = "10"
WIDTH = 320
CAPTCHA_TYPE = "2"
DEFAULT_FP = ""
FP_GLOBAL_NAME = "gdxidpyhxde"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36"
CHROME_DEBUG_HOST = "127.0.0.1"
CHROME_DEBUG_PORT = 9222
CHROME_DEBUG_TIMEOUT = 2.0
FP_PROVIDER = "local"
NODE_PATH = "auto"
LOCAL_FP_TIMEOUT = 8.0
LOCAL_ENV_FP = "16920068974867,37092795074960"
LOCAL_FP_VM_TIMEOUT_MS = 5000
HTTP_POOL_CONNECTIONS = 32
HTTP_POOL_MAXSIZE = 64


def default_recognizer_worker_count() -> int:
    configured = os.environ.get("YIDUN_RECOGNIZER_WORKERS", "").strip()
    if configured:
        try:
            return max(1, min(8, int(configured)))
        except Exception:
            pass
    return min(2, max(1, (os.cpu_count() or 4) // 2))


def default_local_fp_worker_count() -> int:
    configured = os.environ.get("YIDUN_FP_WORKERS", "").strip()
    if configured:
        try:
            return max(1, min(64, int(configured)))
        except Exception:
            pass
    return min(8, max(2, os.cpu_count() or 4))


DEFAULT_LOCAL_FP_WORKERS = default_local_fp_worker_count()
DEFAULT_RECOGNIZER_WORKERS = default_recognizer_worker_count()


def resource_root() -> Path:
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS)
    return Path(__file__).resolve().parent


def runtime_root() -> Path:
    """Return the writable runtime directory, defaulting to AstrBot temp."""
    configured = os.environ.get("ASTRBOT_SOLVER_RUNTIME")
    if configured:
        root = Path(configured)
    else:
        from astrbot.core.utils.astrbot_path import get_astrbot_temp_path

        root = Path(get_astrbot_temp_path()) / "wangshangliao_solver"
    root.mkdir(parents=True, exist_ok=True)
    return root


ROOT = resource_root()
RUN_ROOT = runtime_root()
RUN_REVERSE_DIR = RUN_ROOT / "debug_runs"


def asset_dir() -> Path:
    candidates = [ROOT / "网易_滑块增强版_资源", ROOT / "netease_slide_pro"]
    if not getattr(sys, "frozen", False):
        candidates.append(ROOT.parent / "模型")
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return ROOT / "网易_滑块增强版_资源"


ASSET_DIR = asset_dir()
IR_TEMPLATE_PATH = ASSET_DIR / "ir_request_template.json"
TRACK_TEMPLATE_PATH = ASSET_DIR / "track_template.json"
LOCAL_FP_VM_PATH = ROOT / "网易_滑块增强版_fp_vm.js"
LOCAL_FP_CORE_CANDIDATES = [
    ROOT / "research" / "core-optimi.browser.current.min.js",
]
SAMPLE_NUM = 50
TRUSTED_EVENT_CODE = 2
SUBMIT_WAIT_BUFFER_MS = 60
FIRST_MOVE_MIN_MS = 35
MOVE_INTERVAL_MIN_MS = 11
TRACK_TIME_SCALE_MIN = 0.78
TRACK_TIME_SCALE_MAX = 0.90


def _candidate_path_from_env(env_name: str) -> Path | None:
    base = os.environ.get(env_name, "").strip()
    return (Path(base) / "nodejs" / "node.exe") if base else None


def resolve_node_path(node_path: str | None = None) -> str:
    requested = str(node_path or "").strip().strip('"').strip("'")
    attempted: list[str] = []

    def add_candidate(value: str | Path | None) -> None:
        if value is None:
            return
        text = str(value).strip()
        if text and text not in attempted:
            attempted.append(text)

    def existing_file(value: str | Path) -> str | None:
        text = os.path.expandvars(os.path.expanduser(str(value).strip()))
        if not text:
            return None
        try:
            path = Path(text)
        except Exception:
            return None
        return str(path) if path.is_file() else None

    if requested and requested.lower() not in {"auto", "default"}:
        add_candidate(requested)
        found = existing_file(requested)
        if found:
            return found
        found = shutil.which(requested)
        if found:
            return found
        name = Path(requested).name
        if name and name != requested:
            found = shutil.which(name)
            if found:
                return found

    for candidate in [
        ROOT / "nodejs" / "node.exe",
        RUN_ROOT / "nodejs" / "node.exe",
        _candidate_path_from_env("ProgramFiles"),
        _candidate_path_from_env("ProgramFiles(x86)"),
        Path.home() / "AppData" / "Local" / "Programs" / "nodejs" / "node.exe",
        r"C:\Program Files\nodejs\node.exe",
        r"C:\Program Files (x86)\nodejs\node.exe",
    ]:
        add_candidate(candidate)
        if candidate is None:
            continue
        found = existing_file(candidate)
        if found:
            return found

    for command in ["node", "node.exe"]:
        add_candidate(command)
        found = shutil.which(command)
        if found:
            return found

    configured = requested or "auto"
    raise DynamicFpError(f"找不到 Node.js：配置={configured}；已尝试：{'; '.join(attempted)}")


@dataclass
class SolverContext:
    captcha_id: str = CAPTCHA_ID
    referer: str = REFERER
    zone_id: str = ZONE_ID
    dt: str = DT
    version: str = VERSION
    load_version: str = LOAD_VERSION
    run_env: str = RUN_ENV
    width: int = WIDTH
    captcha_type: str = CAPTCHA_TYPE

    @classmethod
    def build(
        cls,
        captcha_id: str | None = None,
        referer: str | None = None,
        zone_id: str | None = None,
        dt: str | None = None,
        width: int | None = None,
        captcha_type: str | None = None,
    ) -> "SolverContext":
        return cls(
            captcha_id=captcha_id or CAPTCHA_ID,
            referer=referer or REFERER,
            zone_id=zone_id or ZONE_ID,
            dt=dt or DT,
            width=width or WIDTH,
            captcha_type=str(captcha_type or CAPTCHA_TYPE),
        )

PRIVATE_B64_ALPHABET = "MB.CfHUzEeJpsuGkgNwhqiSaI4Fd9L6jYKZAxn1/Vml0c5rbXRP+8tD3QTO2vWyo"
PRIVATE_B64_PADDING = "7"
NORMAL_B64_ALPHABET = "i/x1XgU0z7k8N+lCpOnPrv6\\qu2Gj9HRcwTYZ4bfSJBhaWstAeoMIEQ5mDdVFLKy"
NORMAL_B64_PADDING = "3"
SBOX_HEX = (
    "a7be3f3933fa8c5fcf86c4b6908b569ba1e26c1a6d7cfbf60ae4b00e074a194dac4b73e7f898541159a39d08183b76eedee3ed341e6685d2357440158394b1ff03a9004cbbb5ca7dcb7f41489a16e03dcc9c71eb3c9796685b1d01b4d56193a6e1f1a2470445c191ae49c5d82765dc82c350f263387a24a502fcbf442e2dddaad0e936d9ea22b89275307b42518fbc3a626ba806d4ecd6d725f50cc8c72fefa4551ccd6fc9b2b7ab954f815c7264c6e51f4eaf99885a79892b1b60a0b3526e57ba5d178d370958847eb9fd28f9ce0bc023f4148a2adfe632126769057043d3bd8eda0df7872629f3809ef05310e83113216afe202c460fc23e789f77d1addb5e"
)
ROUND_KEY = "037606da0296055c"
SEED_KEY = "fd6a43ae25f74398b61c03c83be37449"
CRC32_TABLE = [
    0x0, 0x77073096, 0xEE0E612C, 0x990951BA, 0x76DC419, 0x706AF48F, 0xE963A535, 0x9E6495A3,
    0xEDB8832, 0x79DCB8A4, 0xE0D5E91E, 0x97D2D988, 0x9B64C2B, 0x7EB17CBD, 0xE7B82D07, 0x90BF1D91,
    0x1DB71064, 0x6AB020F2, 0xF3B97148, 0x84BE41DE, 0x1ADAD47D, 0x6DDDE4EB, 0xF4D4B551, 0x83D385C7,
    0x136C9856, 0x646BA8C0, 0xFD62F97A, 0x8A65C9EC, 0x14015C4F, 0x63066CD9, 0xFA0F3D63, 0x8D080DF5,
    0x3B6E20C8, 0x4C69105E, 0xD56041E4, 0xA2677172, 0x3C03E4D1, 0x4B04D447, 0xD20D85FD, 0xA50AB56B,
    0x35B5A8FA, 0x42B2986C, 0xDBBBC9D6, 0xACBCF940, 0x32D86CE3, 0x45DF5C75, 0xDCD60DCF, 0xABD13D59,
    0x26D930AC, 0x51DE003A, 0xC8D75180, 0xBFD06116, 0x21B4F4B5, 0x56B3C423, 0xCFBA9599, 0xB8BDA50F,
    0x2802B89E, 0x5F058808, 0xC60CD9B2, 0xB10BE924, 0x2F6F7C87, 0x58684C11, 0xC1611DAB, 0xB6662D3D,
    0x76DC4190, 0x1DB7106, 0x98D220BC, 0xEFD5102A, 0x71B18589, 0x6B6B51F, 0x9FBFE4A5, 0xE8B8D433,
    0x7807C9A2, 0xF00F934, 0x9609A88E, 0xE10E9818, 0x7F6A0DBB, 0x86D3D2D, 0x91646C97, 0xE6635C01,
    0x6B6B51F4, 0x1C6C6162, 0x856530D8, 0xF262004E, 0x6C0695ED, 0x1B01A57B, 0x8208F4C1, 0xF50FC457,
    0x65B0D9C6, 0x12B7E950, 0x8BBEB8EA, 0xFCB9887C, 0x62DD1DDF, 0x15DA2D49, 0x8CD37CF3, 0xFBD44C65,
    0x4DB26158, 0x3AB551CE, 0xA3BC0074, 0xD4BB30E2, 0x4ADFA541, 0x3DD895D7, 0xA4D1C46D, 0xD3D6F4FB,
    0x4369E96A, 0x346ED9FC, 0xAD678846, 0xDA60B8D0, 0x44042D73, 0x33031DE5, 0xAA0A4C5F, 0xDD0D7CC9,
    0x5005713C, 0x270241AA, 0xBE0B1010, 0xC90C2086, 0x5768B525, 0x206F85B3, 0xB966D409, 0xCE61E49F,
    0x5EDEF90E, 0x29D9C998, 0xB0D09822, 0xC7D7A8B4, 0x59B33D17, 0x2EB40D81, 0xB7BD5C3B, 0xC0BA6CAD,
    0xEDB88320, 0x9ABFB3B6, 0x3B6E20C, 0x74B1D29A, 0xEAD54739, 0x9DD277AF, 0x4DB2615, 0x73DC1683,
    0xE3630B12, 0x94643B84, 0xD6D6A3E, 0x7A6A5AA8, 0xE40ECF0B, 0x9309FF9D, 0xA00AE27, 0x7D079EB1,
    0xF00F9344, 0x8708A3D2, 0x1E01F268, 0x6906C2FE, 0xF762575D, 0x806567CB, 0x196C3671, 0x6E6B06E7,
    0xFED41B76, 0x89D32BE0, 0x10DA7A5A, 0x67DD4ACC, 0xF9B9DF6F, 0x8EBEEFF9, 0x17B7BE43, 0x60B08ED5,
    0xD6D6A3E8, 0xA1D1937E, 0x38D8C2C4, 0x4FDFF252, 0xD1BB67F1, 0xA6BC5767, 0x3FB506DD, 0x48B2364B,
    0xD80D2BDA, 0xAF0A1B4C, 0x36034AF6, 0x41047A60, 0xDF60EFC3, 0xA867DF55, 0x316E8EEF, 0x4669BE79,
    0xCB61B38C, 0xBC66831A, 0x256FD2A0, 0x5268E236, 0xCC0C7795, 0xBB0B4703, 0x220216B9, 0x5505262F,
    0xC5BA3BBE, 0xB2BD0B28, 0x2BB45A92, 0x5CB36A04, 0xC2D7FFA7, 0xB5D0CF31, 0x2CD99E8B, 0x5BDEAE1D,
    0x9B64C2B0, 0xEC63F226, 0x756AA39C, 0x26D930A, 0x9C0906A9, 0xEB0E363F, 0x72076785, 0x5005713,
    0x95BF4A82, 0xE2B87A14, 0x7BB12BAE, 0xCB61B38, 0x92D28E9B, 0xE5D5BE0D, 0x7CDCEFB7, 0xBDBDF21,
    0x86D3D2D4, 0xF1D4E242, 0x68DDB3F8, 0x1FDA836E, 0x81BE16CD, 0xF6B9265B, 0x6FB077E1, 0x18B74777,
    0x88085AE6, 0xFF0F6A70, 0x66063BCA, 0x11010B5C, 0x8F659EFF, 0xF862AE69, 0x616BFFD3, 0x166CCF45,
    0xA00AE278, 0xD70DD2EE, 0x4E048354, 0x3903B3C2, 0xA7672661, 0xD06016F7, 0x4969474D, 0x3E6E77DB,
    0xAED16A4A, 0xD9D65ADC, 0x40DF0B66, 0x37D83BF0, 0xA9BCAE53, 0xDEBB9EC5, 0x47B2CF7F, 0x30B5FFE9,
    0xBDBDF21C, 0xCABAC28A, 0x53B39330, 0x24B4A3A6, 0xBAD03605, 0xCDD70693, 0x54DE5729, 0x23D967BF,
    0xB3667A2E, 0xC4614AB8, 0x5D681B02, 0x2A6F2B94, 0xB40BBE37, 0xC30C8EA1, 0x5A05DF1B, 0x2D02EF8D,
]


def to_byte(value: int) -> int:
    value = int(value)
    if value < -128:
        return to_byte(256 + value)
    if value > 127:
        return to_byte(value - 256)
    return value


def byte_u(value: int) -> int:
    return value & 0xFF


def string_to_bytes(value: str) -> list[int]:
    encoded = quote(str(value), safe="-_.!~*'()")
    out = []
    i = 0
    while i < len(encoded):
        if encoded[i] == "%" and i + 2 < len(encoded):
            out.append(to_byte(int(encoded[i + 1 : i + 3], 16)))
            i += 3
        else:
            out.append(to_byte(ord(encoded[i])))
            i += 1
    return out


def bytes_to_string(values: list[int]) -> str:
    return bytes(byte_u(v) for v in values).decode("utf-8")


def hex_to_byte(value: str) -> int:
    return to_byte(int(value[:2], 16))


def hexs_to_bytes(value: str) -> list[int]:
    return [hex_to_byte(value[i : i + 2]) for i in range(0, len(value), 2)]


def int_to_bytes(value: int) -> list[int]:
    value &= 0xFFFFFFFF
    return [to_byte((value >> 24) & 0xFF), to_byte((value >> 16) & 0xFF), to_byte((value >> 8) & 0xFF), to_byte(value & 0xFF)]


def copy_to_bytes(src: list[int], src_pos: int, dst: list[int], dst_pos: int, length: int) -> list[int]:
    if len(dst) < dst_pos + length:
        dst.extend([0] * (dst_pos + length - len(dst)))
    for i in range(length):
        if src_pos + i < len(src):
            dst[dst_pos + i] = src[src_pos + i]
    return dst


def padding_array_zero(length: int) -> list[int]:
    return [0] * length


def xor_byte(left: int, right: int) -> int:
    return to_byte(to_byte(left) ^ to_byte(right))


def shift_byte(left: int, right: int) -> int:
    return to_byte(left + right)


def xors(left: list[int], right: list[int]) -> list[int]:
    return [xor_byte(v, right[i % len(right)]) for i, v in enumerate(left)]


def shifts(left: list[int], right: list[int]) -> list[int]:
    return [shift_byte(v, right[i % len(right)]) for i, v in enumerate(left)]


def gen_crc32(values: list[int]) -> str:
    crc = 0xFFFFFFFF
    for value in values:
        crc = ((crc >> 8) ^ CRC32_TABLE[(crc ^ byte_u(value)) & 0xFF]) & 0xFFFFFFFF
    crc = (0xFFFFFFFF ^ crc) & 0xFFFFFFFF
    return "".join(f"{byte_u(v):02x}" for v in int_to_bytes(crc))


def b64_encode_chunk(chunk: list[int], alphabet: str, padding: str) -> str:
    if len(chunk) == 1:
        a = byte_u(chunk[0])
        return alphabet[(a >> 2) & 0x3F] + alphabet[(a << 4) & 0x30] + padding + padding
    if len(chunk) == 2:
        a, b = [byte_u(x) for x in chunk]
        return alphabet[(a >> 2) & 0x3F] + alphabet[((a << 4) & 0x30) + ((b >> 4) & 0xF)] + alphabet[(b << 2) & 0x3C] + padding
    if len(chunk) == 3:
        a, b, c = [byte_u(x) for x in chunk]
        return (
            alphabet[(a >> 2) & 0x3F]
            + alphabet[((a << 4) & 0x30) + ((b >> 4) & 0xF)]
            + alphabet[((b << 2) & 0x3C) + ((c >> 6) & 0x3)]
            + alphabet[c & 0x3F]
        )
    return ""


def b64_encode(values: list[int], alphabet: str = NORMAL_B64_ALPHABET, padding: str = NORMAL_B64_PADDING) -> str:
    if not values:
        return ""
    out = []
    i = 0
    while i < len(values):
        out.append(b64_encode_chunk(values[i : i + 3], alphabet, padding))
        i += 3
    return "".join(out)


def b64_decode(value: str, alphabet: str = NORMAL_B64_ALPHABET, padding: str = NORMAL_B64_PADDING) -> list[int]:
    stripped = value.split(padding, 1)[0]
    chars = list(stripped)
    out: list[int] = []
    i = 0
    while i < len(chars):
        group = [alphabet.index(ch) for ch in chars[i : i + 4]]
        if len(group) == 2:
            out.append(to_byte(((group[0] << 2) & 0xFF) + ((group[1] >> 4) & 0x3)))
        elif len(group) == 3:
            out.append(to_byte(((group[0] << 2) & 0xFF) + ((group[1] >> 4) & 0x3)))
            out.append(to_byte(((group[1] << 4) & 0xFF) + ((group[2] >> 2) & 0xF)))
        elif len(group) == 4:
            out.append(to_byte(((group[0] << 2) & 0xFF) + ((group[1] >> 4) & 0x3)))
            out.append(to_byte(((group[1] << 4) & 0xFF) + ((group[2] >> 2) & 0xF)))
            out.append(to_byte(((group[2] << 6) & 0xFF) + (group[3] & 0x3F)))
        i += 4
    return out


def repeat_to_64(values: list[int]) -> list[int]:
    if not values:
        return padding_array_zero(64)
    if len(values) >= 64:
        return list(values[:64])
    return [values[i % len(values)] for i in range(64)]


def pad64(values: list[int]) -> list[int]:
    if not values:
        return padding_array_zero(64)
    size = len(values)
    pad_len = 64 - size % 64 - 4 if size % 64 <= 60 else 128 - size % 64 - 4
    out = list(values) + [0] * pad_len + int_to_bytes(size)
    return out


def chunks64(values: list[int]) -> list[list[int]]:
    if len(values) % 64:
        return []
    return [values[i : i + 64] for i in range(0, len(values), 64)]


def sbox_sub(values: list[int]) -> list[int]:
    sbox = hexs_to_bytes(SBOX_HEX)
    return [sbox[((byte_u(v) >> 4) & 0xF) * 16 + (byte_u(v) & 0xF)] for v in values]


def round_transform(values: list[int]) -> list[int]:
    funcs = [
        lambda arr, n: arr,
        lambda arr, n: [xor_byte(v, to_byte(n)) for v in arr],
        lambda arr, n: [shift_byte(v, to_byte(n)) for v in arr],
        lambda arr, n: [xor_byte(v, to_byte(n) + i) for i, v in enumerate(arr)],
        lambda arr, n: [shift_byte(v, to_byte(n) + i) for i, v in enumerate(arr)],
        lambda arr, n: [xor_byte(v, to_byte(n) - i) for i, v in enumerate(arr)],
        lambda arr, n: [shift_byte(v, to_byte(n) - i) for i, v in enumerate(arr)],
    ]
    out = list(values)
    for i in range(0, len(ROUND_KEY), 4):
        part = ROUND_KEY[i : i + 4]
        out = funcs[hex_to_byte(part[:2])](out, hex_to_byte(part[2:]))
    return out


def inverse_sbox_sub(values: list[int]) -> list[int]:
    sbox = hexs_to_bytes(SBOX_HEX)
    reverse = {byte_u(value): index for index, value in enumerate(sbox)}
    return [to_byte(reverse[byte_u(value)]) for value in values]


def unshift_byte(left: int, right: int) -> int:
    return to_byte(byte_u(left) - byte_u(right))


def inverse_round_transform(values: list[int]) -> list[int]:
    def inverse_round(arr: list[int], func_index: int, n: int) -> list[int]:
        if func_index == 0:
            return arr
        if func_index == 1:
            return [xor_byte(v, to_byte(n)) for v in arr]
        if func_index == 2:
            return [unshift_byte(v, to_byte(n)) for v in arr]
        if func_index == 3:
            return [xor_byte(v, to_byte(n) + i) for i, v in enumerate(arr)]
        if func_index == 4:
            return [unshift_byte(v, to_byte(n) + i) for i, v in enumerate(arr)]
        if func_index == 5:
            return [xor_byte(v, to_byte(n) - i) for i, v in enumerate(arr)]
        if func_index == 6:
            return [unshift_byte(v, to_byte(n) - i) for i, v in enumerate(arr)]
        raise ValueError(f"unsupported round transform index: {func_index}")

    out = list(values)
    parts = [ROUND_KEY[i : i + 4] for i in range(0, len(ROUND_KEY), 4)]
    for part in reversed(parts):
        out = inverse_round(out, hex_to_byte(part[:2]), hex_to_byte(part[2:]))
    return out


def aes(value: str) -> str:
    plain = string_to_bytes(value)
    seed = [to_byte(random.randrange(256)) for _ in range(4)]
    key = repeat_to_64(xors(repeat_to_64(string_to_bytes(SEED_KEY)), repeat_to_64(seed)))
    crc_bytes = string_to_bytes(gen_crc32(plain))
    blocks = chunks64(pad64(plain + crc_bytes))
    out = list(seed)
    prev = key
    for index, block in enumerate(blocks):
        mixed = xors(round_transform(block), key)
        mixed = xors(shifts(mixed, prev), prev)
        prev = sbox_sub(sbox_sub(mixed))
        copy_to_bytes(prev, 0, out, 64 * index + 4, 64)
    return b64_encode(out, PRIVATE_B64_ALPHABET, PRIVATE_B64_PADDING)


def _bytes_to_u32(values: list[int]) -> int:
    return (
        (byte_u(values[0]) << 24)
        | (byte_u(values[1]) << 16)
        | (byte_u(values[2]) << 8)
        | byte_u(values[3])
    )


def aes_decode(value: str, verify_crc: bool = True) -> str:
    encrypted = b64_decode(value, PRIVATE_B64_ALPHABET, PRIVATE_B64_PADDING)
    if len(encrypted) < 68 or (len(encrypted) - 4) % 64:
        raise ValueError("invalid aes payload length")
    seed = encrypted[:4]
    key = repeat_to_64(xors(repeat_to_64(string_to_bytes(SEED_KEY)), repeat_to_64(seed)))
    prev = key
    plain_padded: list[int] = []
    for block_start in range(4, len(encrypted), 64):
        cipher_block = encrypted[block_start : block_start + 64]
        mixed = inverse_sbox_sub(inverse_sbox_sub(cipher_block))
        shifted = xors(mixed, prev)
        round_xored = [unshift_byte(v, prev[index]) for index, v in enumerate(shifted)]
        rounded = xors(round_xored, key)
        plain_block = inverse_round_transform(rounded)
        plain_padded.extend(plain_block)
        prev = cipher_block
    if len(plain_padded) < 4:
        raise ValueError("invalid aes plaintext padding")
    size = _bytes_to_u32(plain_padded[-4:])
    if size < 8 or size > len(plain_padded) - 4:
        raise ValueError(f"invalid aes plaintext size: {size}")
    payload = plain_padded[:size]
    plain = payload[:-8]
    crc = bytes_to_string(payload[-8:])
    if verify_crc and crc != gen_crc32(plain):
        raise ValueError("invalid aes crc")
    return bytes_to_string(plain)


def xor_encode(token: str, value: str) -> str:
    return b64_encode(xors(string_to_bytes(value), string_to_bytes(token)), NORMAL_B64_ALPHABET, NORMAL_B64_PADDING)


def xor_decode(token: str, value: str) -> str:
    return bytes_to_string(xors(b64_decode(value), string_to_bytes(token)))


def decode_check_payload(token: str, check_data: str) -> dict:
    payload = json.loads(check_data) if isinstance(check_data, str) else dict(check_data)
    decoded: dict[str, object] = {}
    if payload.get("p"):
        decoded["pRaw"] = xor_decode(token, aes_decode(payload["p"]))
    if payload.get("ext"):
        decoded["extRaw"] = xor_decode(token, aes_decode(payload["ext"]))
    if payload.get("f"):
        decoded["fRaw"] = xor_decode(token, aes_decode(payload["f"]))
    if payload.get("d"):
        trace_raw = aes_decode(payload["d"])
        decoded["dRaw"] = trace_raw
        decoded["traceRows"] = [xor_decode(token, item) for item in trace_raw.split(":") if item]
    if payload.get("m"):
        trace_raw = aes_decode(payload["m"])
        decoded["mRaw"] = trace_raw
        decoded["mouseRows"] = [xor_decode(token, item) for item in trace_raw.split(":") if item]
    return decoded


def gen_cb() -> str:
    alphabet = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
    chars = [random.choice(alphabet) for _ in range(32)]
    for pos, ch in zip([1, 10, 12, 13, 26, 31], "vfnv46"):
        chars[pos] = ch
    return aes("".join(chars))


def safe_sdk_b64(value: str) -> str:
    return value.replace("\\", "-").replace("/", "_").replace("+", "*")


def build_onverify_validate(raw_validate: str, fp: str, zone_id: str = ZONE_ID) -> str:
    encoded = safe_sdk_b64(aes(f"{raw_validate}::{fp}"))
    return f"{zone_id}_{encoded}_v_i_1" if zone_id else f"{encoded}_v_i_1"


def refresh_fp_timestamp(fp: str) -> str:
    expire_ms = int(time.time() * 1000) + 15 * 60 * 1000
    if re.search(r":\d{10,}$", fp):
        return re.sub(r":\d{10,}$", f":{expire_ms}", fp)
    return f"{fp}:{expire_ms}"


class DynamicFpError(RuntimeError):
    pass


def is_valid_fp(fp: str) -> bool:
    return isinstance(fp, str) and len(fp) > 80 and re.search(r":\d{10,}$", fp) is not None


def fp_timestamp_ms(fp: str) -> int | None:
    match = re.search(r":(\d{10,})$", fp or "")
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


def _recv_exact(sock: socket.socket, size: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < size:
        chunk = sock.recv(size - len(chunks))
        if not chunk:
            raise DynamicFpError("Chrome DevTools websocket closed unexpectedly")
        chunks.extend(chunk)
    return bytes(chunks)


def _websocket_handshake(sock: socket.socket, target_url: str) -> None:
    parsed = urlsplit(target_url)
    if parsed.scheme != "ws":
        raise DynamicFpError(f"unsupported Chrome DevTools websocket scheme: {parsed.scheme}")
    host = parsed.hostname or CHROME_DEBUG_HOST
    port = parsed.port or CHROME_DEBUG_PORT
    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"
    host_header = f"{host}:{port}"
    key = base64.b64encode(os.urandom(16)).decode("ascii")
    request = (
        f"GET {path} HTTP/1.1\r\n"
        f"Host: {host_header}\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        "Sec-WebSocket-Version: 13\r\n"
        "\r\n"
    )
    sock.sendall(request.encode("ascii"))
    response = bytearray()
    while b"\r\n\r\n" not in response:
        chunk = sock.recv(4096)
        if not chunk:
            break
        response.extend(chunk)
        if len(response) > 16384:
            break
    first_line = bytes(response).split(b"\r\n", 1)[0].decode("latin1", errors="replace")
    if " 101 " not in f" {first_line} ":
        raise DynamicFpError(f"Chrome DevTools websocket handshake failed: {first_line}")


def _websocket_send_frame(sock: socket.socket, payload: bytes, opcode: int = 1) -> None:
    length = len(payload)
    header = bytearray([0x80 | opcode])
    if length < 126:
        header.append(0x80 | length)
    elif length < 65536:
        header.append(0x80 | 126)
        header.extend(struct.pack("!H", length))
    else:
        header.append(0x80 | 127)
        header.extend(struct.pack("!Q", length))
    mask = os.urandom(4)
    masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
    sock.sendall(bytes(header) + mask + masked)


def _websocket_read_frame(sock: socket.socket) -> tuple[int, bytes]:
    head = _recv_exact(sock, 2)
    opcode = head[0] & 0x0F
    masked = bool(head[1] & 0x80)
    length = head[1] & 0x7F
    if length == 126:
        length = struct.unpack("!H", _recv_exact(sock, 2))[0]
    elif length == 127:
        length = struct.unpack("!Q", _recv_exact(sock, 8))[0]
    mask = _recv_exact(sock, 4) if masked else b""
    payload = _recv_exact(sock, length) if length else b""
    if masked:
        payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
    return opcode, payload


def _websocket_read_json(sock: socket.socket) -> dict:
    while True:
        opcode, payload = _websocket_read_frame(sock)
        if opcode == 1:
            return json.loads(payload.decode("utf-8"))
        if opcode == 8:
            raise DynamicFpError("Chrome DevTools websocket closed")
        if opcode == 9:
            _websocket_send_frame(sock, payload, opcode=10)


def _cdp_evaluate(websocket_url: str, expression: str, timeout: float) -> object:
    parsed = urlsplit(websocket_url)
    host = parsed.hostname or CHROME_DEBUG_HOST
    port = parsed.port or CHROME_DEBUG_PORT
    with socket.create_connection((host, port), timeout=timeout) as sock:
        sock.settimeout(timeout)
        _websocket_handshake(sock, websocket_url)
        message_id = secrets.randbelow(1_000_000_000) + 1
        _websocket_send_frame(
            sock,
            json.dumps(
                {
                    "id": message_id,
                    "method": "Runtime.evaluate",
                    "params": {
                        "expression": expression,
                        "returnByValue": True,
                        "awaitPromise": True,
                    },
                },
                separators=(",", ":"),
            ).encode("utf-8"),
        )
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            message = _websocket_read_json(sock)
            if message.get("id") != message_id:
                continue
            if "error" in message:
                raise DynamicFpError(json.dumps(message["error"], ensure_ascii=False))
            result = message.get("result", {})
            if result.get("exceptionDetails"):
                raise DynamicFpError(json.dumps(result["exceptionDetails"], ensure_ascii=False))
            value = result.get("result", {})
            return value.get("value")
    raise DynamicFpError("Chrome DevTools Runtime.evaluate timed out")


def _load_cdp_targets(host: str, port: int, timeout: float) -> list[dict]:
    session = requests.Session()
    session.trust_env = False
    response = session.get(f"http://{host}:{port}/json", timeout=timeout)
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, list):
        raise DynamicFpError("Chrome DevTools /json did not return a target list")
    return [item for item in data if isinstance(item, dict)]


def _normalize_url_for_match(value: str | None) -> str:
    return unquote(str(value or "")).replace("\\", "/").rstrip("/").lower()


def _target_match_score(target: dict, referer: str | None) -> int:
    url = _normalize_url_for_match(target.get("url"))
    ref = _normalize_url_for_match(referer)
    if ref and url == ref:
        return 3
    if ref and ref in url:
        return 2
    if ref and url and url.rsplit("/", 1)[-1] == ref.rsplit("/", 1)[-1]:
        return 1
    return 0


def _ordered_cdp_targets(targets: list[dict], referer: str | None) -> list[dict]:
    pages = [
        target
        for target in targets
        if target.get("type") == "page" and target.get("webSocketDebuggerUrl")
    ]
    return [
        target
        for _, target in sorted(
            enumerate(pages),
            key=lambda item: (-_target_match_score(item[1], referer), item[0]),
        )
    ]


def load_dynamic_fp_from_chrome(
    referer: str | None = None,
    *,
    host: str = CHROME_DEBUG_HOST,
    port: int = CHROME_DEBUG_PORT,
    timeout: float = CHROME_DEBUG_TIMEOUT,
    global_name: str = FP_GLOBAL_NAME,
) -> tuple[str, dict]:
    targets = _ordered_cdp_targets(_load_cdp_targets(host, port, timeout), referer)
    if not targets:
        raise DynamicFpError(f"Chrome DevTools {host}:{port} has no debuggable page targets")
    expression = f"(() => {{ const fp = window[{json.dumps(global_name)}]; return typeof fp === 'string' ? fp : ''; }})()"
    errors = []
    for target in targets:
        target_url = str(target.get("url") or "")
        try:
            value = _cdp_evaluate(str(target["webSocketDebuggerUrl"]), expression, timeout)
        except Exception as exc:
            errors.append(f"{target_url}: {exc}")
            continue
        fp = str(value or "").strip()
        if is_valid_fp(fp):
            stamp = fp_timestamp_ms(fp)
            now_ms = int(time.time() * 1000)
            return fp, {
                "source": "chrome_devtools",
                "host": host,
                "port": port,
                "targetUrl": target_url,
                "globalName": global_name,
                "fpLength": len(fp),
                "fpTimestampMs": stamp,
                "fpAgeMs": (now_ms - stamp) if stamp is not None else None,
            }
        errors.append(f"{target_url}: invalid {global_name} length={len(fp)}")
    detail = "; ".join(errors[-3:]) if errors else "no page exposed a valid fp"
    raise DynamicFpError(f"无法从 Chrome DevTools 读取动态 fp: {detail}")


def local_fp_core_path() -> Path:
    for item in LOCAL_FP_CORE_CANDIDATES:
        if item.exists():
            return item
    raise DynamicFpError("本地 fp core 脚本不存在：research/core-optimi.browser.current.min.js")


class LocalFpWorker:
    def __init__(self, index: int, node_path: str, script_path: Path, run_root: Path):
        self.index = index
        self.node_path = node_path
        self.script_path = script_path
        self.run_root = run_root
        self._lock = threading.Lock()
        self._responses: queue.Queue[dict] = queue.Queue()
        self._stderr_lines: list[str] = []
        self._stderr_lock = threading.Lock()
        self._proc: subprocess.Popen | None = None
        self._seq = 0

    def _creationflags(self) -> int:
        return _child_creationflags()

    def _drain_stdout(self, pipe, responses: queue.Queue) -> None:
        try:
            for line in iter(pipe.readline, ""):
                text = line.strip()
                if not text:
                    continue
                try:
                    responses.put(json.loads(text))
                except Exception:
                    responses.put({"ok": False, "error": f"worker stdout 不是 JSON: {text[:200]}"})
        except Exception as exc:
            responses.put({"ok": False, "error": f"worker stdout reader failed: {exc}"})

    def _drain_stderr(self, pipe) -> None:
        try:
            for line in iter(pipe.readline, ""):
                text = line.strip()
                if not text:
                    continue
                with self._stderr_lock:
                    self._stderr_lines.append(text)
                    self._stderr_lines = self._stderr_lines[-20:]
        except Exception:
            pass

    def _stderr_tail(self) -> str:
        with self._stderr_lock:
            return " | ".join(self._stderr_lines[-5:])

    def start(self) -> None:
        if not self.script_path.exists():
            raise DynamicFpError(f"本地 fp 生成器不存在: {self.script_path}")
        if self._proc is not None and self._proc.poll() is None:
            return
        responses: queue.Queue[dict] = queue.Queue()
        self._responses = responses
        with self._stderr_lock:
            self._stderr_lines = []
        self._proc = subprocess.Popen(
            [self.node_path, str(self.script_path), "--worker"],
            cwd=str(self.run_root),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=self._creationflags(),
        )
        _track_child_process(self._proc)
        if self._proc.stdout is not None:
            threading.Thread(target=self._drain_stdout, args=(self._proc.stdout, responses), daemon=True).start()
        if self._proc.stderr is not None:
            threading.Thread(target=self._drain_stderr, args=(self._proc.stderr,), daemon=True).start()

    def stop(self) -> None:
        proc = self._proc
        self._proc = None
        if proc is None:
            return
        try:
            if proc.stdin is not None:
                proc.stdin.close()
        except Exception:
            pass
        if proc.poll() is None:
            try:
                proc.wait(timeout=0.8)
            except Exception:
                _kill_process_tree(proc)
        _untrack_child_process(proc)

    def restart(self) -> None:
        self.stop()
        self.start()

    def request(self, payload: dict, timeout: float) -> dict:
        timeout = max(0.1, float(timeout))
        with self._lock:
            self.start()
            self._seq += 1
            request_id = f"{self.index}-{self._seq}"
            payload = dict(payload)
            payload["id"] = request_id
            while True:
                try:
                    self._responses.get_nowait()
                except queue.Empty:
                    break
            proc = self._proc
            if proc is None or proc.stdin is None:
                self.restart()
                raise DynamicFpError("本地 fp worker 未启动")
            try:
                proc.stdin.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
                proc.stdin.flush()
            except Exception as exc:
                self.restart()
                raise DynamicFpError(f"本地 fp worker 写入失败: {exc}") from exc

            deadline = time.monotonic() + timeout
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    detail = self._stderr_tail()
                    self.restart()
                    suffix = f"；stderr={detail}" if detail else ""
                    raise DynamicFpError(f"本地 fp worker 超时{suffix}")
                if proc.poll() is not None:
                    detail = self._stderr_tail()
                    self.restart()
                    suffix = f"；stderr={detail}" if detail else ""
                    raise DynamicFpError(f"本地 fp worker 已退出，code={proc.returncode}{suffix}")
                try:
                    response = self._responses.get(timeout=min(0.2, remaining))
                except queue.Empty:
                    continue
                if str(response.get("id")) != request_id:
                    continue
                if not response.get("ok"):
                    raise DynamicFpError(f"本地 fp worker 生成失败: {response.get('error') or response}")
                response["pythonWorkerId"] = self.index
                return response


class LocalFpWorkerPool:
    def __init__(self, worker_count: int, node_path: str, script_path: Path, run_root: Path):
        self.worker_count = max(1, min(64, int(worker_count)))
        self.configured_node_path = node_path
        self.node_path = resolve_node_path(node_path)
        self.script_path = script_path
        self.run_root = run_root
        self._lock = threading.Lock()
        self._available: queue.Queue[LocalFpWorker] = queue.Queue()
        self._workers: list[LocalFpWorker] = []
        self._started = False

    def start(self) -> None:
        with self._lock:
            if self._started:
                return
            workers = [LocalFpWorker(index + 1, self.node_path, self.script_path, self.run_root) for index in range(self.worker_count)]
            for worker in workers:
                worker.start()
                self._available.put(worker)
            self._workers = workers
            self._started = True

    def stop(self) -> None:
        with self._lock:
            workers = list(self._workers)
            self._workers = []
            self._started = False
            while True:
                try:
                    self._available.get_nowait()
                except queue.Empty:
                    break
        for worker in workers:
            worker.stop()

    def generate(self, payload: dict, timeout: float) -> dict:
        self.start()
        deadline = time.monotonic() + max(0.1, float(timeout))
        last_error: Exception | None = None
        attempts = 0
        max_attempts = min(2, self.worker_count)
        while attempts < max_attempts:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                worker = self._available.get(timeout=remaining)
            except queue.Empty as exc:
                raise DynamicFpError("本地 fp worker 池繁忙") from exc
            try:
                return worker.request(payload, timeout=remaining)
            except DynamicFpError as exc:
                last_error = exc
                attempts += 1
            finally:
                self._available.put(worker)
        if last_error is not None:
            raise DynamicFpError(str(last_error)) from last_error
        raise DynamicFpError("本地 fp worker 池等待超时")

    def stats(self) -> dict:
        with self._lock:
            alive = sum(1 for worker in self._workers if worker._proc is not None and worker._proc.poll() is None)
            return {
                "enabled": True,
                "workers": self.worker_count,
                "alive": alive,
                "available": self._available.qsize(),
                "nodePath": self.node_path,
            }


_LOCAL_FP_WORKER_POOL: LocalFpWorkerPool | None = None
_LOCAL_FP_WORKER_POOL_KEY: tuple[int, str] | None = None
_LOCAL_FP_WORKER_POOL_LOCK = threading.Lock()


def configure_local_fp_worker_pool(
    worker_count: int | None = None,
    *,
    node_path: str = NODE_PATH,
    start: bool = False,
) -> dict:
    global _LOCAL_FP_WORKER_POOL, _LOCAL_FP_WORKER_POOL_KEY
    count = DEFAULT_LOCAL_FP_WORKERS if worker_count is None else max(1, min(64, int(worker_count)))
    resolved_node_path = resolve_node_path(node_path)
    key = (count, resolved_node_path)
    with _LOCAL_FP_WORKER_POOL_LOCK:
        if _LOCAL_FP_WORKER_POOL is None or _LOCAL_FP_WORKER_POOL_KEY != key:
            if _LOCAL_FP_WORKER_POOL is not None:
                _LOCAL_FP_WORKER_POOL.stop()
            _LOCAL_FP_WORKER_POOL = LocalFpWorkerPool(count, resolved_node_path, LOCAL_FP_VM_PATH, RUN_ROOT)
            _LOCAL_FP_WORKER_POOL_KEY = key
        pool = _LOCAL_FP_WORKER_POOL
    if start:
        pool.start()
    return pool.stats()


def shutdown_local_fp_worker_pool() -> None:
    global _LOCAL_FP_WORKER_POOL, _LOCAL_FP_WORKER_POOL_KEY
    with _LOCAL_FP_WORKER_POOL_LOCK:
        pool = _LOCAL_FP_WORKER_POOL
        _LOCAL_FP_WORKER_POOL = None
        _LOCAL_FP_WORKER_POOL_KEY = None
    if pool is not None:
        pool.stop()
    shutdown_tracked_child_processes()


def local_fp_worker_stats() -> dict:
    with _LOCAL_FP_WORKER_POOL_LOCK:
        pool = _LOCAL_FP_WORKER_POOL
    if pool is None:
        return {"enabled": False, "workers": 0, "alive": 0, "available": 0}
    return pool.stats()


atexit.register(shutdown_local_fp_worker_pool)


def _load_dynamic_fp_local_once(
    referer: str | None = None,
    *,
    node_path: str = NODE_PATH,
    timeout: float = LOCAL_FP_TIMEOUT,
    env_fp: str = LOCAL_ENV_FP,
) -> tuple[str, dict]:
    if not LOCAL_FP_VM_PATH.exists():
        raise DynamicFpError(f"本地 fp 生成器不存在: {LOCAL_FP_VM_PATH}")
    core_path = local_fp_core_path()
    resolved_node_path = resolve_node_path(node_path)
    cmd = [
        resolved_node_path,
        str(LOCAL_FP_VM_PATH),
        "--core",
        str(core_path),
        "--referer",
        str(referer or REFERER),
        "--user-agent",
        USER_AGENT,
    ]
    env_fp = str(env_fp or "").strip()
    if env_fp:
        cmd.extend(["--env-fp", env_fp])
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(RUN_ROOT),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=_child_creationflags(),
        )
        _track_child_process(proc)
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            _kill_process_tree(proc)
            raise DynamicFpError("本地 fp 生成超时") from exc
    except FileNotFoundError as exc:
        raise DynamicFpError(f"找不到 Node.js：{resolved_node_path}") from exc
    finally:
        _untrack_child_process(locals().get("proc"))
    if proc.returncode != 0:
        detail = (stderr or stdout or "").strip()
        raise DynamicFpError(f"本地 fp 生成失败: {detail}")
    try:
        data = json.loads((stdout or "").strip().splitlines()[-1])
    except Exception as exc:
        raise DynamicFpError(f"本地 fp 输出不是 JSON: {(stdout or '').strip()[:200]}") from exc
    fp = str(data.get("fp") or "").strip()
    if not data.get("ok") or not is_valid_fp(fp):
        raise DynamicFpError(f"本地 fp 无效: {data}")
    stamp = fp_timestamp_ms(fp)
    now_ms = int(time.time() * 1000)
    return fp, {
        "source": "local_node_vm",
        "nodePath": resolved_node_path,
        "configuredNodePath": node_path,
        "corePath": str(core_path),
        "envFp": env_fp,
        "fpLength": len(fp),
        "fpTimestampMs": stamp,
        "fpAgeMs": (now_ms - stamp) if stamp is not None else None,
    }


def load_dynamic_fp_local(
    referer: str | None = None,
    *,
    node_path: str = NODE_PATH,
    timeout: float = LOCAL_FP_TIMEOUT,
    env_fp: str = LOCAL_ENV_FP,
    worker_count: int | None = DEFAULT_LOCAL_FP_WORKERS,
) -> tuple[str, dict]:
    if not LOCAL_FP_VM_PATH.exists():
        raise DynamicFpError(f"本地 fp 生成器不存在: {LOCAL_FP_VM_PATH}")
    count = DEFAULT_LOCAL_FP_WORKERS if worker_count is None else int(worker_count)
    disabled = os.environ.get("YIDUN_FP_WORKER_DISABLED", "").strip().lower() in {"1", "true", "yes", "on"}
    if disabled or count <= 0:
        return _load_dynamic_fp_local_once(referer, node_path=node_path, timeout=timeout, env_fp=env_fp)

    core_path = local_fp_core_path()
    try:
        configure_local_fp_worker_pool(count, node_path=node_path, start=False)
        with _LOCAL_FP_WORKER_POOL_LOCK:
            pool = _LOCAL_FP_WORKER_POOL
        if pool is None:
            raise DynamicFpError("本地 fp worker 池未初始化")
        data = pool.generate(
            {
                "corePath": str(core_path),
                "referer": str(referer or REFERER),
                "userAgent": USER_AGENT,
                "envFp": str(env_fp or "").strip(),
                "vmTimeoutMs": LOCAL_FP_VM_TIMEOUT_MS,
            },
            timeout=timeout,
        )
        fp = str(data.get("fp") or "").strip()
        if not is_valid_fp(fp):
            raise DynamicFpError(f"本地 fp worker 返回无效 fp: {data}")
        stamp = fp_timestamp_ms(fp)
        now_ms = int(time.time() * 1000)
        return fp, {
            "source": data.get("source") or "local_node_vm_worker",
            "nodePath": pool.node_path,
            "configuredNodePath": node_path,
            "corePath": str(core_path),
            "envFp": str(env_fp or "").strip(),
            "fpLength": len(fp),
            "fpTimestampMs": stamp,
            "fpAgeMs": (now_ms - stamp) if stamp is not None else None,
            "workerId": data.get("pythonWorkerId"),
            "workerPoolSize": pool.worker_count,
        }
    except Exception as exc:
        fp, meta = _load_dynamic_fp_local_once(referer, node_path=node_path, timeout=timeout, env_fp=env_fp)
        meta["workerFallbackError"] = str(exc)
        return fp, meta


def load_dynamic_fp(
    referer: str | None = None,
    *,
    provider: str = FP_PROVIDER,
    node_path: str = NODE_PATH,
    local_timeout: float = LOCAL_FP_TIMEOUT,
    env_fp: str = LOCAL_ENV_FP,
    local_worker_count: int | None = DEFAULT_LOCAL_FP_WORKERS,
    chrome_debug_host: str = CHROME_DEBUG_HOST,
    chrome_debug_port: int = CHROME_DEBUG_PORT,
    chrome_debug_timeout: float = CHROME_DEBUG_TIMEOUT,
) -> tuple[str, dict]:
    provider = (provider or FP_PROVIDER).strip().lower()
    if provider == "local":
        return load_dynamic_fp_local(referer, node_path=node_path, timeout=local_timeout, env_fp=env_fp, worker_count=local_worker_count)
    if provider == "chrome":
        return load_dynamic_fp_from_chrome(
            referer,
            host=chrome_debug_host,
            port=chrome_debug_port,
            timeout=chrome_debug_timeout,
        )
    if provider == "auto":
        errors = []
        for name in ("local", "chrome"):
            try:
                return load_dynamic_fp(
                    referer,
                    provider=name,
                    node_path=node_path,
                    local_timeout=local_timeout,
                    env_fp=env_fp,
                    local_worker_count=local_worker_count,
                    chrome_debug_host=chrome_debug_host,
                    chrome_debug_port=chrome_debug_port,
                    chrome_debug_timeout=chrome_debug_timeout,
                )
            except Exception as exc:
                errors.append(f"{name}: {exc}")
        raise DynamicFpError("动态 fp 生成失败: " + "; ".join(errors))
    raise DynamicFpError(f"未知 fp_provider: {provider}")


def js_number(value: float | int) -> str:
    if isinstance(value, int):
        return str(value)
    if value == 0:
        return "0"
    text = f"{value:.15g}"
    return text


def js_join(values) -> str:
    return ",".join(js_number(v) if isinstance(v, (int, float)) else str(v) for v in values)


def avg(values: list[float]) -> float:
    return sum(values) / len(values)


def std(values: list[float]) -> float:
    m = avg(values)
    return math.sqrt(sum((v - m) ** 2 for v in values if (v - m) ** 2) / len(values))


def fixed4(value: float) -> float:
    return float(f"{value:.4f}")


def percentile(values: list[float], percent: float) -> float:
    sorted_values = sorted(values)
    if percent <= 0:
        return sorted_values[0]
    if percent >= 100:
        return sorted_values[-1]
    pos = (len(sorted_values) - 1) * (percent / 100)
    left = math.floor(pos)
    return sorted_values[left] + (sorted_values[left + 1] - sorted_values[left]) * (pos - left)


def diffs(x_values: list[float], y_values: list[float]) -> list[float]:
    return [(y_values[i + 1] - y_values[i]) / (x_values[i + 1] - x_values[i]) for i in range(len(x_values) - 1)]


def feature_array(trace: list[list[int]]) -> list[float | int]:
    if not isinstance(trace, list) or len(trace) <= 2:
        return []
    xs = [row[0] for row in trace]
    ys = [row[1] for row in trace]
    ts = [row[2] for row in trace]
    vx = diffs(ts, xs)
    vy = diffs(ts, ys)
    speed = [math.sqrt(x * x + y * y) for x, y in zip(xs, ys)]
    vs = diffs(ts, speed)
    ts_short = ts[:-1]
    ax = diffs(ts_short, vx)
    ay = diffs(ts_short, vy)
    aas = diffs(ts_short, vs)
    return [
        len(set(xs)), len(set(ys)), fixed4(avg(ys)), fixed4(std(ys)), len(xs),
        fixed4(min(vx)), fixed4(max(vx)), fixed4(avg(vx)), fixed4(std(vx)), len(set(vx)), fixed4(percentile(vx, 25)), fixed4(percentile(vx, 75)),
        fixed4(min(vy)), fixed4(max(vy)), fixed4(avg(vy)), fixed4(std(vy)), len(set(vy)), fixed4(percentile(vy, 25)), fixed4(percentile(vy, 75)),
        fixed4(min(vs)), fixed4(max(vs)), fixed4(avg(vs)), fixed4(std(vs)), len(set(vs)), fixed4(percentile(vs, 25)), fixed4(percentile(vs, 75)),
        fixed4(min(ax)), fixed4(max(ax)), fixed4(avg(ax)), fixed4(std(ax)), len(set(ax)), fixed4(percentile(ax, 25)), fixed4(percentile(ax, 75)),
        fixed4(min(ay)), fixed4(max(ay)), fixed4(avg(ay)), fixed4(std(ay)), len(set(ay)), fixed4(percentile(ay, 25)), fixed4(percentile(ay, 75)),
        fixed4(min(aas)), fixed4(max(aas)), fixed4(avg(aas)), fixed4(std(aas)), len(set(aas)), fixed4(percentile(aas, 25)), fixed4(percentile(aas, 75)),
    ]


def unique_2d_array(values: list[list[int]], index: int = 0) -> list[list[int]]:
    seen = set()
    out = []
    for row in values:
        key = row[index]
        if key is not None and key not in seen:
            seen.add(key)
            out.append(row)
    return out


def sample_array(values: list, sample_num: int) -> list:
    size = len(values)
    if size <= sample_num:
        return values
    out = []
    cursor = 0
    for i in range(size):
        if i >= cursor * (size - 1) / (sample_num - 1):
            out.append(values[i])
            cursor += 1
    return out


def restrict_drag(drag_x: float, offset: int = -21, width: int = WIDTH, slider_width: int = 40, jigsaw_width: int = 61) -> float:
    start_left = 0
    max_left = width - jigsaw_width
    left = start_left + drag_x
    threshold = -offset if offset < 0 else offset / 2
    if drag_x <= threshold:
        delta = drag_x
        left += -delta / 2 if offset < 0 else delta
    elif width - drag_x - slider_width <= threshold:
        delta = drag_x - (width - slider_width - threshold)
        left += (offset / 2 - delta / 2) if offset < 0 else (offset / 2 + delta)
    else:
        left += offset / 2
    if left <= start_left:
        left = start_left
    if left >= max_left:
        left = max_left
    return left


def jigsaw_position(raw_x: int, attrs: float, width: int = WIDTH) -> dict:
    left = restrict_drag(raw_x, width=width) * ((width / 2 - 61) / width)
    return {
        "RawX": raw_x,
        "position": f"{left}px",
        "rotation": f"rotate({attrs * left}deg)",
        "left": left,
    }


def normalize_track_timing(track: list[list[int]]) -> list[list[int]]:
    last_t = 0
    for index, row in enumerate(track):
        current = int(row[2])
        if index == 0:
            min_t = FIRST_MOVE_MIN_MS
        else:
            min_t = last_t + random.randint(MOVE_INTERVAL_MIN_MS, MOVE_INTERVAL_MIN_MS + 6)
        if current < min_t:
            row[2] = min_t
        last_t = int(row[2])
    return track


def build_track(raw_x: int, steps: int | None = None, trusted_code: int = TRUSTED_EVENT_CODE) -> list[list[int]]:
    steps = steps or random.randint(44, 56)
    first_delay = random.randint(34, 58)
    move_time = random.randint(590, 760)
    out = []
    last_x = -1
    last_t = 0
    for i in range(1, steps + 1):
        progress = i / steps
        ease = 1 - (1 - progress) ** 3
        wave = math.sin(progress * math.pi) * random.uniform(-1.8, 1.8)
        x = int(round(raw_x * ease + wave))
        if i == steps:
            x = raw_x
        x = max(0, min(raw_x, x))
        if x < last_x:
            x = last_x
        y = int(round(math.sin(progress * math.pi * random.uniform(1.4, 2.2)) * random.uniform(-3, 3)))
        t = int(round(first_delay + move_time * progress + random.uniform(-4, 4)))
        if t <= last_t:
            t = last_t + random.randint(MOVE_INTERVAL_MIN_MS, MOVE_INTERVAL_MIN_MS + 5)
        out.append([x, y, t, trusted_code])
        last_x = x
        last_t = t
    return normalize_track_timing(out)


@lru_cache(maxsize=4)
def load_track_template(template_path: Path = TRACK_TEMPLATE_PATH) -> tuple[tuple[int, ...], ...]:
    sample = json.loads(template_path.read_text(encoding="utf-8"))
    return tuple(tuple(int(value) for value in row) for row in sample["atomTraceData"])


def build_track_from_template(
    raw_x: int,
    jitter: bool = True,
    template_path: Path = TRACK_TEMPLATE_PATH,
    trusted_code: int = TRUSTED_EVENT_CODE,
) -> list[list[int]]:
    """
    生成高度拟真的人类滑动轨迹

    关键改进:
    1. 三阶段速度曲线 (加速 -> 匀速 -> 减速微调)
    2. 多频率Y轴波动 (模拟手部自然抖动)
    3. 微停顿和突发加速 (模拟人类决策和纠正)
    4. 非线性时间分布
    """
    template = load_track_template(template_path)
    base_x = template[-1][0] or 1
    out = []
    last_x = -1
    last_t = 0

    # === 全局参数 ===
    # 时间缩放: 保持自然轨迹形状，但缩短过保守的模板耗时。
    time_scale = random.uniform(TRACK_TIME_SCALE_MIN, TRACK_TIME_SCALE_MAX) if jitter else 1.0
    # 全局时间偏移
    time_offset = random.randint(-15, 30) if jitter else 0

    # === Y轴自然波动系统 ===
    # 主波动: 低频大幅度(手臂移动) - 严格限制在模板范围内
    y_wave_main_freq = random.uniform(0.20, 0.30)
    y_wave_main_amp = random.uniform(0.8, 1.5)  # 从2.0-3.8降至0.8-1.5
    # 次波动: 高频小幅度(手指微动) - 进一步降低
    y_wave_sub_freq = random.uniform(1.2, 2.0)
    y_wave_sub_amp = random.uniform(0.2, 0.5)  # 从0.5-1.0降至0.2-0.5
    # 线性漂移 - 最小化
    y_drift_rate = random.uniform(-0.3, 0.3)  # 从±0.8降至±0.3
    # 随机相位(避免轨迹完全相同)
    y_phase_shift = random.uniform(0, math.pi * 2)
    # Y轴零点矫正 - 最小化
    y_center_bias = random.uniform(-0.15, 0.15)  # 从±0.3降至±0.15

    # === X轴非线性进度函数 ===
    # 使用三次贝塞尔曲线模拟加速-匀速-减速
    # 回退到原始参数范围,保持自然的速度变化
    bezier_p1 = random.uniform(0.15, 0.25)  # 第一控制点(加速段)
    bezier_p2 = random.uniform(0.75, 0.85)  # 第二控制点(减速段)

    def bezier_ease(t):
        """三次贝塞尔缓动函数"""
        # B(t) = (1-t)³P0 + 3(1-t)²tP1 + 3(1-t)t²P2 + t³P3
        # P0=0, P3=1
        return 3 * (1 - t) ** 2 * t * bezier_p1 + 3 * (1 - t) * t ** 2 * bezier_p2 + t ** 3

    # === 微停顿点预生成 ===
    # 在20%-40%和60%-75%位置可能有微停顿(模拟人类视觉反馈调整)
    micro_pause_points = []
    if jitter and random.random() < 0.4:
        micro_pause_points.append(random.uniform(0.2, 0.4))
    if jitter and random.random() < 0.3:
        micro_pause_points.append(random.uniform(0.6, 0.75))

    # === 生成轨迹点 ===
    for index, row in enumerate(template):
        progress = index / (len(template) - 1) if len(template) > 1 else 0

        # === X轴: 贝塞尔缓动 + 智能抖动 ===
        eased_progress = bezier_ease(progress)
        x = int(round(row[0] * raw_x / base_x * (1 / bezier_ease(1) if bezier_ease(1) > 0 else 1)))

        if jitter and 0 < index < len(template) - 1:
            # 根据速度阶段调整抖动强度
            if progress < 0.15:  # 起始阶段: 小抖动
                jitter_x = random.choice([-1, 0, 0, 1])
            elif progress < 0.75:  # 中间阶段: 中等抖动
                jitter_x = random.choice([-2, -1, 0, 0, 0, 1, 2])
            else:  # 结束阶段: 小抖动(精细调整)
                jitter_x = random.choice([-1, -1, 0, 0, 1])

            # 5%概率出现"滑过头再回拉"(过冲现象)
            if 0.7 < progress < 0.95 and random.random() < 0.05:
                jitter_x += random.choice([2, 3])

            x += jitter_x

        x = max(0, min(raw_x, x))
        if x < last_x:
            x = last_x

        # === Y轴: 多层波动合成 ===
        y_base = int(row[1])
        y = y_base

        if jitter:
            # 主波动(手臂自然摆动)
            wave_main = math.sin((progress + y_phase_shift) * math.pi * y_wave_main_freq * 10) * y_wave_main_amp
            # 次波动(手指微抖)
            wave_sub = math.sin((progress + y_phase_shift) * math.pi * y_wave_sub_freq * 10) * y_wave_sub_amp
            # 线性漂移
            drift = y_drift_rate * progress
            # 随机噪声 - 降低
            noise = random.uniform(-0.4, 0.4)  # 从±0.8降至±0.4

            # 合成Y轴偏移 - 添加零点矫正
            y_offset = wave_main + wave_sub + drift + noise + y_center_bias

            # 在减速阶段(最后25%)增加Y轴稳定性(模拟用户集中注意力)
            if progress > 0.75:
                y_offset *= (1 - (progress - 0.75) / 0.25) * 0.6 + 0.4

            y += int(round(y_offset))

            # 偶尔的突发抖动(降低概率和幅度) - 进一步降低
            if random.random() < 0.01:  # 从0.015降至0.01
                y += random.choice([-1, 0, 1])  # 从±2降至±1

        # === 时间: 三阶段非线性时间分布 ===
        t = int(row[2] * time_scale + time_offset)

        if jitter and 0 < index < len(template) - 1:
            # 根据进度阶段调整时间间隔
            if progress < 0.15:  # 起始加速: 时间间隔递减
                time_jitter = random.randint(-5, 8)
            elif progress < 0.75:  # 匀速: 时间间隔较稳定
                time_jitter = random.randint(-3, 4)
            else:  # 减速微调: 时间间隔递增
                time_jitter = random.randint(-2, 6)

            t += time_jitter

            # 微停顿注入 - 降低停顿时长
            for pause_point in micro_pause_points:
                if abs(progress - pause_point) < 0.05:  # 在停顿点附近
                    t += random.randint(10, 22)
                    break

            # 随机突发加速(模拟用户"猛拉一下",降低概率)
            if 0.3 < progress < 0.6 and random.random() < 0.02:
                t -= random.randint(5, 10)

        # 确保时间单调递增
        if t <= last_t:
            t = last_t + random.randint(MOVE_INTERVAL_MIN_MS, MOVE_INTERVAL_MIN_MS + 6)

        out.append([x, y, t, trusted_code])
        last_x = x
        last_t = t

    # === 后处理 ===
    # 确保最后一个点精确到达目标X
    out[-1][0] = raw_x

    # 最后3-5个点模拟"微调对齐"(Y轴细微变化) - 减小调整幅度
    if jitter and len(out) >= 5:
        fine_tune_start = random.randint(-5, -3)
        for i in range(fine_tune_start, 0):
            out[i][1] += random.choice([-1, -1, 0, 0, 0, 1, 1])
            # 最后几个点时间间隔略增(模拟放慢确认) - 减小增量
            if i > fine_tune_start:
                out[i][2] += random.randint(2, 5)

    return normalize_track_timing(out)


def track_duration_ms(track: list[list[int]]) -> int:
    return max((int(row[2]) for row in track if len(row) >= 3), default=0)


def wait_for_browser_like_submit(
    challenge_ready_at: float | None,
    track: list[list[int]],
    wait_time_ms: int | None = None,
) -> dict:
    declared_ms = track_duration_ms(track)
    server_wait_ms = max(0, int(wait_time_ms or 0))
    # waitTime is a minimum readiness window. The drag duration already consumes
    # wall-clock time after the challenge is ready, so summing both is overly slow.
    required_ms = max(server_wait_ms, declared_ms) + SUBMIT_WAIT_BUFFER_MS
    elapsed_before_ms = int((time.monotonic() - challenge_ready_at) * 1000) if challenge_ready_at else 0
    sleep_ms = max(0, required_ms - elapsed_before_ms)
    if sleep_ms:
        time.sleep(sleep_ms / 1000)
    elapsed_after_ms = int((time.monotonic() - challenge_ready_at) * 1000) if challenge_ready_at else elapsed_before_ms
    return {
        "serverWaitMs": server_wait_ms,
        "declaredDurationMs": declared_ms,
        "requiredElapsedMs": required_ms,
        "elapsedBeforeCheckMs": elapsed_before_ms,
        "sleepBeforeCheckMs": sleep_ms,
        "elapsedAtCheckMs": elapsed_after_ms,
        "policy": "max(serverWaitMs, declaredDurationMs) + buffer",
        "bufferMs": SUBMIT_WAIT_BUFFER_MS,
    }


def build_check_data(token: str, raw_x: int, attrs: float, width: int = WIDTH, track: list[list[int]] | None = None) -> tuple[str, dict]:
    track = track or build_track(raw_x)
    trace_data = [xor_encode(token, js_join(row)) for row in track]
    sampled = sample_array(trace_data, SAMPLE_NUM)
    left = jigsaw_position(raw_x, attrs, width)["left"]
    p_raw = f"{int(left) / width * 100}"
    f_values = feature_array(unique_2d_array(track, 2))
    ext_raw = f"1,{len(trace_data)}"
    payload = {
        "d": aes(":".join(sampled)),
        "m": "",
        "p": aes(xor_encode(token, p_raw)),
        "f": aes(xor_encode(token, js_join(f_values))),
        "ext": aes(xor_encode(token, ext_raw)),
    }
    debug = {
        "rawX": raw_x,
        "jigsawLeft": left,
        "pRaw": p_raw,
        "extRaw": ext_raw,
        "traceLength": len(trace_data),
        "declaredDurationMs": track_duration_ms(track),
        "trustedCodes": sorted({row[3] for row in track if len(row) >= 4}),
        "fArray": f_values,
        "track": track,
    }
    return json.dumps(payload, separators=(",", ":")), debug


def build_intellisense_check_data(
    token: str,
    wait_time_ms: int = 300,
    trace: list[str] | None = None,
) -> tuple[str, dict]:
    trace = trace or []
    elapsed = max(0, int(wait_time_ms))
    encoded_trace = [xor_encode(token, item) for item in trace]
    p_raw = js_join([0, 0, elapsed])
    payload = {
        "d": "",
        "m": aes(":".join(sample_array(encoded_trace, SAMPLE_NUM))),
        "p": aes(xor_encode(token, p_raw)),
        "ext": aes(xor_encode(token, f"1,{len(encoded_trace)}")),
    }
    debug = {
        "pRaw": p_raw,
        "traceLength": len(encoded_trace),
        "waitTimeMs": elapsed,
    }
    return json.dumps(payload, separators=(",", ":")), debug


def is_intellisense_challenge(data: dict) -> bool:
    return str(data.get("type")) == "5" and not data.get("front") and not data.get("attrs")


def captcha_attrs(data: dict) -> float:
    attrs = data.get("attrs", 0.0)
    if isinstance(attrs, list):
        attrs = attrs[0] if attrs else 0.0
    if attrs in (None, ""):
        attrs = 0.0
    try:
        return float(attrs)
    except (TypeError, ValueError):
        return 0.0


def ensure_jigsaw_captcha(data: dict) -> None:
    missing = [key for key in ("bg", "front", "token") if not data.get(key)]
    if str(data.get("type")) != "2" or missing:
        raise RuntimeError(
            "unsupported captcha response: expected jigsaw type=2 with bg/front/token, "
            f"got type={data.get('type')!r}, missing={missing}"
        )


def parse_jsonp(text: str) -> dict:
    match = re.search(r"^[^(]*\((.*)\);?\s*$", text, re.S)
    if not match:
        return json.loads(text)
    return json.loads(match.group(1))


def callback_name(prefix: str = "__JSONP_py", suffix: int | str | None = None) -> str:
    name = f"{prefix}_{secrets.token_hex(4)}"
    return f"{name}_{suffix}" if suffix is not None else name


def normalize_proxy_url(proxy: str | None) -> str:
    proxy = str(proxy or "").strip()
    if not proxy or proxy.lower() in {"0", "false", "none", "no", "direct"}:
        return ""
    if "://" not in proxy:
        return f"http://{proxy}"
    return proxy


def requests_session(proxy: str | None = None) -> requests.Session:
    session = requests.Session()
    session.trust_env = False
    adapter = HTTPAdapter(pool_connections=HTTP_POOL_CONNECTIONS, pool_maxsize=HTTP_POOL_MAXSIZE, max_retries=0)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    proxy_url = normalize_proxy_url(proxy)
    if proxy_url:
        session.proxies.update({"http": proxy_url, "https": proxy_url})
    session.headers.update(
        {
            "Accept": "*/*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8,en-GB;q=0.7,en-US;q=0.6",
            "Connection": "keep-alive",
            "Sec-Fetch-Dest": "script",
            "Sec-Fetch-Mode": "no-cors",
            "Sec-Fetch-Site": "cross-site",
            "Sec-Fetch-Storage-Access": "active",
            "User-Agent": USER_AGENT,
            "sec-ch-ua": '"Chromium";v="149", "Google Chrome";v="149", "Not/A)Brand";v="99"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
        }
    )
    return session


def get_ir_token(session: requests.Session, template_path: Path = IR_TEMPLATE_PATH) -> str:
    """
    获取IR指纹token (支持指纹池轮换)
    IR (Intelligent Risk) 是网易易盾的设备指纹和行为特征系统
    """
    bodies = []
    ir_pool_dir = ASSET_DIR / "ir_pool"
    if ir_pool_dir.exists():
        pool_files = list(ir_pool_dir.glob("ir_fingerprint_*.json"))
        if pool_files:
            random.shuffle(pool_files)
            for item in pool_files:
                body = json.loads(item.read_text(encoding="utf-8"))
                body["n"] = secrets.token_hex(16)
                bodies.append((item.name, body))

    body = json.loads(template_path.read_text(encoding="utf-8"))
    body["n"] = secrets.token_hex(16)
    bodies.append((template_path.name, body))

    headers = {
        "Accept": "*/*",
        "Content-Type": "text/plain",
        "Origin": "null",
        "User-Agent": session.headers["User-Agent"],
    }
    errors = []
    for source, body in bodies:
        resp = session.post(
            "https://ir-sdk.dun.163.com/v4/j/up",
            data=json.dumps(body, separators=(",", ":")),
            headers=headers,
            timeout=15,
            verify=False,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("ok"):
            return data["data"]["tk"]
        errors.append({"source": source, "code": data.get("code"), "msg": data.get("msg")})
    raise RuntimeError(f"ir token failed: {errors}")


def getconf(session: requests.Session, ctx: SolverContext | None = None) -> dict:
    ctx = ctx or SolverContext()
    params = {
        "referer": ctx.referer,
        "zoneId": "",
        "dt": ctx.dt,
        "id": ctx.captcha_id,
        "type": ctx.captcha_type,
        "ipv6": "false",
        "runEnv": ctx.run_env,
        "iv": "5",
        "loadVersion": ctx.load_version,
        "callback": callback_name("__JSONP_conf", 0),
    }
    resp = session.get("https://c.dun.163.com/api/v2/getconf", params=params, timeout=15, verify=False)
    resp.raise_for_status()
    return parse_jsonp(resp.text)


def get_captcha(
    session: requests.Session,
    fp: str,
    ir_token: str,
    previous_token: str = "",
    ctx: SolverContext | None = None,
    request_type: str | None = None,
    request_width: int | str | None = None,
    size_type: str = "10",
) -> dict:
    ctx = ctx or SolverContext()
    captcha_type = ctx.captcha_type if request_type is None else str(request_type)
    width = ctx.width if request_width is None else request_width
    params = {
        "referer": ctx.referer,
        "zoneId": ctx.zone_id,
        "dt": ctx.dt,
        "irToken": ir_token,
        "id": ctx.captcha_id,
        "fp": fp,
        "https": "true",
        "type": captcha_type,
        "version": ctx.version,
        "dpr": "1",
        "dev": "1",
        "cb": gen_cb(),
        "ipv6": "false",
        "runEnv": ctx.run_env,
        "group": "",
        "scene": "",
        "lang": "zh-CN",
        "sdkVersion": "",
        "loadVersion": ctx.load_version,
        "iv": "4",
        "user": "",
        "width": str(width),
        "audio": "false",
        "sizeType": str(size_type),
        "smsVersion": "v3",
        "token": previous_token,
        "callback": callback_name("__JSONP_get", 0),
    }
    resp = session.get("https://c.dun.163.com/api/v3/get", params=params, timeout=15, verify=False)
    resp.raise_for_status()
    data = parse_jsonp(resp.text)
    if data.get("error") != 0:
        raise RuntimeError(f"get captcha failed: {data}")
    return data


def check_captcha(
    session: requests.Session,
    token: str,
    check_data: str,
    ctx: SolverContext | None = None,
    request_type: str | None = None,
    request_width: int | str | None = None,
    sdk_version: str | None = "",
) -> dict:
    ctx = ctx or SolverContext()
    captcha_type = ctx.captcha_type if request_type is None else str(request_type)
    width = ctx.width if request_width is None else request_width
    params = {
        "referer": ctx.referer,
        "zoneId": ctx.zone_id,
        "dt": ctx.dt,
        "id": ctx.captcha_id,
        "token": token,
        "data": check_data,
        "width": str(width),
        "type": captcha_type,
        "version": ctx.version,
        "cb": gen_cb(),
        "user": "",
        "extraData": "",
        "bf": "0",
        "runEnv": ctx.run_env,
        "sdkVersion": "" if sdk_version is None else str(sdk_version),
        "loadVersion": ctx.load_version,
        "iv": "4",
        "callback": callback_name("__JSONP_check", 1),
    }
    resp = session.get("https://c.dun.163.com/api/v3/check", params=params, timeout=15, verify=False)
    resp.raise_for_status()
    return parse_jsonp(resp.text)


def raw_candidates(raw_x: int, radius: int = 10, extra_raw_xs: list[int] | None = None, extra_radius: int = 2) -> list[int]:
    extras = [int(item) for item in extra_raw_xs or []]
    ordered = [raw_x, *extras]
    max_delta = max(radius, extra_radius if extras else 0)
    for delta in range(1, max_delta + 1):
        if delta <= radius:
            ordered.extend([raw_x - delta, raw_x + delta])
        if delta <= extra_radius:
            for item in extras:
                ordered.extend([item - delta, item + delta])
    seen = set()
    out = []
    for item in ordered:
        if 0 <= item <= 280 and item not in seen:
            seen.add(item)
            out.append(item)
    return out


def broad_raw_candidates(raw_x: int, radius: int, extra_raw_xs: list[int] | None = None) -> list[int]:
    candidates = raw_candidates(raw_x, radius=radius, extra_raw_xs=extra_raw_xs)
    if raw_x >= 250:
        # The SDK clamps jigsaw position after dragX=259. Add a coarse left-side
        # sweep because edge matches can be visually over-confident.
        candidates.extend(range(259, 79, -8))
    seen = set()
    out = []
    for item in candidates:
        if 0 <= item <= 280 and item not in seen:
            seen.add(item)
            out.append(item)
    return out


def track_variant(raw_x: int, name: str, template_path: Path = TRACK_TEMPLATE_PATH) -> list[list[int]]:
    if name == "template":
        return build_track_from_template(raw_x, jitter=False, template_path=template_path)
    if name == "template_jitter":
        return build_track_from_template(raw_x, jitter=True, template_path=template_path)
    if name == "random":
        return build_track(raw_x)
    raise ValueError(f"unknown track variant: {name}")


def check_with_retries(
    session: requests.Session,
    token: str,
    raw_x: int,
    attrs: float,
    attempts: int = 1,
    ctx: SolverContext | None = None,
    template_path: Path = TRACK_TEMPLATE_PATH,
    candidate_radius: int = 0,
    extra_raw_xs: list[int] | None = None,
    challenge_ready_at: float | None = None,
    challenge_wait_ms: int | None = None,
) -> tuple[dict, dict, list[dict]]:
    """优化版本：只使用识别出的精确RawX值进行一次验证"""
    ctx = ctx or SolverContext()
    logs = []

    # 只使用识别出的精确 RawX，但轨迹要保持人类拖动的自然抖动。
    track = build_track_from_template(raw_x, jitter=True, template_path=template_path)
    check_data, debug = build_check_data(token, raw_x, attrs, width=ctx.width, track=track)
    submit_timing = wait_for_browser_like_submit(challenge_ready_at, track, challenge_wait_ms)
    debug["submitTiming"] = submit_timing
    result = check_captcha(session, token, check_data, ctx=ctx)

    entry = {
        "attempt": 1,
        "track": "template_jitter",
        "rawX": raw_x,
        "pRaw": debug["pRaw"],
        "declaredDurationMs": debug["declaredDurationMs"],
        "sleepBeforeCheckMs": submit_timing["sleepBeforeCheckMs"],
        "trustedCodes": debug["trustedCodes"],
        "result": bool(result.get("data", {}).get("result")),
        "error": result.get("error"),
        "validate": result.get("data", {}).get("validate", ""),
    }
    logs.append(entry)

    return result, debug, logs


_RECOGNIZER_CACHE = None
_RECOGNIZER_CACHE_KEY: tuple[int] | None = None
_RECOGNIZER_MODULE_CACHE = None
RECOGNIZER_INIT_LOCK = threading.Lock()
MODEL_INFERENCE_LOCK = threading.Lock()


class RecognizerPool:
    def __init__(self, workers: list[tuple[object, object, object | None]]):
        self.workers = workers
        self._available: queue.Queue[tuple[object, object, object | None]] = queue.Queue()
        for worker in workers:
            self._available.put(worker)

    def run(
        self,
        sample: dict,
        out_dir: Path,
        *,
        save_artifacts: bool = True,
        render_overlay: bool = True,
        proxy: str | None = None,
    ) -> dict:
        worker = self._available.get()
        try:
            module, model, ctx = worker
            normalized = module.normalize_sample(sample)
            result = module.run_sample(
                model,
                ctx,
                normalized,
                out_dir,
                save_artifacts=save_artifacts,
                render_overlay=render_overlay,
                proxy=proxy,
                inference_lock=None,
            )
            rec = dict(result["result"])
            rec["top_scores"] = result.get("top_scores", [])
            rec["overlay_png_b64"] = result.get("overlay_png_b64", "")
            return rec
        finally:
            self._available.put(worker)

    def stats(self) -> dict:
        return {
            "enabled": True,
            "workers": len(self.workers),
            "available": self._available.qsize(),
            "mode": "model_pool",
        }


def _load_recognizer_module():
    global _RECOGNIZER_MODULE_CACHE
    if _RECOGNIZER_MODULE_CACHE is not None:
        return _RECOGNIZER_MODULE_CACHE
    module_path = ROOT / "网易_滑块增强版_识别.py"
    spec = importlib.util.spec_from_file_location("网易_滑块增强版_识别", module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"failed to load recognizer module: {module_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    _RECOGNIZER_MODULE_CACHE = module
    return module


def _load_recognizer(use_cache: bool = True, worker_count: int | None = None):
    global _RECOGNIZER_CACHE, _RECOGNIZER_CACHE_KEY
    count = DEFAULT_RECOGNIZER_WORKERS if worker_count is None else max(1, min(8, int(worker_count)))
    cache_key = (count,)
    if use_cache and _RECOGNIZER_CACHE is not None and _RECOGNIZER_CACHE_KEY == cache_key:
        return _RECOGNIZER_CACHE

    module = _load_recognizer_module()
    workers = []
    for _ in range(count):
        model = module.load_model()
        module.warmup_model(model)
        workers.append((module, model, None))
    _RECOGNIZER_CACHE = workers[0] if count == 1 else RecognizerPool(workers)
    _RECOGNIZER_CACHE_KEY = cache_key
    return _RECOGNIZER_CACHE


def load_recognizer(use_cache: bool = True, worker_count: int | None = None):
    with RECOGNIZER_INIT_LOCK:
        return _load_recognizer(use_cache=use_cache, worker_count=worker_count)


def recognizer_stats(recognizer=None) -> dict:
    item = recognizer if recognizer is not None else _RECOGNIZER_CACHE
    if isinstance(item, RecognizerPool):
        return item.stats()
    if item is not None:
        return {"enabled": True, "workers": 1, "available": 1, "mode": "single_model_locked"}
    return {"enabled": False, "workers": 0, "available": 0, "mode": "not_loaded"}


def recognize_sample(
    sample: dict,
    out_dir: Path,
    recognizer=None,
    save_artifacts: bool = True,
    render_overlay: bool = True,
    proxy: str | None = None,
) -> dict:
    recognizer = recognizer or load_recognizer()
    if isinstance(recognizer, RecognizerPool):
        return recognizer.run(
            sample,
            out_dir,
            save_artifacts=save_artifacts,
            render_overlay=render_overlay,
            proxy=proxy,
        )
    module, model, ctx = recognizer
    normalized = module.normalize_sample(sample)
    result = module.run_sample(
        model,
        ctx,
        normalized,
        out_dir,
        save_artifacts=save_artifacts,
        render_overlay=render_overlay,
        proxy=proxy,
        inference_lock=MODEL_INFERENCE_LOCK,
    )
    rec = dict(result["result"])
    rec["top_scores"] = result.get("top_scores", [])
    rec["overlay_png_b64"] = result.get("overlay_png_b64", "")
    return rec


def solve_once(
    captcha_id: str | None = None,
    referer: str | None = None,
    fp: str | None = DEFAULT_FP,
    out_dir: Path | None = None,
    save_debug: bool = True,
    check_attempts: int = 1,
    zone_id: str | None = None,
    dt: str | None = None,
    width: int | None = None,
    recognizer=None,
    candidate_radius: int = 0,
    top_score_limit: int = 0,
    refresh_fp: bool = True,
    render_overlay: bool = True,
    proxy: str | None = None,
    min_similarity: float = 0.0,
    captcha_type: str | None = None,
    allow_intellisense_precheck: bool = False,
    dynamic_fp: bool = True,
    fp_provider: str = FP_PROVIDER,
    node_path: str = NODE_PATH,
    local_fp_timeout: float = LOCAL_FP_TIMEOUT,
    local_env_fp: str = LOCAL_ENV_FP,
    local_fp_workers: int | None = DEFAULT_LOCAL_FP_WORKERS,
    chrome_debug_host: str = CHROME_DEBUG_HOST,
    chrome_debug_port: int = CHROME_DEBUG_PORT,
    chrome_debug_timeout: float = CHROME_DEBUG_TIMEOUT,
) -> dict:
    solve_started_at = time.perf_counter()
    timings: dict[str, float] = {}

    def record_timing(name: str, started_at: float) -> None:
        timings[name] = round((time.perf_counter() - started_at) * 1000, 1)

    warnings.filterwarnings("ignore", message="Unverified HTTPS request")
    out_dir = out_dir or (RUN_REVERSE_DIR / "protocol_runs")
    ctx = SolverContext.build(
        captcha_id=captcha_id,
        referer=referer,
        zone_id=zone_id,
        dt=dt,
        width=width,
        captcha_type=captcha_type,
    )
    fp_meta = {}
    fp = str(fp or "").strip()
    stage_started_at = time.perf_counter()
    if fp:
        fp_source = "payload_refresh_timestamp" if refresh_fp else "payload"
        if refresh_fp:
            fp = refresh_fp_timestamp(fp)
    elif dynamic_fp:
        fp, fp_meta = load_dynamic_fp(
            ctx.referer,
            provider=fp_provider,
            node_path=node_path,
            local_timeout=float(local_fp_timeout),
            env_fp=local_env_fp,
            local_worker_count=local_fp_workers,
            chrome_debug_host=chrome_debug_host,
            chrome_debug_port=int(chrome_debug_port),
            chrome_debug_timeout=float(chrome_debug_timeout),
        )
        fp_source = fp_meta.get("source") or fp_provider
    else:
        raise DynamicFpError("缺少 fp：dynamic_fp=false 且请求未传 fp")
    record_timing("fpMs", stage_started_at)
    if not is_valid_fp(fp):
        raise DynamicFpError("fp 无效：必须是浏览器 SDK 生成的长字符串并带毫秒时间戳")
    session = requests_session(proxy)
    ir_session = requests_session(proxy)
    stage_started_at = time.perf_counter()
    with ThreadPoolExecutor(max_workers=2) as executor:
        conf_future = executor.submit(getconf, session, ctx)
        ir_future = executor.submit(get_ir_token, ir_session)
        conf = conf_future.result()
        ir_token = ir_future.result()
    record_timing("getconfAndIrMs", stage_started_at)
    conf_data = conf.get("data") if isinstance(conf, dict) else {}
    if isinstance(conf_data, dict):
        ctx = replace(
            ctx,
            dt=conf_data.get("dt") or ctx.dt,
            zone_id=conf_data.get("zoneId") or ctx.zone_id,
        )
    stage_started_at = time.perf_counter()
    initial_captcha = get_captcha(session, fp, ir_token, ctx=ctx)
    record_timing("getCaptchaMs", stage_started_at)
    challenge_ready_at = time.monotonic()
    captcha = initial_captcha
    data = captcha["data"]
    intellisense = {}
    ctx = replace(ctx, zone_id=data.get("zoneId") or ctx.zone_id)
    if is_intellisense_challenge(data):
        if not allow_intellisense_precheck:
            result = {
                "error": 0,
                "msg": "intellisense challenge skipped in strict single-check mode",
                "data": {
                    "result": False,
                    "zoneId": ctx.zone_id,
                    "token": data.get("token", ""),
                    "validate": "",
                },
            }
            return {
                "request": {
                    "id": ctx.captcha_id,
                    "referer": ctx.referer,
                    "zoneId": ctx.zone_id,
                    "dt": ctx.dt,
                    "width": ctx.width,
                    "type": ctx.captcha_type,
                    "fp": fp,
                    "fpSource": fp_source,
                    "fpMeta": fp_meta,
                    "proxy": normalize_proxy_url(proxy),
                    "minSimilarity": min_similarity,
                },
                "conf": conf,
                "get": captcha,
                "intellisense": {"initialGet": initial_captcha, "skipped": True, "reason": "single_check_no_precheck"},
                "recognition": {},
                "checkAttempts": [],
                "checkDebug": {"skipReason": "single_check_no_precheck"},
                "check": result,
                "onVerify": {},
            }
        wait_time = int(data.get("waitTime") or 300) + random.randint(20, 180)
        precheck_data, precheck_debug = build_intellisense_check_data(data["token"], wait_time_ms=wait_time)
        precheck = check_captcha(
            session,
            data["token"],
            precheck_data,
            ctx=ctx,
            request_type="5",
            request_width=240,
            sdk_version="undefined",
        )
        captcha = get_captcha(
            session,
            fp,
            ir_token,
            previous_token=data["token"],
            ctx=ctx,
            request_type="",
            request_width=ctx.width,
        )
        challenge_ready_at = time.monotonic()
        intellisense = {
            "initialGet": initial_captcha,
            "precheck": precheck,
            "precheckDebug": precheck_debug,
        }
        data = captcha["data"]
        ctx = replace(ctx, zone_id=data.get("zoneId") or ctx.zone_id)
    ensure_jigsaw_captcha(data)
    ctx = replace(ctx, zone_id=data.get("zoneId") or ctx.zone_id, captcha_type=str(data.get("type") or ctx.captcha_type))
    sample = {
        "attrs": captcha_attrs(data),
        "bg": data["bg"],
        "front": data["front"],
        "token": data["token"],
        "type": data["type"],
        "waitTime": data["waitTime"],
        "zoneId": data["zoneId"],
    }
    stage_started_at = time.perf_counter()
    rec = recognize_sample(
        sample,
        out_dir / "recognition_samples",
        recognizer=recognizer,
        save_artifacts=save_debug,
        render_overlay=render_overlay,
        proxy=proxy,
    )
    record_timing("recognitionMs", stage_started_at)
    similarity = float(rec.get("similarity") or 0.0)
    if min_similarity > 0 and similarity < min_similarity:
        result = {
            "error": 0,
            "msg": "skipped low-confidence recognition",
            "data": {
                "result": False,
                "zoneId": ctx.zone_id,
                "token": data["token"],
                "validate": "",
            },
        }
        debug = {"skipReason": "low_similarity", "minSimilarity": min_similarity, "similarity": similarity}
        attempts = []
    else:
        # 不使用extra_raw_xs，只使用识别出的精确值
        stage_started_at = time.perf_counter()
        result, debug, attempts = check_with_retries(
            session,
            data["token"],
            int(rec["RawX"]),
            float(sample["attrs"]),
            attempts=check_attempts,
            ctx=ctx,
            candidate_radius=candidate_radius,
            extra_raw_xs=None,
            challenge_ready_at=challenge_ready_at,
            challenge_wait_ms=int(data.get("waitTime") or 0),
        )
        record_timing("checkStageMs", stage_started_at)
    onverify = {}
    raw_validate = result.get("data", {}).get("validate") if isinstance(result, dict) else ""
    if raw_validate:
        stage_started_at = time.perf_counter()
        onverify["validate"] = build_onverify_validate(raw_validate, fp, ctx.zone_id)
        record_timing("onVerifyWrapMs", stage_started_at)
    timings["totalMs"] = round((time.perf_counter() - solve_started_at) * 1000, 1)
    run = {
        "request": {
            "id": ctx.captcha_id,
            "referer": ctx.referer,
            "zoneId": ctx.zone_id,
            "dt": ctx.dt,
            "width": ctx.width,
            "type": ctx.captcha_type,
            "fp": fp,
            "fpSource": fp_source,
            "fpMeta": fp_meta,
            "proxy": normalize_proxy_url(proxy),
            "minSimilarity": min_similarity,
        },
        "conf": conf,
        "get": captcha,
        "intellisense": intellisense,
        "recognition": rec,
        "checkAttempts": attempts,
        "checkDebug": debug,
        "check": result,
        "onVerify": onverify,
        "timings": timings,
    }
    if save_debug:
        debug_run = json.loads(json.dumps(run, ensure_ascii=False))
        debug_run.get("recognition", {})["overlay_png_b64"] = ""
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / f"{data['token']}.json").write_text(json.dumps(debug_run, ensure_ascii=False, indent=2), encoding="utf-8")
    return run


def self_test() -> None:
    sample_path = TRACK_TEMPLATE_PATH
    sample = json.loads(sample_path.read_text(encoding="utf-8"))
    token = sample["token"]
    atom0 = sample["atomTraceData"][0]
    encoded0 = xor_encode(token, js_join(atom0))
    f_values = feature_array(sample["uniqueAtomTraceData"])
    print("xorEncode[0]", encoded0, "expected", sample["traceData"][0], encoded0 == sample["traceData"][0])
    print("fArray match", f_values == sample["fArray"])
    if f_values != sample["fArray"]:
        for i, (left, right) in enumerate(zip(f_values, sample["fArray"])):
            if left != right:
                print("first fArray diff", i, left, right)
                break
    print("cb sample", gen_cb()[:32], "len", len(gen_cb()))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--id", dest="captcha_id", default=CAPTCHA_ID)
    parser.add_argument("--referer", default=REFERER)
    parser.add_argument("--zone-id", default=ZONE_ID)
    parser.add_argument("--dt", default=DT)
    parser.add_argument("--width", type=int, default=WIDTH)
    parser.add_argument("--fp", default=DEFAULT_FP)
    parser.add_argument("--out-dir", type=Path, default=RUN_REVERSE_DIR / "protocol_runs")
    parser.add_argument("--check-attempts", type=int, default=1)
    parser.add_argument("--candidate-radius", type=int, default=0)
    parser.add_argument("--top-score-limit", type=int, default=0)
    parser.add_argument("--min-similarity", type=float, default=0.0)
    parser.add_argument("--type", dest="captcha_type", default=CAPTCHA_TYPE)
    parser.add_argument("--allow-intellisense-precheck", action="store_true", help="Allow one type=5 precheck before fetching a jigsaw challenge.")
    parser.add_argument("--no-refresh-fp", action="store_true", help="Reuse the fingerprint exactly as provided.")
    parser.add_argument("--no-dynamic-fp", action="store_true", help="Require --fp instead of generating a dynamic fingerprint.")
    parser.add_argument("--fp-provider", choices=["local", "chrome", "auto"], default=FP_PROVIDER)
    parser.add_argument("--node-path", default=NODE_PATH)
    parser.add_argument("--local-fp-timeout", type=float, default=LOCAL_FP_TIMEOUT)
    parser.add_argument("--local-env-fp", default=LOCAL_ENV_FP)
    parser.add_argument("--local-fp-workers", type=int, default=DEFAULT_LOCAL_FP_WORKERS)
    parser.add_argument("--chrome-debug-host", default=CHROME_DEBUG_HOST)
    parser.add_argument("--chrome-debug-port", type=int, default=CHROME_DEBUG_PORT)
    parser.add_argument("--chrome-debug-timeout", type=float, default=CHROME_DEBUG_TIMEOUT)
    parser.add_argument("--raw-check", action="store_true", help="Print the raw /api/v3/check response instead of the SDK onVerify payload.")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        self_test()
        return
    result = solve_once(
        captcha_id=args.captcha_id,
        referer=args.referer,
        fp=args.fp,
        out_dir=args.out_dir,
        check_attempts=args.check_attempts,
        zone_id=args.zone_id,
        dt=args.dt,
        width=args.width,
        candidate_radius=args.candidate_radius,
        top_score_limit=args.top_score_limit,
        refresh_fp=not args.no_refresh_fp,
        min_similarity=args.min_similarity,
        captcha_type=args.captcha_type,
        allow_intellisense_precheck=args.allow_intellisense_precheck,
        dynamic_fp=not args.no_dynamic_fp,
        fp_provider=args.fp_provider,
        node_path=args.node_path,
        local_fp_timeout=args.local_fp_timeout,
        local_env_fp=args.local_env_fp,
        local_fp_workers=args.local_fp_workers,
        chrome_debug_host=args.chrome_debug_host,
        chrome_debug_port=args.chrome_debug_port,
        chrome_debug_timeout=args.chrome_debug_timeout,
    )
    print(json.dumps(result["check"] if args.raw_check else result["onVerify"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
