#!/usr/bin/env python3
"""
Steam Manifest Downloader - Python Edition
Fully standalone, 100% standard library (no pip packages required).
Runs seamlessly on Linux (including SteamOS), Android (Termux/Pydroid), macOS, and Windows.
"""

import os
import sys
import json
import time
import uuid
import re
import zlib
import shutil
import base64
import subprocess
import urllib.request
import urllib.error
import urllib.parse
import threading
import queue
import webbrowser
import io
import struct
import lzma
import zipfile
import concurrent.futures
import ctypes
import ctypes.util
import hashlib
import atexit
import errno
import socket
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from pathlib import Path

# HTTP Connection Pool with Keep-Alive (eliminates TLS handshakes per chunk)
HTTP_POOL = None
try:
    import urllib3
    HTTP_POOL = urllib3.PoolManager(
        maxsize=64,
        num_pools=16,
        timeout=urllib3.Timeout(connect=10.0, read=30.0),
        retries=urllib3.Retry(total=3, backoff_factor=0.2)
    )
except Exception:
    HTTP_POOL = None

# ---------- Chunk decryption: several backends, each self-tested at startup ----------
IS_ANDROID = bool(
    os.environ.get("ANDROID_ROOT") or os.environ.get("ANDROID_DATA")
    or os.environ.get("IS_ANDROID_APP")
    or "pydroid" in str(Path.home()).lower() or "pydroid" in sys.executable.lower()
    or "com.termux" in str(Path.home()) or "com.steammanifestdownloader" in str(Path.home())
)


def _pkcs7_unpad(data: bytes) -> bytes:
    if not data or len(data) % 16:
        raise ValueError("decryption failed (bad block length)")
    n = data[-1]
    if n < 1 or n > 16 or data[-n:] != bytes([n]) * n:
        raise ValueError("decryption failed (wrong depot key or corrupt data)")
    return data[:-n]

def _check_cipher_input(key: bytes, raw: bytes):
    if len(key) != 32:
        raise ValueError("invalid depot key (need 32 bytes)")
    if len(raw) < 32 or (len(raw) - 16) % 16:
        raise ValueError("encrypted chunk has an invalid length")

# --- Backend 1: OpenSSL's libcrypto through ctypes (fastest, not always present on Android) ---
libcrypto = None
def _load_libcrypto():
    names = [ctypes.util.find_library("crypto"), "libcrypto.so.3", "libcrypto.so.1.1", "libcrypto.so",
             "libcrypto.dylib", "libcrypto-3-x64.dll", "libcrypto-1_1-x64.dll"]
    for name in [n for n in names if n]:
        try:
            lc = ctypes.CDLL(name)
            lc.EVP_CIPHER_CTX_new.restype = ctypes.c_void_p
            lc.EVP_CIPHER_CTX_free.argtypes = [ctypes.c_void_p]
            lc.EVP_aes_256_ecb.restype = ctypes.c_void_p
            lc.EVP_aes_256_cbc.restype = ctypes.c_void_p
            lc.EVP_CipherInit_ex.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_char_p, ctypes.c_char_p, ctypes.c_int]
            lc.EVP_CipherInit_ex.restype = ctypes.c_int
            lc.EVP_CIPHER_CTX_set_padding.argtypes = [ctypes.c_void_p, ctypes.c_int]
            lc.EVP_CIPHER_CTX_set_padding.restype = ctypes.c_int
            lc.EVP_CipherUpdate.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_int), ctypes.c_char_p, ctypes.c_int]
            lc.EVP_CipherUpdate.restype = ctypes.c_int
            lc.EVP_CipherFinal_ex.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_int)]
            lc.EVP_CipherFinal_ex.restype = ctypes.c_int
            return lc
        except Exception:
            continue
    return None

def _decrypt_libcrypto(key: bytes, raw: bytes) -> bytes:
    _check_cipher_input(key, raw)
    lc = libcrypto
    outl, finl = ctypes.c_int(0), ctypes.c_int(0)

    ctx = lc.EVP_CIPHER_CTX_new()
    if not ctx:
        raise ValueError("decryption failed (cipher context)")
    try:
        iv = ctypes.create_string_buffer(32)
        if lc.EVP_CipherInit_ex(ctx, lc.EVP_aes_256_ecb(), None, key, None, 0) != 1:
            raise ValueError("decryption failed (init)")
        lc.EVP_CIPHER_CTX_set_padding(ctx, 0)
        if lc.EVP_CipherUpdate(ctx, ctypes.byref(iv), ctypes.byref(outl), raw[:16], 16) != 1 or outl.value != 16:
            raise ValueError("decryption failed (iv)")
        iv_bytes = iv.raw[:16]
    finally:
        lc.EVP_CIPHER_CTX_free(ctx)

    ctx = lc.EVP_CIPHER_CTX_new()
    if not ctx:
        raise ValueError("decryption failed (cipher context)")
    try:
        body = raw[16:]
        if lc.EVP_CipherInit_ex(ctx, lc.EVP_aes_256_cbc(), None, key, iv_bytes, 0) != 1:
            raise ValueError("decryption failed (init)")
        lc.EVP_CIPHER_CTX_set_padding(ctx, 1)
        out = ctypes.create_string_buffer(len(body) + 32)
        if lc.EVP_CipherUpdate(ctx, ctypes.byref(out), ctypes.byref(outl), body, len(body)) != 1:
            raise ValueError("decryption failed (update)")
        n = outl.value
        if lc.EVP_CipherFinal_ex(ctx, ctypes.byref(out, n), ctypes.byref(finl)) != 1:
            raise ValueError("decryption failed (wrong depot key or corrupt data)")
        return out.raw[:n + finl.value]
    finally:
        lc.EVP_CIPHER_CTX_free(ctx)

# --- Backend 2: the "cryptography" package ---
def _make_cryptography_backend():
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    def decrypt(key, raw):
        _check_cipher_input(key, raw)
        iv = Cipher(algorithms.AES(key), modes.ECB()).decryptor().update(raw[:16])
        d = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
        return _pkcs7_unpad(d.update(raw[16:]) + d.finalize())
    return decrypt

# --- Backend 3: pycryptodome ---
def _make_pycryptodome_backend():
    try:
        from Crypto.Cipher import AES
    except ImportError:
        from Cryptodome.Cipher import AES
    def decrypt(key, raw):
        _check_cipher_input(key, raw)
        iv = AES.new(key, AES.MODE_ECB).decrypt(raw[:16])
        return _pkcs7_unpad(AES.new(key, AES.MODE_CBC, iv).decrypt(raw[16:]))
    return decrypt

# --- Backend 4: openssl command line (desktop Linux/macOS; usually missing on Android) ---
def _make_openssl_cli_backend():
    exe = shutil.which("openssl")
    if not exe:
        raise RuntimeError("openssl binary not found")
    def decrypt(key, raw):
        _check_cipher_input(key, raw)
        p1 = subprocess.run([exe, "enc", "-d", "-aes-256-ecb", "-K", key.hex(), "-nopad"], input=raw[:16], capture_output=True, timeout=20)
        if p1.returncode != 0 or len(p1.stdout) != 16:
            raise ValueError("decryption failed (openssl iv)")
        p2 = subprocess.run([exe, "enc", "-d", "-aes-256-cbc", "-K", key.hex(), "-iv", p1.stdout.hex()], input=raw[16:], capture_output=True, timeout=60)
        if p2.returncode != 0:
            raise ValueError("decryption failed (wrong depot key or corrupt data)")
        return p2.stdout
    return decrypt

# --- Backend 5: pure Python AES-256 (always works, but slow: last resort) ---
_AES_T = {}
def _aes_tables():
    if _AES_T:
        return _AES_T
    sbox = [0] * 256
    p = q = 1
    while True:
        p = (p ^ ((p << 1) & 0xFF) ^ (0x1B if p & 0x80 else 0)) & 0xFF
        q ^= (q << 1) & 0xFF
        q ^= (q << 2) & 0xFF
        q ^= (q << 4) & 0xFF
        if q & 0x80:
            q ^= 0x09
        q &= 0xFF
        rot = lambda v, n: ((v << n) | (v >> (8 - n))) & 0xFF
        sbox[p] = (q ^ rot(q, 1) ^ rot(q, 2) ^ rot(q, 3) ^ rot(q, 4) ^ 0x63) & 0xFF
        if p == 1:
            break
    sbox[0] = 0x63
    inv = [0] * 256
    for i, v in enumerate(sbox):
        inv[v] = i

    def mul(a, b):
        r = 0
        while b:
            if b & 1:
                r ^= a
            a = ((a << 1) ^ (0x11B if a & 0x80 else 0)) & 0x1FF
            b >>= 1
        return r & 0xFF

    td0 = []
    for x in range(256):
        s = inv[x]
        td0.append((mul(s, 14) << 24) | (mul(s, 9) << 16) | (mul(s, 13) << 8) | mul(s, 11))
    ror = lambda w, n: ((w >> n) | (w << (32 - n))) & 0xFFFFFFFF
    _AES_T.update(sbox=sbox, inv=inv, td0=td0,
                  td1=[ror(w, 8) for w in td0], td2=[ror(w, 16) for w in td0], td3=[ror(w, 24) for w in td0])
    return _AES_T

def _aes256_decrypt_key_schedule(key: bytes):
    t = _aes_tables()
    sbox = t["sbox"]
    w = list(struct.unpack(">8I", key))
    rcon = 1
    for i in range(8, 60):
        tmp = w[i - 1]
        if i % 8 == 0:
            tmp = ((tmp << 8) | (tmp >> 24)) & 0xFFFFFFFF
            tmp = (sbox[tmp >> 24] << 24) | (sbox[(tmp >> 16) & 255] << 16) | (sbox[(tmp >> 8) & 255] << 8) | sbox[tmp & 255]
            tmp ^= rcon << 24
            rcon = ((rcon << 1) ^ (0x11B if rcon & 0x80 else 0)) & 0xFF
        elif i % 8 == 4:
            tmp = (sbox[tmp >> 24] << 24) | (sbox[(tmp >> 16) & 255] << 16) | (sbox[(tmp >> 8) & 255] << 8) | sbox[tmp & 255]
        w.append(w[i - 8] ^ tmp)
    dk = []
    for r in range(15):
        dk.extend(w[4 * (14 - r): 4 * (14 - r) + 4])
    td0, td1, td2, td3 = t["td0"], t["td1"], t["td2"], t["td3"]
    for i in range(4, 56):   # InvMixColumns on the middle round keys
        v = dk[i]
        dk[i] = td0[sbox[v >> 24]] ^ td1[sbox[(v >> 16) & 255]] ^ td2[sbox[(v >> 8) & 255]] ^ td3[sbox[v & 255]]
    return dk

def _aes256_cbc_decrypt_pure(dk, iv: bytes, data: bytes) -> bytes:
    t = _aes_tables()
    td0, td1, td2, td3, inv = t["td0"], t["td1"], t["td2"], t["td3"], t["inv"]
    nblocks = len(data) // 16
    words = struct.unpack(">%dI" % (nblocks * 4), data)
    prev = struct.unpack(">4I", iv)
    out = []
    k = dk
    for b in range(nblocks):
        c0, c1, c2, c3 = words[4 * b: 4 * b + 4]
        s0, s1, s2, s3 = c0 ^ k[0], c1 ^ k[1], c2 ^ k[2], c3 ^ k[3]
        for r in range(1, 14):
            o = 4 * r
            t0 = td0[s0 >> 24] ^ td1[(s3 >> 16) & 255] ^ td2[(s2 >> 8) & 255] ^ td3[s1 & 255] ^ k[o]
            t1 = td0[s1 >> 24] ^ td1[(s0 >> 16) & 255] ^ td2[(s3 >> 8) & 255] ^ td3[s2 & 255] ^ k[o + 1]
            t2 = td0[s2 >> 24] ^ td1[(s1 >> 16) & 255] ^ td2[(s0 >> 8) & 255] ^ td3[s3 & 255] ^ k[o + 2]
            t3 = td0[s3 >> 24] ^ td1[(s2 >> 16) & 255] ^ td2[(s1 >> 8) & 255] ^ td3[s0 & 255] ^ k[o + 3]
            s0, s1, s2, s3 = t0, t1, t2, t3
        o = 56
        p0 = ((inv[s0 >> 24] << 24) | (inv[(s3 >> 16) & 255] << 16) | (inv[(s2 >> 8) & 255] << 8) | inv[s1 & 255]) ^ k[o] ^ prev[0]
        p1 = ((inv[s1 >> 24] << 24) | (inv[(s0 >> 16) & 255] << 16) | (inv[(s3 >> 8) & 255] << 8) | inv[s2 & 255]) ^ k[o + 1] ^ prev[1]
        p2 = ((inv[s2 >> 24] << 24) | (inv[(s1 >> 16) & 255] << 16) | (inv[(s0 >> 8) & 255] << 8) | inv[s3 & 255]) ^ k[o + 2] ^ prev[2]
        p3 = ((inv[s3 >> 24] << 24) | (inv[(s2 >> 16) & 255] << 16) | (inv[(s1 >> 8) & 255] << 8) | inv[s0 & 255]) ^ k[o + 3] ^ prev[3]
        out.extend((p0, p1, p2, p3))
        prev = (c0, c1, c2, c3)
    return struct.pack(">%dI" % len(out), *out)

def _make_pure_python_backend():
    cache = {}
    def decrypt(key, raw):
        _check_cipher_input(key, raw)
        dk = cache.get(key)
        if dk is None:
            dk = cache[key] = _aes256_decrypt_key_schedule(key)
        iv = _aes256_cbc_decrypt_pure(dk, bytes(16), raw[:16])   # ECB == CBC with a zero IV for one block
        return _pkcs7_unpad(_aes256_cbc_decrypt_pure(dk, iv, raw[16:]))
    return decrypt

# Known-answer test vector (key = 00..1f, generated with OpenSSL): proves a backend really decrypts
_SELFTEST_KEY = bytes(range(32))
_SELFTEST_RAW = bytes.fromhex("a60591fefef6dfa04306c3d6490e45802b63dc248ad8faafc2eda3dcd2a4347924c333076664949df41dc8a6e465df7dda77aa816b820b67a07cbb9ba2885325")
_SELFTEST_PLAIN = b"Steam chunk self-test 0123456789!"

def _select_crypto_backend():
    global libcrypto
    libcrypto = _load_libcrypto()
    makers = []
    if libcrypto is not None:
        makers.append(("libcrypto", lambda: _decrypt_libcrypto))
    makers += [("cryptography", _make_cryptography_backend),
               ("pycryptodome", _make_pycryptodome_backend),
               ("openssl-cli", _make_openssl_cli_backend),
               ("pure-python", _make_pure_python_backend)]
    for name, maker in makers:
        try:
            fn = maker()
            if fn(_SELFTEST_KEY, _SELFTEST_RAW) == _SELFTEST_PLAIN:
                return name, fn
        except Exception:
            continue
    return "none", None

CRYPTO_BACKEND_NAME, _crypto_decrypt = _select_crypto_backend()

def crypto_backend_note() -> str:
    if CRYPTO_BACKEND_NAME == "pure-python":
        return "Decryption: built-in pure-Python AES (slow). For full speed run: pip install pycryptodome"
    return f"Decryption: {CRYPTO_BACKEND_NAME}"

def decode_chunk_payload(decrypted: bytes) -> bytes:
    """Decompresses a decrypted chunk: LZMA (VZa), zstd (VSZa), zip (PK), zlib or stored."""
    try:
        if len(decrypted) >= 16 and decrypted[:3] == b"VZa":
            header = decrypted[7:12]
            body_end = len(decrypted) - 10
            expected_crc, expected_size = struct.unpack_from("<II", decrypted, body_end)
            out = lzma.decompress(header + struct.pack("<Q", expected_size) + decrypted[12:body_end], format=lzma.FORMAT_ALONE)
            if len(out) != expected_size:
                raise ValueError(f"lzma size {len(out)} != {expected_size}")
            return out
        if decrypted[:4] == b"VSZa":
            frame = decrypted[8:-15]
            try:
                import zstandard
                return zstandard.ZstdDecompressor().decompressobj().decompress(frame)
            except ImportError:
                pass
            try:
                from compression import zstd   # Python 3.14+
                return zstd.decompress(frame)
            except ImportError:
                raise RuntimeError("This depot uses zstd chunks. Install support with: pip install zstandard")
        if decrypted[:2] == b"PK":
            with zipfile.ZipFile(io.BytesIO(decrypted)) as zf:
                return zf.read(zf.namelist()[0])
        if len(decrypted) >= 2 and decrypted[0] == 0x78 and ((decrypted[0] << 8) | decrypted[1]) % 31 == 0:
            try:
                return zlib.decompress(decrypted)
            except zlib.error:
                return decrypted     # looked like zlib but was not: size/SHA check decides
    except RuntimeError:
        raise
    except Exception as e:
        raise ValueError(f"decompression failed ({e})")
    return decrypted

def process_chunk(raw_chunk: bytes, depot_key_hex: str) -> bytes:
    """Decrypt + decompress one chunk. Raises ValueError if it cannot be decrypted:
    undecrypted (raw) bytes are NEVER returned as if they were valid data."""
    if len(depot_key_hex or "") != 64:
        raise ValueError("invalid depot key")
    if _crypto_decrypt is None:
        raise RuntimeError("No working AES backend found. Install one with: pip install pycryptodome")
    key = bytes.fromhex(depot_key_hex)
    return decode_chunk_payload(_crypto_decrypt(key, raw_chunk))


BASE_DIR = Path(os.environ.get("SMD_WEB_DIR", Path(__file__).resolve().parent))
DATA_DIR = Path(os.environ.get("SMD_DATA_DIR", Path.home() / ".local" / "share" / "steam-manifest-downloader"))
DATA_DIR.mkdir(parents=True, exist_ok=True)

SETTINGS_FILE = DATA_DIR / "settings.json"
HISTORY_FILE  = DATA_DIR / "history.json"
FOLLOWUP_FILE = DATA_DIR / "pending_followup.json"
TEMP_UPLOADS  = DATA_DIR / "uploads"
TEMP_UPLOADS.mkdir(parents=True, exist_ok=True)

# ---------- SSE Event Streaming & Android Bridge ----------
sse_clients = []
sse_lock = threading.Lock()

def emit_event(event_name, payload):
    # 1. Broadcast to SSE Web Clients
    with sse_lock:
        data = f"data: {json.dumps({'event': event_name, 'payload': payload})}\n\n"
        dead = []
        for q in sse_clients:
            try:
                q.put_nowait(data)
            except Exception:
                dead.append(q)
        for d in dead:
            if d in sse_clients:
                sse_clients.remove(d)

    # 2. Direct Android Notification & Foreground Service Bridge
    if IS_ANDROID:
        try:
            from com.steammanifestdownloader import AndroidBridgeManager
            AndroidBridgeManager.onDownloadEvent(event_name, json.dumps(payload))
        except Exception:
            pass

# ---------- Settings & Persistence ----------
DEFAULT_DOWNLOAD_LOCATION = os.environ.get("SMD_DOWNLOAD_DIR")
if not DEFAULT_DOWNLOAD_LOCATION:
    if IS_ANDROID:
        DEFAULT_DOWNLOAD_LOCATION = "/storage/emulated/0/Download/SteamDownloads"
    else:
        DEFAULT_DOWNLOAD_LOCATION = str(Path.home() / "Downloads" / "SteamDownloads")

DEFAULT_SETTINGS = {
    "download_location": DEFAULT_DOWNLOAD_LOCATION,
    "dd_extra_args": ["-max-downloads", "16", "-verify-all"],
    "auto_update": False,
    "max_retries": 3,
    "notification_sound": True,
    "download_speed_limit": "",
    "native_chunk_concurrency": 16,
    "proxy": "",
    "telemetry_consent": "declined",
    "installation_id": str(uuid.uuid4()),
    "depot_sources": [
        "https://archive.org/download/manifest-hub-repo/NEW-depot-keys.zip/",
        "https://archive.org/download/manifest-hub-repo/branches.zip/"
    ],
    "language": "en",
    "auto_seeded": True,
    "pristine_default_sources": [],
    "hubcap_api_key": "",
    "use_native_downloader": True,
    "cancel_keep_files": False,
    "ryuu_api_key": "",
    "auto_select_depots": True,
    "auto_start_download": False,
    "include_dlc": True,
    "game_language": "english",
    "target_platform": "windows",
    "steam_path": "",
    "verify_chunks": True
}

def load_settings():
    if SETTINGS_FILE.exists():
        try:
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                s = json.load(f)
                res = DEFAULT_SETTINGS.copy()
                res.update(s)
                return res
        except Exception:
            pass
    return DEFAULT_SETTINGS.copy()

def save_settings(data):
    try:
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        return True
    except Exception as e:
        print(f"Error saving settings: {e}")
        return False

def load_history():
    if HISTORY_FILE.exists():
        try:
            with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"entries": []}

def save_history(data):
    try:
        with open(HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        return True
    except Exception as e:
        print(f"Error saving history: {e}")
        return False

def load_pending_followup():
    if FOLLOWUP_FILE.exists():
        try:
            with open(FOLLOWUP_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return None

def save_pending_followup(data):
    try:
        with open(FOLLOWUP_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        return True
    except Exception:
        return False

def clear_pending_followup():
    try:
        if FOLLOWUP_FILE.exists():
            FOLLOWUP_FILE.unlink()
        return True
    except Exception:
        return False

# ---------- HTTP Helpers ----------
def fetch_url(url, headers=None, timeout=20):
    req_headers = {"User-Agent": "Valve/Steam HTTP Client 1.0", "Accept": "*/*"}
    if headers:
        req_headers.update(headers)
    req = urllib.request.Request(url, headers=req_headers)
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return response.read()

def fetch_json(url, headers=None, timeout=15):
    raw = fetch_url(url, headers=headers, timeout=timeout)
    return json.loads(raw.decode("utf-8", errors="ignore"))

def head_url(url, headers=None, timeout=10):
    req_headers = {"User-Agent": "SteamManifestDownloader/1.5.0"}
    if headers:
        req_headers.update(headers)
    req = urllib.request.Request(url, headers=req_headers, method='HEAD')
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return response.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception:
        return 0

# ---------- Parsers ----------
def parse_lua_content(content):
    depot_map = {}
    main_app_id = None
    all_app_ids = []

    add_pattern = re.compile(r'(?i)addappid\((\d+)(?:\s*,\s*(\d+)\s*,\s*"([a-f0-9]+)")?\)')
    for match in add_pattern.finditer(content):
        app_id = int(match.group(1))
        key = match.group(3)
        if app_id not in all_app_ids:
            all_app_ids.append(app_id)
        if not key:
            if main_app_id is None:
                main_app_id = app_id
        else:
            if app_id not in depot_map:
                depot_map[app_id] = {
                    "depot_id": app_id,
                    "depot_key": key,
                    "manifest_id": None,
                    "size_bytes": None
                }
            else:
                depot_map[app_id]["depot_key"] = key

    set_pattern = re.compile(r'(?i)setManifestid\((\d+)\s*,\s*"(\d+)"(?:\s*,\s*(\d+))?[^)]*\)')
    for match in set_pattern.finditer(content):
        depot_id = int(match.group(1))
        manifest_id = match.group(2)
        size_bytes = int(match.group(3)) if match.group(3) else None
        if depot_id not in depot_map:
            depot_map[depot_id] = {
                "depot_id": depot_id,
                "depot_key": None,
                "manifest_id": manifest_id,
                "size_bytes": size_bytes
            }
        else:
            depot_map[depot_id]["manifest_id"] = manifest_id
            if size_bytes:
                depot_map[depot_id]["size_bytes"] = size_bytes

    if not main_app_id and all_app_ids:
        main_app_id = all_app_ids[0]

    return {
        "main_app_id": main_app_id,
        "depots": list(depot_map.values()),
        "all_app_ids": all_app_ids
    }

def parse_st_file(buffer):
    if len(buffer) < 12:
        raise ValueError("File too small for .st header")
    xor_key_raw = int.from_bytes(buffer[0:4], "little")
    size = int.from_bytes(buffer[4:8], "little")
    xor_key = ((xor_key_raw ^ 0xFFFEA4C8) & 0xFF)
    encrypted = buffer[12:12 + size]
    decrypted = bytes([b ^ xor_key for b in encrypted])
    decompressed = zlib.decompress(decrypted)
    if len(decompressed) <= 512:
        raise ValueError(".st decompressed payload too small")
    lua_text = decompressed[512:].decode("utf-8", errors="ignore")
    return parse_lua_content(lua_text)

def parse_vdf_keys(text):
    keys = {}
    pattern = re.compile(r'"(\d+)"\s*\{[^}]*?"DecryptionKey"\s*"([0-9a-fA-F]+)"', re.DOTALL)
    for m in pattern.finditer(text):
        keys[m.group(1)] = m.group(2)
    return keys

def extract_depot_sizes(json_data):
    sizes = {}
    depots = json_data.get("depot", {}) if isinstance(json_data, dict) else {}
    for depot_id, d_val in depots.items():
        manifests = d_val.get("manifests", {}) if isinstance(d_val, dict) else {}
        for branch, b_data in manifests.items():
            if isinstance(b_data, dict):
                gid = str(b_data.get("gid", ""))
                try:
                    s = int(b_data.get("size", 0))
                    if s > 100:
                        sizes[(depot_id, gid)] = s
                except Exception:
                    pass
    return sizes

# ---------- AES Decryption (via openssl subprocess) ----------
def aes_ecb_decrypt(key_hex: str, data: bytes) -> bytes:
    try:
        result = subprocess.run(
            ["openssl", "enc", "-aes-256-ecb", "-d", "-nopad", "-K", key_hex, "-nosalt"],
            input=data, capture_output=True, check=True
        )
        return result.stdout
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"AES decrypt failed: {e.stderr.decode(errors='ignore')}") from e

# ---------- Protobuf Manifest Parser ----------
def read_varint(buf, pos):
    result = 0
    shift = 0
    while pos < len(buf):
        b = buf[pos]; pos += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            break
        shift += 7
    return result, pos

def read_protobuf_field(buf, pos):
    if pos >= len(buf):
        return None
    tag, pos = read_varint(buf, pos)
    field_number = tag >> 3
    wire_type = tag & 0x07
    if wire_type == 0:
        v, pos = read_varint(buf, pos)
        return field_number, wire_type, v, pos
    elif wire_type == 2:
        length, pos = read_varint(buf, pos)
        value = buf[pos:pos + length]
        return field_number, wire_type, value, pos + length
    elif wire_type == 1:
        return field_number, wire_type, buf[pos:pos+8], pos + 8
    elif wire_type == 5:
        return field_number, wire_type, buf[pos:pos+4], pos + 4
    else:
        return None

def parse_manifest_protobuf(data: bytes):
    MAGIC = 0x71F617D0
    if len(data) < 8:
        raise ValueError("Manifest payload too small")
    magic = int.from_bytes(data[0:4], "little")
    if magic != MAGIC:
        raise ValueError(f"Bad manifest magic: {magic:#x}")
    payload = data[8:]
    chunks = []
    pos = 0
    while pos < len(payload):
        f = read_protobuf_field(payload, pos)
        if f is None:
            break
        field_number, wire_type, value, pos = f
        if field_number == 1 and wire_type == 2:
            mpos = 0
            while mpos < len(value):
                mf = read_protobuf_field(value, mpos)
                if mf is None:
                    break
                mfield, mwire, mvalue, mpos = mf
                if mfield == 2 and mwire == 2:
                    chunk = {}
                    cpos = 0
                    while cpos < len(mvalue):
                        cf = read_protobuf_field(mvalue, cpos)
                        if cf is None:
                            break
                        cf_num, cf_wire, cf_val, cpos = cf
                        if cf_num == 1 and cf_wire == 2:   chunk["sha"] = cf_val.hex()
                        elif cf_num == 3 and cf_wire == 0: chunk["offset"] = cf_val
                        elif cf_num == 4 and cf_wire == 0: chunk["uncompressed_size"] = cf_val
                        elif cf_num == 5 and cf_wire == 0: chunk["size"] = cf_val
                    if "sha" in chunk:
                        chunks.append(chunk)
    return chunks

# ---------- Manifest Request Code ----------
MANIFEST_CODE_URLS = [
    "https://manifest.steam.run/api/manifest/{gid}",
    "http://gmrc.wudrm.com/manifest/{gid}"
]

def get_manifest_request_code(manifest_gid: str) -> int:
    for url_tpl in MANIFEST_CODE_URLS:
        try:
            url = url_tpl.format(gid=manifest_gid)
            raw = fetch_url(url, timeout=8)
            text = raw.decode("utf-8", errors="ignore").strip()
            try:
                j = json.loads(text)
                val = j.get("content") or j.get("manifest_request_code") or 0
                return int(val)
            except Exception:
                if text.isdigit():
                    return int(text)
        except Exception:
            continue
    return 0

# ---------- Dynamic Steam Content Server Discovery & Ranking ----------
RANKED_CDN_CACHE = {"ts": 0, "hosts": [], "speeds": {}, "bench_ts": 0}
RANKED_CDN_LOCK = threading.Lock()

DEFAULT_FALLBACK_HOSTS = [
    "steampipe.akamaized.net",
    "cache4-dxb1.steamcontent.com",
    "cache1-dxb1.steamcontent.com",
    "cache3-dxb1.steamcontent.com",
    "cache2-dxb1.steamcontent.com",
    "fastly.cdn.steampipe.steamcontent.com",
    "google2.cdn.steampipe.steamcontent.com",
]

def probe_host_latency(host: str, timeout: float = 1.2) -> float:
    """Reachability check only (TCP connect). Not used to decide which server is best."""
    t0 = time.time()
    try:
        s = socket.create_connection((host, 443), timeout=timeout)
        s.close()
        return (time.time() - t0) * 1000.0
    except Exception:
        return 99999.0

def get_ranked_cdn_hosts(force_refresh: bool = False) -> list:
    """Returns the list of reachable Steam content servers.
    Before a speed test has run, order is: known-good defaults first, then the rest.
    After benchmark_cdn_hosts() has run, order is by measured download speed."""
    global RANKED_CDN_CACHE
    now = time.time()
    with RANKED_CDN_LOCK:
        if not force_refresh and RANKED_CDN_CACHE["hosts"] and (now - RANKED_CDN_CACHE["ts"] < 1800):
            return list(RANKED_CDN_CACHE["hosts"])

    candidate_hosts = set(DEFAULT_FALLBACK_HOSTS)

    # 1. Ask Steam's directory service which content servers serve this client
    try:
        url = "https://api.steampowered.com/IContentServerDirectoryService/GetServersForSteamPipe/v1/"
        req = urllib.request.Request(url, headers={"User-Agent": "Valve/Steam HTTP Client 1.0"})
        with urllib.request.urlopen(req, timeout=3.5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            for s in data.get("response", {}).get("servers", []):
                h = s.get("host")
                if h and s.get("https_support") != "unavailable":
                    candidate_hosts.add(h)
    except Exception as e:
        print(f"Notice: Steam server directory unreachable ({e}), using defaults.")

    # 2. Drop servers that cannot be reached at all (this is NOT a ranking step)
    reachable = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(24, len(candidate_hosts))) as pool:
        future_map = {pool.submit(probe_host_latency, h): h for h in candidate_hosts}
        for fut in concurrent.futures.as_completed(future_map):
            h = future_map[fut]
            try:
                lat = fut.result()
            except Exception:
                lat = 99999.0
            if lat < 99990.0:
                reachable.append((h, lat))

    default_order = {h: i for i, h in enumerate(DEFAULT_FALLBACK_HOSTS)}
    reachable.sort(key=lambda x: (default_order.get(x[0], 999), x[1]))
    ranked = [h for h, _ in reachable] or list(DEFAULT_FALLBACK_HOSTS)

    with RANKED_CDN_LOCK:
        RANKED_CDN_CACHE["ts"] = now
        RANKED_CDN_CACHE["hosts"] = ranked
        RANKED_CDN_CACHE["speeds"] = {}
        RANKED_CDN_CACHE["bench_ts"] = 0

    return ranked

def short_host(host: str) -> str:
    for suffix in (".steamcontent.com", ".steampipe.steamcontent.com", ".akamaized.net", ".cdn"):
        if host.endswith(suffix):
            host = host[: -len(suffix)]
    return host

def measure_host_speed(host: str, depot_id: str, shas: list, max_seconds: float = 2.5):
    """Downloads real chunks from one host and returns measured MB/s, or None if it failed."""
    total = 0
    t0 = time.time()
    for sha in shas:
        url = f"{CHUNK_URL_SCHEME}://{host}/depot/{depot_id}/chunk/{sha}"
        try:
            if HTTP_POOL is not None:
                resp = HTTP_POOL.request(
                    "GET", url, preload_content=False, retries=False,
                    headers={"User-Agent": "Valve/Steam HTTP Client 1.0", "Accept": "*/*"},
                    timeout=urllib3.Timeout(connect=3.0, read=3.0))
                if resp.status != 200:
                    resp.close()
                    return None
                for block in resp.stream(65536):
                    total += len(block)
                    if time.time() - t0 > max_seconds:
                        break
                if time.time() - t0 > max_seconds:
                    resp.close()
                    break
                resp.release_conn()
            else:
                req = urllib.request.Request(url, headers={"User-Agent": "Valve/Steam HTTP Client 1.0"})
                with urllib.request.urlopen(req, timeout=3.0) as r:
                    while True:
                        block = r.read(65536)
                        if not block:
                            break
                        total += len(block)
                        if time.time() - t0 > max_seconds:
                            break
        except Exception:
            return None
        if time.time() - t0 > max_seconds:
            break
    elapsed = time.time() - t0
    if total < 16 * 1024 or elapsed <= 0:
        return None
    return (total / 1048576.0) / elapsed

def benchmark_cdn_hosts(depot_id: str, sample_chunks: list, max_hosts: int = 8, force: bool = False):
    """Speed-tests servers with real chunks from this depot (one at a time, so they don't
    steal bandwidth from each other). Results are cached in memory and on disk for 30 minutes.
    Returns (ranked_hosts, speeds_dict, from_cache)."""
    hosts = get_ranked_cdn_hosts()
    now = time.time()
    if not force:
        with RANKED_CDN_LOCK:
            mem = dict(RANKED_CDN_CACHE.get("speeds") or {})
            fresh = now - RANKED_CDN_CACHE.get("bench_ts", 0) < SPEED_CACHE_TTL
        cached = mem if (mem and fresh) else load_speed_cache()
        known = {h: cached[h] for h in hosts if h in cached}
        if len(known) >= min(2, len(hosts)):
            ranked = sorted(known, key=lambda h: known[h], reverse=True) + [h for h in hosts if h not in known]
            with RANKED_CDN_LOCK:
                RANKED_CDN_CACHE.update(hosts=ranked, speeds=dict(known), bench_ts=now)
            return ranked, known, True

    usable = [c for c in sample_chunks if 200 * 1024 <= (c.get("size") or 0) <= 1100 * 1024]
    usable.sort(key=lambda c: c.get("size", 0), reverse=True)
    shas = [c["sha"] for c in usable[:2]] or [c["sha"] for c in sorted(sample_chunks, key=lambda c: c.get("size", 0), reverse=True)[:2]]
    if not shas:
        return hosts, {}, False

    speeds = {}
    for h in hosts[:max_hosts]:
        s = measure_host_speed(h, depot_id, shas, max_seconds=2.0)
        if s is not None:
            speeds[h] = s
    if not speeds:
        return hosts, {}, False

    ranked = sorted(speeds, key=lambda h: speeds[h], reverse=True) + [h for h in hosts if h not in speeds]
    with RANKED_CDN_LOCK:
        RANKED_CDN_CACHE.update(hosts=ranked, ts=now, speeds=dict(speeds), bench_ts=now)
    save_speed_cache(speeds)
    return ranked, speeds, False

# ---------- Live server pool: weighted load distribution + stability rules ----------
import random

SPEED_CACHE_FILE = DATA_DIR / "server_speeds.json"
CHUNK_URL_SCHEME = "https"
SPEED_CACHE_TTL = 1800.0   # a speed test stays valid for 30 minutes

class ChunkMiss(Exception):
    """This server does not have the chunk (404/403). Not the server's fault."""

class HostFailure(Exception):
    """Network/server problem (timeout, reset, 5xx). Counts against the server."""

def device_connection_cap() -> int:
    """Maximum parallel connections this device should open."""
    cap = 12 if IS_ANDROID else 32
    try:
        import resource
        soft = resource.getrlimit(resource.RLIMIT_NOFILE)[0]
        if soft and soft != resource.RLIM_INFINITY and soft > 0:
            cap = min(cap, max(4, (soft - 64) // 8))
    except Exception:
        pass
    return max(2, cap)

def load_speed_cache() -> dict:
    try:
        data = json.loads(SPEED_CACHE_FILE.read_text(encoding="utf-8"))
        if time.time() - float(data.get("ts", 0)) < SPEED_CACHE_TTL:
            return {h: float(s) for h, s in (data.get("speeds") or {}).items() if float(s) > 0}
    except Exception:
        pass
    return {}

def save_speed_cache(speeds: dict):
    try:
        merged = load_speed_cache()
        merged.update({h: round(float(s), 3) for h, s in speeds.items() if s and s > 0})
        tmp = SPEED_CACHE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps({"ts": time.time(), "speeds": merged}), encoding="utf-8")
        os.replace(tmp, SPEED_CACHE_FILE)
    except Exception:
        pass   # a read-only/odd storage location must never break a download

class ServerPool:
    """Tracks every server's real speed while chunks stream in, shares work between the
    fast ones in proportion to their speed, and avoids flip-flopping between servers."""
    RERANK_INTERVAL = 30.0   # seconds between re-rankings
    SWITCH_MARGIN = 1.25     # a challenger must be 25% faster before it replaces the primary
    DROP_RATIO = 0.5         # a window under 50% of the usual speed counts as a drop...
    DROP_WINDOWS = 2         # ...and only 2 drops in a row are treated as real
    EXCLUDE_RATIO = 0.15     # persistently below 15% of the best server -> paused
    FAIL_LIMIT = 3           # consecutive network failures before a server is paused
    COOLDOWN_BASE = 60.0     # first pause length; doubles on each repeat
    COOLDOWN_MAX = 600.0
    MIN_WINDOW_SECS = 1.0    # ignore windows with less transfer time than this
    TRICKLE = 0.05           # slow servers keep a 5% share so their speed stays measured

    def __init__(self, hosts, seed_speeds=None, total_conns=16, notify=None, clock=time.time):
        self.lock = threading.Lock()
        self.clock = clock
        self.notify = notify
        self.total_conns = max(1, int(total_conns))
        seeds = {h: s for h, s in (seed_speeds or {}).items() if s and s > 0}
        default = (sorted(seeds.values())[len(seeds) // 2] * 0.5) if seeds else 1.0
        self.st = {}
        for h in hosts:
            speed = seeds.get(h, default)
            self.st[h] = {"base": speed, "cur": speed, "eff": speed, "strikes": 0, "fails": 0,
                          "active": 0, "excluded_until": 0.0, "cool_n": 0, "probation": False,
                          "win_bytes": 0, "win_secs": 0.0, "total_bytes": 0, "chunks": 0, "cap": self.total_conns}
        self.last_rerank = clock()
        self.primary = None
        self._recompute()

    # ----- internals (call with lock held) -----
    def _say(self, text):
        if self.notify:
            try:
                self.notify(text)
            except Exception:
                pass

    def _live(self, now):
        return [h for h, s in self.st.items() if s["excluded_until"] <= now]

    def _recompute(self):
        now = self.clock()
        live = self._live(now) or list(self.st)
        for h, s in self.st.items():
            s["eff"] = s["cur"] if s["strikes"] >= self.DROP_WINDOWS else s["base"]
        best = max(self.st[h]["eff"] for h in live)
        # the primary only changes when it is gone or clearly beaten
        if self.primary not in live:
            self.primary = max(live, key=lambda h: self.st[h]["eff"])
        else:
            challenger = max(live, key=lambda h: self.st[h]["eff"])
            if self.st[challenger]["eff"] > self.st[self.primary]["eff"] * self.SWITCH_MARGIN:
                self._say(f"Main server: {short_host(challenger)} (was {short_host(self.primary)})")
                self.primary = challenger
        self.weights = {}
        for h in live:
            s = self.st[h]
            w = max(s["eff"], 1e-6)
            if s["probation"]:
                w *= 0.3
            if s["eff"] < 0.1 * best:
                w *= 0.3
            self.weights[h] = max(w, self.TRICKLE * best)
        total_w = sum(self.weights.values()) or 1.0
        for h in self.st:
            share = self.weights.get(h, 0.0) / total_w
            self.st[h]["cap"] = max(1, min(self.total_conns, int(share * self.total_conns + 0.999) + 1))

    def _exclude(self, h, reason):
        now = self.clock()
        if len(self._live(now)) <= 1:
            return   # never pause the last working server
        s = self.st[h]
        cool = min(self.COOLDOWN_MAX, self.COOLDOWN_BASE * (2 ** s["cool_n"]))
        s["cool_n"] += 1
        s["excluded_until"] = now + cool
        s["strikes"] = 0
        s["fails"] = 0
        self._say(f"Server {short_host(h)} paused for {int(cool)}s ({reason})")
        self._recompute()

    def _rerank(self):
        now = self.clock()
        self.last_rerank = now
        for h, s in self.st.items():
            if s["excluded_until"] and now >= s["excluded_until"]:     # cooldown over: retry on probation
                s["excluded_until"] = 0.0
                s["probation"] = True
                s["strikes"] = 0
                s["fails"] = 0
                self._say(f"Server {short_host(h)} is being retried")
        live = self._live(now)
        for h in live:
            s = self.st[h]
            if s["win_secs"] < self.MIN_WINDOW_SECS:
                continue
            win = (s["win_bytes"] / s["win_secs"]) / 1048576.0
            s["win_bytes"], s["win_secs"] = 0, 0.0
            s["cur"] = win
            if win < s["base"] * self.DROP_RATIO:
                s["strikes"] += 1
                s["base"] = 0.9 * s["base"] + 0.1 * win if s["strikes"] < self.DROP_WINDOWS else 0.5 * s["base"] + 0.5 * win
            else:
                s["strikes"] = 0
                s["base"] = 0.6 * s["base"] + 0.4 * win
            if s["probation"]:
                s["probation"] = False
        self._recompute()
        best = max((self.st[h]["eff"] for h in live), default=0.0)
        for h in live:
            s = self.st[h]
            if s["strikes"] >= self.DROP_WINDOWS and s["cur"] < self.EXCLUDE_RATIO * best and h != self.primary:
                self._exclude(h, f"too slow: {s['cur']:.1f} MB/s")
            elif s["strikes"] == self.DROP_WINDOWS:
                self._say(f"{short_host(h)} slowed to {s['cur']:.1f} MB/s - sending less work there")

    def _tick(self):
        if self.clock() - self.last_rerank >= self.RERANK_INTERVAL:
            self._rerank()

    # ----- public API -----
    def acquire(self, avoid=()):
        """Chooses a server for the next request (weighted by speed, within connection caps)."""
        with self.lock:
            self._tick()
            now = self.clock()
            live = self._live(now)
            if not live:   # everything paused: use whichever comes back first
                live = [min(self.st, key=lambda h: self.st[h]["excluded_until"])]
            cands = [h for h in live if h not in avoid] or live
            roomy = [h for h in cands if self.st[h]["active"] < self.st[h]["cap"]]
            pick_from = roomy or cands
            weights = [self.weights.get(h, self.TRICKLE) for h in pick_from]
            host = random.choices(pick_from, weights=weights, k=1)[0]
            self.st[host]["active"] += 1
            return host

    def release(self, host):
        with self.lock:
            self.st[host]["active"] = max(0, self.st[host]["active"] - 1)

    def record(self, host, nbytes, secs):
        """Called as data arrives, so slow in-flight requests are seen immediately."""
        with self.lock:
            s = self.st[host]
            s["win_bytes"] += nbytes
            s["win_secs"] += max(secs, 1e-4)
            s["total_bytes"] += nbytes

    def success(self, host):
        with self.lock:
            s = self.st[host]
            s["fails"] = 0
            s["chunks"] += 1

    def failure(self, host):
        with self.lock:
            s = self.st[host]
            s["fails"] += 1
            if s["fails"] >= self.FAIL_LIMIT:
                self._exclude(host, "repeated errors")

    def snapshot(self) -> dict:
        with self.lock:
            return {h: s["base"] for h, s in self.st.items() if s["total_bytes"] > 0}

    def summary(self) -> str:
        with self.lock:
            used = sorted((h for h, s in self.st.items() if s["total_bytes"] > 0),
                          key=lambda h: -self.st[h]["total_bytes"])
            parts = [f"{short_host(h)} {self.st[h]['total_bytes'] / 1048576:.0f} MB ({self.st[h]['base']:.1f} MB/s)" for h in used[:4]]
        return "Servers used: " + ", ".join(parts) if parts else ""

def stream_chunk(pool: "ServerPool", host: str, depot_id: str, sha: str) -> bytes:
    """Downloads one chunk from one server, reporting speed to the pool as data arrives."""
    url = f"{CHUNK_URL_SCHEME}://{host}/depot/{depot_id}/chunk/{sha}"
    headers = {"User-Agent": "Valve/Steam HTTP Client 1.0", "Accept": "*/*"}
    buf = bytearray()
    expected = None
    t_prev = time.time()
    try:
        if HTTP_POOL is not None:
            resp = HTTP_POOL.request("GET", url, headers=headers, preload_content=False, retries=False,
                                     timeout=urllib3.Timeout(connect=6.0, read=15.0))
            clean = False
            try:
                if resp.status in (403, 404, 410):
                    resp.drain_conn() if hasattr(resp, "drain_conn") else None
                    clean = True
                    raise ChunkMiss(f"HTTP {resp.status}")
                if resp.status != 200:
                    raise HostFailure(f"HTTP {resp.status}")
                cl = resp.headers.get("Content-Length")
                expected = int(cl) if cl and cl.isdigit() else None
                for block in resp.stream(65536):
                    now = time.time()
                    buf += block
                    pool.record(host, len(block), now - t_prev)
                    t_prev = time.time()
                clean = True
            finally:
                if clean:
                    resp.release_conn()
                else:
                    resp.close()
        else:
            req = urllib.request.Request(url, headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=15) as r:
                    cl = r.headers.get("Content-Length")
                    expected = int(cl) if cl and cl.isdigit() else None
                    while True:
                        block = r.read(65536)
                        if not block:
                            break
                        now = time.time()
                        buf += block
                        pool.record(host, len(block), now - t_prev)
                        t_prev = time.time()
            except urllib.error.HTTPError as he:
                if he.code in (403, 404, 410):
                    raise ChunkMiss(f"HTTP {he.code}")
                raise HostFailure(f"HTTP {he.code}")
    except ChunkMiss:
        raise
    except HostFailure:
        pool.failure(host)
        raise
    except Exception as e:
        pool.failure(host)
        raise HostFailure(f"{type(e).__name__}: {e}")
    if not buf or (expected is not None and len(buf) != expected):
        pool.failure(host)
        raise HostFailure(f"incomplete download ({len(buf)} of {expected} bytes)")
    pool.success(host)
    return bytes(buf)


def fetch_manifest_binary(depot_id: str, manifest_gid: str, request_code: int = 0) -> bytes:
    # 1. First check if a local .manifest file is available on disk
    local_candidates = [
        BASE_DIR / f"{depot_id}_{manifest_gid}.manifest",
        BASE_DIR / f"{depot_id}.manifest",
        DATA_DIR / f"{depot_id}_{manifest_gid}.manifest",
        DATA_DIR / f"{depot_id}.manifest",
        Path.cwd() / f"{depot_id}_{manifest_gid}.manifest",
        Path.cwd() / f"{depot_id}.manifest",
        Path.home() / "Downloads" / f"{depot_id}_{manifest_gid}.manifest",
        Path.home() / "Downloads" / "manifests" / f"{depot_id}_{manifest_gid}.manifest",
        Path.home() / "Downloads" / "SteamDownloads" / f"{depot_id}_{manifest_gid}.manifest",
    ]
    # Check common download subfolders
    for base in [Path.home() / "Downloads", Path.cwd().parent]:
        if base.exists():
            try:
                for sub in base.iterdir():
                    if sub.is_dir():
                        c1 = sub / f"{depot_id}_{manifest_gid}.manifest"
                        if c1.exists():
                            local_candidates.append(c1)
                        # Check one level deeper (e.g. Release/net9.0)
                        try:
                            for sub2 in sub.iterdir():
                                if sub2.is_dir():
                                    c2 = sub2 / f"{depot_id}_{manifest_gid}.manifest"
                                    if c2.exists():
                                        local_candidates.append(c2)
                        except Exception:
                            pass
            except Exception:
                pass

    for cand in local_candidates:
        if cand.exists() and cand.is_file() and cand.stat().st_size > 0:
            print(f"Using local manifest from {cand}")
            return cand.read_bytes()

    # 2. Try Steam CDNs with request code in path across fastest ranked servers
    urls = []
    ranked_hosts = get_ranked_cdn_hosts()
    if request_code:
        for h in ranked_hosts:
            urls.append(f"https://{h}/depot/{depot_id}/manifest/{manifest_gid}/5/{request_code}")
    for h in ranked_hosts:
        urls.append(f"https://{h}/depot/{depot_id}/manifest/{manifest_gid}/5")

    for url in urls:
        if HTTP_POOL is not None:
            try:
                resp = HTTP_POOL.request("GET", url, headers={"User-Agent": "Valve/Steam HTTP Client 1.0", "Accept": "*/*"}, timeout=25)
                if resp.status == 200 and resp.data:
                    return resp.data
            except Exception:
                pass
        try:
            data = fetch_url(url, timeout=25)
            if data and len(data) > 0:
                return data
        except Exception:
            continue

    raise RuntimeError(f"Could not fetch manifest {manifest_gid} for depot {depot_id}")

def decrypt_steam_filename(raw_bytes: bytes, key_bytes: bytes) -> str:
    if not key_bytes or len(key_bytes) != 32:
        return raw_bytes.decode("utf-8", errors="ignore").strip()
    try:
        clean = raw_bytes.strip()
        c = base64.b64decode(clean)
        if len(c) < 32 or len(c) % 16 != 0:
            return raw_bytes.decode("utf-8", errors="ignore").strip()

        if libcrypto is None:
            # No libcrypto (typical on Android/Pydroid): built-in AES, fine for short filenames
            dk = _aes256_decrypt_key_schedule(key_bytes)
            iv_plain = _aes256_cbc_decrypt_pure(dk, bytes(16), c[:16])
            dec = _aes256_cbc_decrypt_pure(dk, iv_plain, c[16:])
        else:
            # Decrypt IV with AES-256-ECB
            ctx = libcrypto.EVP_CIPHER_CTX_new()
            libcrypto.EVP_CipherInit_ex(ctx, libcrypto.EVP_aes_256_ecb(), None, key_bytes, None, 0)
            libcrypto.EVP_CIPHER_CTX_set_padding(ctx, 0)
            iv = ctypes.create_string_buffer(16)
            outl = ctypes.c_int(0)
            libcrypto.EVP_CipherUpdate(ctx, ctypes.byref(iv), ctypes.byref(outl), c[:16], 16)
            libcrypto.EVP_CIPHER_CTX_free(ctx)

            # Decrypt body with AES-256-CBC using decrypted IV
            ctx = libcrypto.EVP_CIPHER_CTX_new()
            libcrypto.EVP_CipherInit_ex(ctx, libcrypto.EVP_aes_256_cbc(), None, key_bytes, iv.raw, 0)
            libcrypto.EVP_CIPHER_CTX_set_padding(ctx, 0)
            body = c[16:]
            out = ctypes.create_string_buffer(len(body) + 32)
            finall = ctypes.c_int(0)
            libcrypto.EVP_CipherUpdate(ctx, ctypes.byref(out), ctypes.byref(outl), body, len(body))
            libcrypto.EVP_CipherFinal_ex(ctx, ctypes.c_void_p(ctypes.addressof(out) + outl.value), ctypes.byref(finall))
            libcrypto.EVP_CIPHER_CTX_free(ctx)
            dec = out.raw[:outl.value + finall.value]
        if b"\x00" in dec:
            return dec.split(b"\x00", 1)[0].decode("utf-8", errors="ignore")
        return dec.decode("utf-8", errors="ignore")
    except Exception:
        return raw_bytes.decode("utf-8", errors="ignore").strip()

def parse_manifest_files(raw_bytes: bytes, depot_key: str = "") -> list:
    key_bytes = bytes.fromhex(depot_key) if depot_key and len(depot_key) == 64 else None
    entry = raw_bytes
    if len(raw_bytes) >= 4 and raw_bytes[:4] == b"PK\x03\x04":
        try:
            zf = zipfile.ZipFile(io.BytesIO(raw_bytes))
            entry = zf.read(zf.namelist()[0])
        except Exception as e:
            print(f"Zip extraction error: {e}")
    elif len(raw_bytes) >= 2 and raw_bytes[:2] == b"\x1f\x8b":
        try:
            entry = zlib.decompress(raw_bytes, 16 + zlib.MAX_WBITS)
        except Exception:
            pass

    # Check magic header (PAYLOAD_MAGIC: 0x71F617D0)
    payload = entry
    if len(entry) >= 8:
        magic, length = struct.unpack_from("<II", entry, 0)
        if magic == 0x71F617D0 and len(entry) >= 8 + length:
            payload = entry[8:8+length]
        else:
            idx = entry.find(b"\xd0\x17\xf6\x71")
            if idx != -1 and len(entry) >= idx + 8:
                _, length = struct.unpack_from("<II", entry, idx)
                if len(entry) >= idx + 8 + length:
                    payload = entry[idx+8:idx+8+length]

    # Helper for protobuf varint
    def read_varint(buf, pos):
        res = shift = 0
        while pos < len(buf):
            b = buf[pos]; pos += 1
            res |= (b & 0x7f) << shift
            if not (b & 0x80): return res, pos
            shift += 7
        return res, pos

    # Parse protobuf FileMapping and ChunkData
    files = []
    pos = 0
    while pos < len(payload):
        tag, pos = read_varint(payload, pos)
        fn, wt = tag >> 3, tag & 7
        if wt == 0:
            _, pos = read_varint(payload, pos)
        elif wt == 1:
            pos += 8
        elif wt == 5:
            pos += 4
        elif wt == 2:
            length, pos = read_varint(payload, pos)
            field_bytes = payload[pos:pos+length]
            pos += length
            if fn == 1: # FileMapping
                fpos = 0
                file_info = {'filename': '', 'size': 0, 'flags': 0, 'chunks': []}
                while fpos < len(field_bytes):
                    ft, fpos = read_varint(field_bytes, fpos)
                    fnum, fwt = ft >> 3, ft & 7
                    if fwt == 0:
                        v, fpos = read_varint(field_bytes, fpos)
                        if fnum == 2: file_info['size'] = v
                        elif fnum == 3: file_info['flags'] = v
                    elif fwt == 1:
                        fpos += 8
                    elif fwt == 5:
                        fpos += 4
                    elif fwt == 2:
                        flen, fpos = read_varint(field_bytes, fpos)
                        fval = field_bytes[fpos:fpos+flen]
                        fpos += flen
                        if fnum == 1:
                            if key_bytes:
                                d = decrypt_steam_filename(fval, key_bytes)
                            else:
                                d = fval.decode('utf-8', errors='ignore')
                            file_info['filename'] = d.replace('\\', '/').strip('/')
                        elif fnum == 7:
                            if key_bytes:
                                d = decrypt_steam_filename(fval, key_bytes)
                            else:
                                d = fval.decode('utf-8', errors='ignore')
                            file_info['linktarget'] = d
                        elif fnum == 6: # ChunkData
                            cpos = 0
                            chunk = {}
                            while cpos < len(fval):
                                ct, cpos = read_varint(fval, cpos)
                                cnum, cwt = ct >> 3, ct & 7
                                if cwt == 0:
                                    cv, cpos = read_varint(fval, cpos)
                                    if cnum == 3: chunk['offset'] = cv
                                    elif cnum == 4: chunk['uncompressed_size'] = cv
                                    elif cnum == 5: chunk['size'] = cv
                                elif cwt == 1:
                                    cpos += 8
                                elif cwt == 5:
                                    if cnum == 2 and cpos + 4 <= len(fval):
                                        chunk['crc'] = struct.unpack_from('<I', fval, cpos)[0]
                                    cpos += 4
                                elif cwt == 2:
                                    clen, cpos = read_varint(fval, cpos)
                                    cv = fval[cpos:cpos+clen]
                                    cpos += clen
                                    if cnum == 1:
                                        chunk['sha'] = cv.hex()
                                else:
                                    break
                            if 'sha' in chunk:
                                file_info['chunks'].append(chunk)
                    else:
                        break

                flags = file_info.get('flags', 0)
                file_info['is_directory'] = bool(flags & 64)
                file_info['is_symlink'] = bool(flags & 32)
                if file_info['filename']:
                    files.append(file_info)
        else:
            break

    return files

def parse_chunks_from_manifest(raw_bytes: bytes) -> list:
    files = parse_manifest_files(raw_bytes)
    chunks = []
    for f in files:
        chunks.extend(f.get('chunks', []))
    return chunks

def download_chunk(depot_id: str, chunk_sha: str, timeout: int = 30, preferred_hosts: list = None) -> bytes:
    hosts = preferred_hosts or get_ranked_cdn_hosts()
    last_err = None
    for host in hosts:
        url = f"https://{host}/depot/{depot_id}/chunk/{chunk_sha}"
        if HTTP_POOL is not None:
            try:
                resp = HTTP_POOL.request(
                    "GET",
                    url,
                    headers={"User-Agent": "Valve/Steam HTTP Client 1.0", "Accept": "*/*"},
                    timeout=timeout
                )
                if resp.status == 200 and resp.data:
                    return resp.data
                elif resp.status in (404, 403):
                    continue
            except Exception as e:
                last_err = e
                continue
        try:
            return fetch_url(url, timeout=timeout)
        except Exception as e:
            last_err = e
            continue
    raise RuntimeError(f"Could not download chunk {chunk_sha} for depot {depot_id}: {last_err}")

# ---------- Depot Sources URL Generator ----------
def strip_archive_ext(name):
    for ext in (".zip", ".tar.gz", ".tgz", ".tar"):
        if name.endswith(ext):
            return name[:-len(ext)]
    return None

def zip_prefix_from_path(base):
    trimmed = base.rstrip('/')
    last_segment = trimmed.rsplit('/', 1)[-1]
    return strip_archive_ext(last_segment) or ""

def build_source_urls(source_url, app_id, filename):
    trimmed = source_url.strip()
    urls = []
    if "archive.org/view_archive.php" in trimmed:
        base = trimmed.rstrip('&? ')
        archive_val = ""
        if "?" in trimmed:
            query = trimmed.split("?", 1)[1]
            for part in query.split("&"):
                if part.startswith("archive="):
                    archive_val = part[len("archive="):]
        prefix = ""
        if archive_val:
            decoded = urllib.parse.unquote(archive_val)
            basename = decoded.rsplit('/', 1)[-1]
            prefix = strip_archive_ext(basename) or basename
        file_path = f"{prefix}/{app_id}/{filename}" if prefix else f"{app_id}/{filename}"
        encoded = urllib.parse.quote(file_path, safe="")
        sep = "&" if "?" in base else "?"
        urls.append(f"{base}{sep}file={encoded}")
    elif "github.com/" in trimmed:
        m = re.match(r'https?://github\.com/([^/]+)/([^/]+)(?:/tree/([^/]+)(?:/(.*))?)?', trimmed)
        if m:
            user, repo, branch, extra = m.group(1), m.group(2).rstrip('.git'), m.group(3) or "main", m.group(4) or ""
            base = f"https://raw.githubusercontent.com/{user}/{repo}/{branch}"
            if extra:
                base += f"/{extra}"
            urls.append(f"{base}/{app_id}/{filename}")
    else:
        base = trimmed.rstrip('/')
        prefix = zip_prefix_from_path(base)
        if prefix:
            urls.append(f"{base}/{prefix}/{app_id}/{filename}")
        urls.append(f"{base}/{app_id}/{filename}")
    return urls

def probe_app_in_sources(sources, app_id):
    filename = f"{app_id}.lua"
    for s in sources:
        for url in build_source_urls(s, app_id, filename):
            status = head_url(url)
            if status == 200:
                return "found"
    return "missing"

def download_file_from_sources(sources, app_id, filename):
    for s in sources:
        for url in build_source_urls(s, app_id, filename):
            try:
                return fetch_url(url, timeout=12).decode("utf-8", errors="ignore")
            except Exception:
                continue
    return None

def detect_executables(download_dir):
    p = Path(download_dir)
    exes = []
    if p.exists() and p.is_dir():
        for root, dirs, files in os.walk(p):
            for f in files:
                f_lower = f.lower()
                if f_lower.endswith(".exe") or f_lower.endswith(".sh") or os.access(os.path.join(root, f), os.X_OK):
                    full_p = os.path.join(root, f)
                    rel_p = os.path.relpath(full_p, p)
                    exes.append({"name": f, "path": full_p, "relative_path": rel_p})
    return {"executables": exes}

# ---------- Resume & Chunk Verification ----------
RESUME_DIR_NAME = ".smd_resume"   # hidden folder inside the game folder (progress journals)
RESUME_RECHECK_TAIL = 64          # "fast" resume: re-check this many most recent chunks

def steam_adler32(data: bytes) -> int:
    # Steam's chunk checksum is Adler-32 that starts from a=0 (not 1 like zlib's default)
    return zlib.adler32(data, 0) & 0xFFFFFFFF

def verify_chunk_data(data: bytes, chunk: dict, from_disk: bool = False):
    """Strictly verify a decrypted+decompressed chunk against manifest specs.
    Guarantees 0 damaged files: checks uncompressed size, SHA-1 digest, and CRC/Adler32.
    Returns (ok, how). `how` explains the check that passed or why it failed."""
    expected_len = chunk.get("uncompressed_size") or 0
    if expected_len and len(data) != expected_len:
        return False, f"size mismatch ({len(data)} != {expected_len})"
    if not data:
        return False, "empty chunk"

    sha = (chunk.get("sha") or "").lower()
    if len(sha) == 40:
        actual_sha = hashlib.sha1(data).hexdigest().lower()
        if actual_sha == sha:
            return True, "sha1"
        return False, f"sha1 mismatch (actual {actual_sha} != expected {sha})"

    crc = chunk.get("crc") or 0
    if crc:
        if steam_adler32(data) == crc or (zlib.adler32(data) & 0xFFFFFFFF) == crc:
            return True, "adler32"
        if (zlib.crc32(data) & 0xFFFFFFFF) == crc:
            return True, "crc32"
        return False, "checksum mismatch"

    # Manifest has no checksum for this chunk: only the size can be trusted.
    # A pre-allocated (sparse, never written) region reads back as all zeros -> not valid.
    if from_disk and data.count(b"\x00") == len(data):
        return False, "region is empty"
    return True, "size"

def write_at(path, data: bytes, offset: int):
    fd = os.open(str(path), os.O_RDWR | getattr(os, "O_BINARY", 0))
    try:
        view = memoryview(data)
        pos = 0
        while pos < len(view):
            if hasattr(os, "pwrite"):
                n = os.pwrite(fd, view[pos:], offset + pos)
            else:  # Windows
                os.lseek(fd, offset + pos, os.SEEK_SET)
                n = os.write(fd, view[pos:])
            if n <= 0:
                raise OSError("short write")
            pos += n
    finally:
        os.close(fd)

def read_at(path, offset: int, length: int) -> bytes:
    fd = os.open(str(path), os.O_RDONLY | getattr(os, "O_BINARY", 0))
    try:
        parts = []
        pos, remaining = offset, length
        while remaining > 0:
            if hasattr(os, "pread"):
                b = os.pread(fd, remaining, pos)
            else:  # Windows
                os.lseek(fd, pos, os.SEEK_SET)
                b = os.read(fd, remaining)
            if not b:
                break
            parts.append(b)
            pos += len(b)
            remaining -= len(b)
        return b"".join(parts)
    finally:
        os.close(fd)

_open_journals = set()
_open_journals_lock = threading.Lock()

class ResumeJournal:
    """Append-only list of finished chunk SHAs for one depot+manifest.
    Survives crashes/restarts; each finished chunk costs one tiny append."""
    def __init__(self, path):
        self.path = Path(path)
        self.lock = threading.Lock()
        self.order = []
        self.known = set()
        self.fh = None

    def load(self) -> bool:
        self.order, self.known = [], set()
        try:
            with open(self.path, "r", encoding="ascii", errors="ignore") as f:
                for line in f:
                    sha = line.strip()
                    # ignore a half-written last line after a crash
                    if len(sha) == 40 and sha not in self.known:
                        self.known.add(sha)
                        self.order.append(sha)
            return bool(self.order)
        except Exception:
            return False

    def rewrite(self, shas):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        with open(tmp, "w", encoding="ascii") as f:
            for sha in shas:
                f.write(sha + "\n")
            f.flush()
            try: os.fsync(f.fileno())
            except Exception: pass
        os.replace(tmp, self.path)
        self.order = list(shas)
        self.known = set(shas)

    def open(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.fh = open(self.path, "a", encoding="ascii")
        with _open_journals_lock:
            _open_journals.add(self)

    def mark(self, sha):
        with self.lock:
            if sha in self.known or self.fh is None:
                return
            self.known.add(sha)
            self.order.append(sha)
            self.fh.write(sha + "\n")
            self.fh.flush()

    def sync(self):
        with self.lock:
            if self.fh is not None:
                try:
                    self.fh.flush()
                    os.fsync(self.fh.fileno())
                except Exception:
                    pass

    def close(self):
        self.sync()
        with self.lock:
            if self.fh is not None:
                try: self.fh.close()
                except Exception: pass
                self.fh = None
        with _open_journals_lock:
            _open_journals.discard(self)

    def clear(self):
        self.close()
        try: self.path.unlink()
        except Exception: pass
        try: self.path.parent.rmdir()   # only succeeds when empty
        except Exception: pass

def _flush_all_journals():
    with _open_journals_lock:
        items = list(_open_journals)
    for j in items:
        try: j.sync()
        except Exception: pass

atexit.register(_flush_all_journals)

def record_engine_history(entry):
    entry["engine_recorded"] = True
    hist = load_history()
    hist["entries"] = [
        e for e in hist.get("entries", [])
        if not (e.get("engine_recorded")
                and e.get("download_dir") == entry.get("download_dir")
                and str(e.get("app_id")) == str(entry.get("app_id")))
    ]
    hist["entries"].insert(0, entry)
    save_history(hist)

def now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

# ---------- Real Download Engine ----------
active_jobs = {}

def execute_download(job_id, config):
    try:
        app_id = str(config.get("mainAppId") or config.get("app_id", ""))
        game_name = config.get("gameName", f"App {app_id}")
        depots = config.get("selectedDepots", [])
        dest_dir = Path(config.get("downloadDir") or (Path.home() / "Downloads" / "SteamDownloads"))
        safe_name = re.sub(r'[<>:"/\\|?*]', '_', game_name)
        target_dir = dest_dir / f"{app_id} - {safe_name}"
        target_dir.mkdir(parents=True, exist_ok=True)

        total_depots = len(depots)
        started_at = now_iso()

        def is_cancelled():
            return bool(active_jobs.get(job_id, {}).get("cancelled"))

        def finish_cancelled(done_results):
            # Files are always kept on disk; the progress journal lets the next run continue here.
            active_jobs.setdefault(job_id, {})["status"] = "cancelled"
            emit_event("download-progress", {
                "jobId": job_id,
                "type": "cancelled",
                "step": "cancelled_kept"
            })
            try:
                record_engine_history({
                    "id": job_id,
                    "app_id": app_id,
                    "game_name": game_name,
                    "header_image": config.get("headerImage"),
                    "depot_count": total_depots,
                    "depots_downloaded": len([r for r in done_results if r["success"]]),
                    "status": "cancelled_resumable",
                    "download_dir": str(target_dir),
                    "started_at": started_at,
                    "completed_at": now_iso(),
                    "depot_ids": [d.get("depotId") for d in depots],
                    "resume_payload": config
                })
            except Exception as he:
                print(f"History error: {he}")

        emit_event("download-progress", {
            "jobId": job_id,
            "type": "start",
            "gameName": game_name,
            "downloadDir": str(target_dir),
            "appId": app_id,
            "totalDepots": total_depots
        })

        emit_event("download-progress", {
            "jobId": job_id,
            "type": "status",
            "step": "starting_downloader",
            "total": total_depots,
            "current": 1,
            "depotId": depots[0].get("depotId") if depots else 0
        })

        results = []
        for i, depot in enumerate(depots):
            if is_cancelled():
                finish_cancelled(results)
                return

            depot_id = str(depot.get("depotId") or depot.get("depot_id", ""))
            manifest_id = str(depot.get("manifestId") or depot.get("manifest_id") or depot.get("manifest_gid") or "")
            depot_key = str(depot.get("depotKey") or depot.get("depot_key") or "")

            emit_event("download-progress", {
                "jobId": job_id,
                "type": "status",
                "step": "running_downloader",
                "current": i + 1,
                "total": total_depots,
                "depotId": int(depot_id) if depot_id.isdigit() else 0
            })

            raw_manifest_id = depot.get("manifestId") or depot.get("manifest_id") or depot.get("manifest_gid")
            manifest_id = str(raw_manifest_id).strip() if raw_manifest_id and str(raw_manifest_id).strip() not in ("None", "N/A", "") else None
            depot_key = str(depot.get("depotKey") or depot.get("depot_key") or "")

            emit_event("download-progress", {
                "jobId": job_id,
                "type": "status",
                "step": "running_downloader",
                "current": i + 1,
                "total": total_depots,
                "depotId": int(depot_id) if depot_id.isdigit() else 0
            })

            emit_event("download-progress", {
                "jobId": job_id,
                "type": "output",
                "stream": "stdout",
                "line": f"Processing depot {depot_id} (Manifest: {manifest_id or 'none'})..."
            })

            has_key = bool(depot_key and len(depot_key) == 64)
            depot_dir = target_dir
            depot_dir.mkdir(parents=True, exist_ok=True)

            # Clean up any leftover dummy depot_data.bin
            for dummy in [target_dir / "depot_data.bin", target_dir / depot_id / "depot_data.bin"]:
                if dummy.exists():
                    try: dummy.unlink()
                    except Exception: pass

            depot_success = True
            if not manifest_id:
                emit_event("download-progress", {
                    "jobId": job_id,
                    "type": "output",
                    "stream": "stdout",
                    "line": f"Depot {depot_id} is a base app or container without manifest. Marked complete."
                })
            else:
                try:
                    req_code = depot.get("manifest_request_code") or depot.get("manifestRequestCode") or 0
                    code = int(req_code) if str(req_code).isdigit() else 0
                    if not code:
                        code = get_manifest_request_code(manifest_id)
                    emit_event("download-progress", {
                        "jobId": job_id,
                        "type": "output",
                        "stream": "stdout",
                        "line": f"Manifest request code: {code}"
                    })

                    manifest_raw = fetch_manifest_binary(depot_id, manifest_id, code)
                    files = parse_manifest_files(manifest_raw, depot_key)

                    # Keep a copy of the manifest so a later resume works even if the
                    # request-code service or CDN manifest endpoint is unavailable.
                    try:
                        cache_p = DATA_DIR / f"{depot_id}_{manifest_id}.manifest"
                        if files and not cache_p.exists():
                            cache_p.write_bytes(manifest_raw)
                    except Exception:
                        pass

                    # 1. Setup file system: create directories, symlinks, and allocate files.
                    #    Existing files are NEVER wiped: they are only grown/shrunk to the expected
                    #    size, so data from an earlier (interrupted) run survives and can be resumed.
                    dir_count = 0
                    file_count = 0
                    had_existing = False
                    fresh_files = set()
                    for f in files:
                        rel = f["filename"]
                        norm = os.path.normpath(rel)
                        if norm.startswith("..") or os.path.isabs(norm):
                            continue
                        full_path = target_dir / norm
                        if f["is_directory"]:
                            full_path.mkdir(parents=True, exist_ok=True)
                            dir_count += 1
                        elif f["is_symlink"]:
                            full_path.parent.mkdir(parents=True, exist_ok=True)
                            if full_path.exists() or full_path.is_symlink():
                                full_path.unlink()
                            try:
                                os.symlink(f.get("linktarget", ""), full_path)
                            except Exception:
                                pass
                        else:
                            full_path.parent.mkdir(parents=True, exist_ok=True)
                            file_size = f.get("size", 0)
                            try:
                                existed = full_path.is_file()
                                cur_size = full_path.stat().st_size if existed else -1
                            except OSError:
                                existed, cur_size = False, -1
                            if existed and cur_size > 0:
                                had_existing = True
                            if not existed or cur_size != file_size:
                                fresh_files.add(full_path)
                                with open(full_path, "r+b" if existed else "wb") as fp:
                                    fp.truncate(file_size)
                            if f.get("flags", 0) & 0x80 or full_path.suffix.lower() in (".exe", ".sh"):
                                try:
                                    os.chmod(full_path, 0o755)
                                except Exception:
                                    pass
                            file_count += 1

                    # 2. Collect unique chunks to download
                    unique_chunks = {}
                    for f in files:
                        if f["is_directory"] or f["is_symlink"]:
                            continue
                        rel = f["filename"]
                        norm = os.path.normpath(rel)
                        if norm.startswith("..") or os.path.isabs(norm):
                            continue
                        full_path = target_dir / norm

                        for ch in f["chunks"]:
                            sha = ch.get("sha")
                            if not sha:
                                continue
                            if sha not in unique_chunks:
                                unique_chunks[sha] = {
                                    "sha": sha,
                                    "size": ch.get("size", 0),
                                    "uncompressed_size": ch.get("uncompressed_size", 0),
                                    "crc": ch.get("crc", 0),
                                    "targets": []
                                }
                            unique_chunks[sha]["targets"].append((full_path, ch.get("offset", 0)))

                    chunk_list = list(unique_chunks.values())
                    total_chunks = len(chunk_list)
                    total_bytes = sum(c["uncompressed_size"] for c in chunk_list)

                    emit_event("download-progress", {
                        "jobId": job_id,
                        "type": "output",
                        "stream": "stdout",
                        "line": f"Depot {depot_id}: Initialized {file_count} files, {dir_count} directories, {total_chunks} chunks ({total_bytes:,} bytes)."
                    })

                    settings = load_settings()
                    concurrency = int(settings.get("native_chunk_concurrency") or 16)
                    conn_cap = device_connection_cap()
                    if concurrency > conn_cap:
                        emit_event("download-progress", {"jobId": job_id, "type": "output", "stream": "stdout",
                                                         "line": f"Connections limited to {conn_cap} for this device."})
                    concurrency = max(2, min(concurrency, conn_cap))
                    try:
                        max_attempts = max(1, int(settings.get("max_retries") or 3))
                    except Exception:
                        max_attempts = 3
                    verify_enabled = bool(settings.get("verify_chunks", True)) and has_key
                    resume_mode = str(config.get("resumeMode") or "verify").lower()

                    # 3. Resume: work out which chunks are already safely on disk
                    by_sha = {c["sha"]: c for c in chunk_list}
                    journal = ResumeJournal(target_dir / RESUME_DIR_NAME / f"{depot_id}_{manifest_id}.done")
                    journal_found = journal.load()
                    trusted, to_check = [], []
                    if journal_found:
                        order = [sh for sh in journal.order if sh in by_sha]
                        if not verify_enabled:
                            trusted = order
                        elif resume_mode == "fast":
                            tail = set(order[-RESUME_RECHECK_TAIL:])
                            for sh in order:
                                risky = sh in tail or any(pth in fresh_files for pth, _ in by_sha[sh]["targets"])
                                (to_check if risky else trusted).append(sh)
                        else:
                            to_check = order
                    elif had_existing and verify_enabled:
                        # No journal (older download / journal lost) but files exist: scan them
                        to_check = [c["sha"] for c in chunk_list]

                    good = set(trusted)
                    if to_check:
                        emit_event("download-progress", {
                            "jobId": job_id,
                            "type": "output",
                            "stream": "stdout",
                            "line": f"Resuming: verifying {len(to_check):,} chunks already on disk..."
                        })

                        def check_on_disk(sha):
                            if is_cancelled():
                                return sha, False
                            item = by_sha[sha]
                            length = item["uncompressed_size"]
                            if not length:
                                return sha, False
                            try:
                                for pth, off in item["targets"]:
                                    ok, _ = verify_chunk_data(read_at(pth, off, length), item, from_disk=True)
                                    if not ok:
                                        return sha, False
                                return sha, True
                            except Exception:
                                return sha, False

                        verified = set()
                        checked = 0
                        last_v_emit = time.time()
                        with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as vex:
                            for sha, ok in vex.map(check_on_disk, to_check):
                                checked += 1
                                if ok:
                                    verified.add(sha)
                                now = time.time()
                                if now - last_v_emit >= 1.0 and checked < len(to_check):
                                    last_v_emit = now
                                    emit_event("download-progress", {
                                        "jobId": job_id,
                                        "type": "output",
                                        "stream": "stdout",
                                        "line": f"Verifying existing data: {checked:,}/{len(to_check):,} chunks ({len(verified):,} good)"
                                    })
                        if is_cancelled():
                            finish_cancelled(results)
                            return
                        bad = len(to_check) - len(verified)
                        good |= verified
                        emit_event("download-progress", {
                            "jobId": job_id,
                            "type": "output",
                            "stream": "stdout",
                            "line": f"Verification finished: {len(verified):,} chunks OK" + (f", {bad:,} damaged/missing will be downloaded again." if bad else ".")
                        })

                    if journal_found or good:
                        base_order = [sh for sh in (journal.order if journal_found else []) if sh in good]
                        in_order = set(base_order)
                        base_order += [c["sha"] for c in chunk_list if c["sha"] in good and c["sha"] not in in_order]
                        journal.rewrite(base_order)
                    journal.open()

                    skipped_bytes = sum(by_sha[sh]["uncompressed_size"] for sh in good)
                    todo = [c for c in chunk_list if c["sha"] not in good]
                    for idx, c in enumerate(todo):
                        c["_idx"] = idx

                    ranked_hosts, host_speeds, from_cache = [], {}, False
                    if todo:
                        if not (RANKED_CDN_CACHE.get("speeds") or load_speed_cache()):
                            emit_event("download-progress", {"jobId": job_id, "type": "output", "stream": "stdout",
                                                             "line": "Testing server speeds..."})
                        ranked_hosts, host_speeds, from_cache = benchmark_cdn_hosts(depot_id, todo)
                    else:
                        ranked_hosts = get_ranked_cdn_hosts()
                    if host_speeds:
                        shown = ", ".join(f"{short_host(h)} {host_speeds[h]:.1f} MB/s" for h in ranked_hosts[:3] if h in host_speeds)
                        fast_summary = shown + (" (cached test)" if from_cache else "")
                    else:
                        fast_summary = ", ".join(short_host(h) for h in ranked_hosts[:2])

                    if good:
                        emit_event("download-progress", {
                            "jobId": job_id,
                            "type": "output",
                            "stream": "stdout",
                            "line": f"{round(skipped_bytes / total_bytes * 100, 1) if total_bytes else 100}% ({len(good)}/{total_chunks} chunks) already downloaded - continuing with {len(todo)} remaining.",
                            "completedBytes": skipped_bytes,
                            "totalBytes": total_bytes,
                            "percent": round((skipped_bytes / total_bytes * 100) if total_bytes > 0 else 100, 1),
                            "skippedChunks": len(good),
                            "skippedBytes": skipped_bytes
                        })

                    emit_event("download-progress", {
                        "jobId": job_id,
                        "type": "output",
                        "stream": "stdout",
                        "line": f"Using fastest server: {fast_summary}"
                    })

                    emit_event("download-progress", {
                        "jobId": job_id,
                        "type": "output",
                        "stream": "stdout",
                        "line": f"Downloading {len(todo)} chunks, {concurrency} streams" + (", verification on" if verify_enabled else ", verification off")
                    })

                    progress_lock = threading.Lock()
                    download_state = {
                        "completed_bytes": skipped_bytes,
                        "session_bytes": 0,
                        "network_bytes": 0,
                        "done_chunks": len(good),
                        "failed": [],
                        "retries": 0,
                        "unverifiable": 0,
                        "fatal": None,
                        "last_emit_time": time.time(),
                        "last_sync_time": time.time(),
                        "start_time": time.time()
                    }

                    err_kinds = {}

                    def log_line(text, stream="stdout"):
                        emit_event("download-progress", {
                            "jobId": job_id, "type": "output", "stream": stream, "line": text
                        })

                    server_pool = ServerPool(ranked_hosts, host_speeds, total_conns=concurrency, notify=log_line)
                    done_shas = set()   # chunks verified and written in this run: never fetched twice
                    emit_event("download-progress", {"jobId": job_id, "type": "output", "stream": "stdout",
                                                     "line": crypto_backend_note()})

                    def chunk_worker(item):
                        if is_cancelled() or download_state["fatal"]:
                            return False
                        chunk_idx = item.get("_idx", 0)
                        chunk_sha = item["sha"]
                        chunk_uncompressed = item["uncompressed_size"]
                        targets = item["targets"]
                        last_err = "unknown error"
                        raw_len = 0
                        data = None

                        with progress_lock:
                            if chunk_sha in done_shas:
                                return True
                        verified = None      # verified data kept in memory if only the disk write failed
                        tried = set()        # servers that already failed for this chunk
                        attempts = max(max_attempts, 3)

                        for attempt in range(1, attempts + 1):
                            if is_cancelled() or download_state["fatal"]:
                                return False
                            host = None
                            try:
                                if verified is None:
                                    host = server_pool.acquire(avoid=tried)
                                    try:
                                        raw_chunk = stream_chunk(server_pool, host, depot_id, chunk_sha)
                                    finally:
                                        server_pool.release(host)
                                    raw_len = len(raw_chunk)
                                    # Decryption failure raises: undecrypted bytes are never accepted
                                    data = process_chunk(raw_chunk, depot_key) if has_key else raw_chunk

                                    # Size (always) + SHA-1/checksum (when enabled) BEFORE anything is saved
                                    if has_key:
                                        ok, how = verify_chunk_data(data, item) if verify_enabled else (len(data) == chunk_uncompressed or not chunk_uncompressed, "size mismatch")
                                        if not ok:
                                            raise ValueError(f"Chunk integrity verification failed: {how}")
                                    verified = data

                                for full_path, offset in targets:
                                    write_at(full_path, verified, offset)
                                last_err = None
                                break
                            except ChunkMiss as cm:
                                last_err = f"server has no such chunk ({cm})"
                                if host:
                                    tried.add(host)
                            except HostFailure as hf:
                                last_err = str(hf)
                                if host:
                                    tried.add(host)
                            except OSError as oe:
                                en = getattr(oe, "errno", None)
                                if en == errno.ENOSPC:
                                    download_state["fatal"] = "Disk is full. Free some space and start the download again to continue."
                                    return False
                                if en in (errno.EACCES, errno.EPERM, errno.EROFS) or isinstance(oe, PermissionError):
                                    download_state["fatal"] = ("Cannot write to the download folder (permission denied). "
                                        + ("On Android pick a folder like /storage/emulated/0/Download and allow storage access for the app."
                                           if IS_ANDROID else "Choose a folder you have write access to."))
                                    return False
                                last_err = str(oe)
                            except RuntimeError as re_:
                                # missing zstd / no AES backend: retrying will not help
                                download_state["fatal"] = str(re_)
                                return False
                            except MemoryError:
                                last_err = "out of memory (lower the parallel streams in Settings)"
                                time.sleep(1.0)
                            except Exception as ce:
                                last_err = str(ce)
                                if host:
                                    tried.add(host)
                            with progress_lock:
                                download_state["retries"] += 1
                            time.sleep(min(1.5, 0.25 * attempt))

                        if last_err is not None:
                            kind = re.sub(r"\(.*?\)|[0-9a-f]{40}|\d+", "#", last_err.replace("Chunk integrity verification failed: ", ""))[:70]
                            with progress_lock:
                                n = err_kinds.get(kind, 0) + 1
                                err_kinds[kind] = n
                                download_state["failed"].append(chunk_sha)
                            if n <= 2:   # only the first couple of each error type are printed
                                print(f"Chunk {chunk_sha[:8]} failed: {last_err.replace('Chunk integrity verification failed: ', '')}")
                            return False

                        journal.mark(chunk_sha)

                        with progress_lock:
                            done_shas.add(chunk_sha)
                            download_state["completed_bytes"] += chunk_uncompressed
                            download_state["session_bytes"] += chunk_uncompressed
                            download_state["network_bytes"] += raw_len
                            download_state["done_chunks"] += 1
                            now = time.time()
                            done = download_state["done_chunks"]
                            comp = download_state["completed_bytes"]
                            if now - download_state["last_sync_time"] >= 2.0:
                                download_state["last_sync_time"] = now
                                journal.sync()
                            if now - download_state["last_emit_time"] >= 0.4 or done == total_chunks:
                                download_state["last_emit_time"] = now
                                elapsed = max(0.1, now - download_state["start_time"])
                                speed_mb = (download_state["session_bytes"] / 1024 / 1024) / elapsed
                                pct = round((comp / total_bytes * 100) if total_bytes > 0 else 100, 1)
                                payload = {
                                    "jobId": job_id,
                                    "type": "output",
                                    "stream": "stdout",
                                    "line": f"{pct}% ({done}/{total_chunks} chunks) @ {speed_mb:.2f} MB/s",
                                    "completedBytes": comp,
                                    "totalBytes": total_bytes,
                                    "percent": pct,
                                    "networkBytes": download_state["network_bytes"]
                                }
                                if good:
                                    payload["skippedChunks"] = len(good)
                                    payload["skippedBytes"] = skipped_bytes
                                emit_event("download-progress", payload)
                        return True

                    try:
                        with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as executor:
                            futures = [executor.submit(chunk_worker, c) for c in todo]
                            for future in concurrent.futures.as_completed(futures):
                                if is_cancelled() or download_state["fatal"]:
                                    executor.shutdown(wait=False, cancel_futures=True)
                                    break
                    finally:
                        journal.close()   # flushes progress to disk so the next run can resume

                    if is_cancelled():
                        finish_cancelled(results)
                        return
                    if download_state["fatal"]:
                        raise RuntimeError(download_state["fatal"])

                    try:
                        live_speeds = server_pool.snapshot()
                        if live_speeds:
                            save_speed_cache(live_speeds)
                            with RANKED_CDN_LOCK:
                                RANKED_CDN_CACHE["speeds"].update(live_speeds)
                        used = server_pool.summary()
                        if used:
                            log_line(used)
                    except Exception:
                        pass

                    failed_chunks = download_state["failed"]
                    if failed_chunks:
                        depot_success = False
                        log_line(
                            f"Depot {depot_id}: {len(failed_chunks)} chunk(s) failed. "
                            + "; ".join(f"{v}x {k}" for k, v in sorted(err_kinds.items(), key=lambda kv: -kv[1])[:3])
                            + ". Progress saved - run again to resume.", "stderr")
                    else:
                        if verify_enabled:
                            log_line(f"Depot {depot_id}: all {total_chunks} chunks verified"
                                     + (f" ({download_state['retries']} retried)." if download_state["retries"] else "."))
                        journal.clear()
                        try:   # drop journals of older manifest versions of this depot
                            for old in (target_dir / RESUME_DIR_NAME).glob(f"{depot_id}_*.done"):
                                old.unlink()
                            (target_dir / RESUME_DIR_NAME).rmdir()
                        except Exception:
                            pass

                except Exception as e:
                    print(f"Depot {depot_id} error: {e}")
                    emit_event("download-progress", {
                        "jobId": job_id,
                        "type": "output",
                        "stream": "stderr",
                        "line": f"Depot {depot_id} error: {e}"
                    })
                    depot_success = False

            results.append({"depotId": depot_id, "success": depot_success})

            emit_event("download-progress", {
                "jobId": job_id,
                "type": "depot_complete",
                "depotId": int(depot_id) if depot_id.isdigit() else depot_id,
                "current": i + 1,
                "total": total_depots
            })

        # Record in history
        ok_count = len([r for r in results if r["success"]])
        all_ok = ok_count == total_depots
        record_engine_history({
            "id": job_id,
            "app_id": app_id,
            "game_name": game_name,
            "header_image": config.get("headerImage"),
            "depot_count": total_depots,
            "depots_downloaded": ok_count,
            "status": "complete" if all_ok else "partial",
            "download_dir": str(target_dir),
            "started_at": started_at,
            "completed_at": now_iso(),
            "depot_ids": [d.get("depotId") for d in depots]
        })

        active_jobs[job_id]["completed"] = True
        active_jobs[job_id]["status"] = "completed" if all_ok else "partial"

        emit_event("download-progress", {
            "jobId": job_id,
            "type": "complete",
            "diag": {"outcome": "complete" if all_ok else "partial"},
            "results": results,
            "message": (f"Successfully completed download for {game_name}" if all_ok else
                        f"Download of {game_name} finished with errors ({total_depots - ok_count} of {total_depots} depots incomplete). "
                        f"Your progress is saved - start the same download again to continue where it stopped.")
        })

    except Exception as e:
        active_jobs[job_id]["status"] = "error"
        active_jobs[job_id]["error"] = str(e)
        emit_event("download-progress", {
            "jobId": job_id,
            "type": "error",
            "message": str(e)
        })

# ---------- Command Dispatcher ----------
def handle_invoke(cmd, args):
    if cmd == "get_settings":
        return load_settings()

    elif cmd == "save_settings":
        settings_data = args.get("settings", args)
        save_settings(settings_data)
        return True

    elif cmd == "get_history":
        return load_history().get("entries", [])

    elif cmd == "clear_history":
        save_history({"entries": []})
        return True

    elif cmd == "remove_history_entry":
        entry_id = args.get("id") or args.get("entryId")
        hist = load_history()
        hist["entries"] = [e for e in hist.get("entries", []) if e.get("id") != entry_id]
        save_history(hist)
        return True

    elif cmd == "record_history_entry":
        entry = args.get("entry", {})
        if entry:
            hist = load_history()
            # The download engine already records its own entry for this download;
            # the UI's richer entry replaces it so history doesn't show duplicates.
            hist["entries"] = [
                e for e in hist.get("entries", [])
                if not (e.get("engine_recorded")
                        and e.get("download_dir") == entry.get("download_dir")
                        and str(e.get("app_id")) == str(entry.get("app_id")))
            ]
            hist["entries"].insert(0, entry)
            save_history(hist)
        return True

    elif cmd == "get_pending_followup":
        return load_pending_followup()

    elif cmd == "save_pending_followup":
        return save_pending_followup(args.get("followup", args))

    elif cmd == "clear_pending_followup":
        return clear_pending_followup()

    elif cmd == "search_steam_games":
        query = args.get("query", "").strip()
        if not query:
            return []
        url = "https://store.steampowered.com/api/storesearch/?" + urllib.parse.urlencode({
            "term": query, "l": "english", "cc": "US"
        })
        try:
            data = fetch_json(url)
            items = data.get("items", [])
            return [
                {
                    "appId": item.get("id"),
                    "name": item.get("name"),
                    "image": item.get("tiny_image", "")
                }
                for item in items[:15]
            ]
        except Exception:
            return []

    elif cmd == "get_steam_app_info":
        app_id = str(args.get("appId") or args.get("app_id", ""))
        url = f"https://store.steampowered.com/api/appdetails?appids={app_id}"
        try:
            data = fetch_json(url)
            entry = data.get(app_id, {})
            if entry.get("success"):
                d = entry.get("data", {})
                return {
                    "name": d.get("name"),
                    "headerImage": d.get("header_image"),
                    "shortDescription": d.get("short_description"),
                    "type": d.get("type")
                }
        except Exception:
            pass
        return None

    elif cmd in ("fetch_depot_metadata", "fetch_depot_metadata_steam"):
        app_id = str(args.get("appId") or args.get("app_id", ""))
        url = f"https://api.steamcmd.net/v1/info/{app_id}"
        try:
            data = fetch_json(url)
            depots_obj = data.get("data", {}).get(app_id, {}).get("depots", {})
            results = []
            for k, v in depots_obj.items():
                if not k.isdigit():
                    continue
                cfg = v.get("config", {})
                man = v.get("manifests", {}).get("public", {})
                results.append({
                    "depot_id": k,
                    "oslist": cfg.get("oslist"),
                    "osarch": cfg.get("osarch"),
                    "language": cfg.get("language"),
                    "size_bytes": int(man["size"]) if man.get("size") and str(man["size"]).isdigit() else None,
                    "download_bytes": int(man["download"]) if man.get("download") and str(man["download"]).isdigit() else None,
                    "manifest_gid": man.get("gid")
                })
            return results
        except Exception:
            return []

    elif cmd == "recommend_depots":
        depots = args.get("depots", [])
        platform = args.get("platform", "windows").lower()
        lang = args.get("language", "english").lower()
        selected = []
        for d in depots:
            depot_id = str(d.get("depotId") or d.get("depot_id", ""))
            oslist = str(d.get("oslist", "")).lower()
            d_lang = str(d.get("language", "")).lower()
            if oslist and platform not in oslist:
                continue
            if d_lang and d_lang != lang and d_lang != "all":
                continue
            selected.append(depot_id)
        return selected

    elif cmd == "search_repos":
        app_id = str(args.get("appId") or args.get("app_id", ""))
        repos = [{
            "repo": "configured",
            "date": time.strftime("%Y-%m-%d"),
            "sha": None,
            "type": "remote",
            "source": "remote"
        }]
        return {"repos": repos, "sourceProbe": "found"}

    elif cmd == "get_repo_manifests":
        app_id = str(args.get("appId") or args.get("app_id", ""))
        settings = load_settings()
        sources = settings.get("depot_sources", DEFAULT_SETTINGS["depot_sources"])

        # Try sources with quick timeout
        vdf_keys = {}
        manifests = []
        try:
            lua_text = download_file_from_sources(sources, app_id, f"{app_id}.lua")
            parsed = parse_lua_content(lua_text) if lua_text else {"depots": []}
            vdf_text = download_file_from_sources(sources, app_id, "key.vdf")
            vdf_keys = parse_vdf_keys(vdf_text) if vdf_text else {}
            json_text = download_file_from_sources(sources, app_id, f"{app_id}.json")
            sizes = {}
            if json_text:
                try:
                    sizes = extract_depot_sizes(json.loads(json_text))
                except Exception:
                    pass

            for d in parsed.get("depots", []):
                d_id = str(d.get("depot_id"))
                m_id = str(d.get("manifest_id") or "N/A")
                key = d.get("depot_key") or vdf_keys.get(d_id)
                size = d.get("size_bytes") or sizes.get((d_id, m_id))
                manifests.append({
                    "depot_id": d_id,
                    "manifest_id": m_id,
                    "filename": f"{d_id}_{m_id}.manifest",
                    "depot_key": key,
                    "size_bytes": size
                })
        except Exception:
            pass

        # If manifests empty or not found in repos, populate from official SteamCMD
        if not manifests:
            try:
                steamcmd_url = f"https://api.steamcmd.net/v1/info/{app_id}"
                data = fetch_json(steamcmd_url, timeout=6)
                depots_obj = data.get("data", {}).get(str(app_id), {}).get("depots", {})
                for k, v in depots_obj.items():
                    if not k.isdigit():
                        continue
                    man = v.get("manifests", {}).get("public", {})
                    gid = man.get("gid")
                    size = int(man["size"]) if man.get("size") and str(man["size"]).isdigit() else None
                    manifests.append({
                        "depot_id": k,
                        "manifest_id": str(gid) if gid else "N/A",
                        "filename": f"{k}_{gid}.manifest" if gid else f"{k}.manifest",
                        "depot_key": vdf_keys.get(k),
                        "size_bytes": size
                    })
            except Exception as e:
                print(f"SteamCMD fallback error: {e}")

        return {
            "manifests": manifests,
            "has_key_vdf": bool(vdf_keys),
            "key_vdf_filename": "key.vdf" if vdf_keys else None,
            "lua_filename": f"{app_id}.lua",
            "files": [f"{app_id}.lua"],
            "depot_keys": vdf_keys
        }

    elif cmd == "parse_lua_file":
        path = args.get("path", "")
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(f"File not found: {path}")
        raw = p.read_bytes()
        if p.suffix.lower() == ".st":
            return parse_st_file(raw)
        else:
            return parse_lua_content(raw.decode("utf-8", errors="ignore"))

    elif cmd == "parse_lua_content":
        return parse_lua_content(args.get("content", ""))

    elif cmd == "start_download":
        cfg = args.get("config", args)
        job_id = str(uuid.uuid4())
        dest_dir = cfg.get("downloadDir") or str(Path.home() / "Downloads" / "SteamDownloads")
        app_id = str(cfg.get("mainAppId") or cfg.get("app_id", ""))
        game_name = cfg.get("gameName", f"App {app_id}")
        safe_name = re.sub(r'[<>:"/\\|?*]', '_', game_name)
        target_dir = Path(dest_dir) / f"{app_id} - {safe_name}"

        active_jobs[job_id] = {"cancelled": False, "config": cfg}
        threading.Thread(target=execute_download, args=(job_id, cfg), daemon=True).start()
        return {"jobId": job_id, "downloadDir": str(target_dir)}

    elif cmd == "cancel_download":
        job_id = args.get("jobId") or args.get("job_id")
        if job_id in active_jobs:
            active_jobs[job_id]["cancelled"] = True
        return True

    elif cmd == "pause_download":
        return True

    elif cmd == "detect_executables":
        return detect_executables(args.get("downloadDir", ""))

    elif cmd in ("check_free_space", "get_disk_space"):
        path = args.get("path", str(Path.home()))
        try:
            usage = shutil.disk_usage(path)
            return {
                "free": usage.free,
                "total": usage.total,
                "freeGB": round(usage.free / (1024**3), 2),
                "drive": path,
                "path": path
            }
        except Exception:
            return {"free": 107374182400, "total": 536870912000, "freeGB": 100.0, "drive": "/", "path": path}

    elif cmd == "check_dotnet":
        return {"installed": True, "version": "python-native"}

    elif cmd == "is_shortcut_supported":
        return True

    elif cmd in ("check_for_updates", "get_auto_update_enabled"):
        return None

    elif cmd == "get_covers" or cmd == "check_game_updates":
        return {}

    elif cmd == "get_build_info":
        return {
            "version": "1.5.0-python",
            "channel": "python-standalone",
            "target": sys.platform
        }

    elif cmd == "open_folder":
        folder = args.get("path", "")
        if IS_ANDROID:
            try:
                from com.steammanifestdownloader import AndroidBridgeManager
                if AndroidBridgeManager.openFolder(folder):
                    return True
            except Exception:
                pass
        if os.path.exists(folder):
            if sys.platform == "win32":
                os.startfile(folder)
            elif sys.platform == "darwin":
                subprocess.Popen(["open", folder])
            else:
                subprocess.Popen(["xdg-open", folder])
        return True

    elif cmd in ("minimize_window", "maximize_window", "close_window", "restart_app",
                 "set_download_progress", "emit_telemetry_event", "get_telemetry_status",
                 "set_telemetry_consent"):
        return True

    elif cmd.startswith("queue_"):
        return [] if "list" in cmd else True

    elif cmd.startswith("emu_") or cmd.startswith("steam_"):
        return None

    return None

# ---------- HTTP Request Handler ----------
class AppServerHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(BASE_DIR), **kwargs)

    def log_message(self, fmt, *a):
        pass

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        if self.path == "/api/events":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()

            q = queue.Queue()
            with sse_lock:
                sse_clients.append(q)

            try:
                while True:
                    data = q.get()
                    self.wfile.write(data.encode("utf-8"))
                    self.wfile.flush()
            except (ConnectionResetError, BrokenPipeError):
                with sse_lock:
                    if q in sse_clients:
                        sse_clients.remove(q)
            return

        super().do_GET()

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length > 0 else b"{}"

        if self.path == "/api/invoke":
            try:
                payload = json.loads(body.decode("utf-8"))
                cmd = payload.get("cmd")
                args = payload.get("args", {})
                res = handle_invoke(cmd, args)
                resp_bytes = json.dumps({"data": res}).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(resp_bytes)
            except Exception as e:
                err_bytes = json.dumps({"error": str(e)}).encode("utf-8")
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(err_bytes)
            return

        elif self.path == "/api/upload_file":
            try:
                data = json.loads(body.decode("utf-8"))
                filename = data.get("name", "uploaded.lua")
                content = data.get("content", "")
                is_base64 = data.get("is_base64", False)
                save_path = TEMP_UPLOADS / filename
                if is_base64:
                    raw_bytes = base64.b64decode(content)
                    save_path.write_bytes(raw_bytes)
                else:
                    save_path.write_text(content, encoding="utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(json.dumps({"path": str(save_path)}).encode("utf-8"))
            except Exception as e:
                self.send_response(500)
                self.end_headers()
                self.wfile.write(json.dumps({"error": str(e)}).encode("utf-8"))
            return

        self.send_response(404)
        self.end_headers()

def run_server(port=8899):
    server = ThreadingHTTPServer(("0.0.0.0", port), AppServerHandler)
    url = f"http://localhost:{port}/index.html"
    print("=" * 60)
    print(" Steam Manifest Downloader (Python Standalone Edition)")
    print(f" Web UI: {url}")
    print(" Compatible with: Linux, Android (Termux/Browser), Windows, macOS")
    print(" Press Ctrl+C to stop.")
    print("=" * 60)
    try:
        webbrowser.open(url)
    except Exception:
        pass
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down server...")
        server.server_close()

if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 8899
    run_server(port)
