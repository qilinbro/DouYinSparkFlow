"""Portable browser authentication state, encrypted before any filesystem write.

Callers must verify the logged-in account before capture. Metadata binding here
prevents restoring another account's file; it is not a server authentication check.
"""

import base64
import hashlib
import json
import math
import os
import re
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

from cryptography.fernet import Fernet, InvalidToken


VERSION = 1
MAX_PLAINTEXT_BYTES = 16 * 1024 * 1024
MAX_ENCRYPTED_BYTES = 24 * 1024 * 1024
MAX_ORIGINS = 128
MAX_COOKIES = 2048
MAX_STORAGE_ENTRIES = 4096
MAX_VALUE_CHARS = 1024 * 1024
TARGET_DOMAINS = ("douyin.com", "bytedance.com", "snssdk.com", "iesdouyin.com", "amemv.com")
_MARKER_PREFIX = "__douyin_spark_cloud_auth_"
_PURPOSES = {"auth", "login-view"}


class CloudAuthError(ValueError):
    """A state error that never includes tokens, storage values or account details."""


def _account_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_]{1,128}", value):
        raise CloudAuthError("Account identifier is invalid.")
    return value


def _uid(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{5,25}", value) or int(value) == 0:
        raise CloudAuthError("Expected account UID is invalid.")
    return value


def _fingerprint(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{5}", value):
        raise CloudAuthError("Browser fingerprint is invalid.")
    if not 10000 <= int(value) <= 99999:
        raise CloudAuthError("Browser fingerprint is invalid.")
    return value


def deterministic_fingerprint(account_id):
    account_id = _account_id(account_id)
    digest = hashlib.sha256(account_id.upper().encode("ascii")).digest()
    return str(10000 + int.from_bytes(digest[:8], "big") % 90000)


def _target_domain(domain):
    return isinstance(domain, str) and any(
        domain.lower().lstrip(".") == target
        or domain.lower().lstrip(".").endswith("." + target)
        for target in TARGET_DOMAINS
    )


def _origin(value):
    if not isinstance(value, str) or len(value) > 512:
        raise CloudAuthError("Storage origin is invalid.")
    try:
        parsed = urlsplit(value)
        valid = (parsed.scheme == "https" and _target_domain(parsed.hostname)
                 and parsed.username is None and parsed.password is None
                 and parsed.port is None and parsed.path == ""
                 and not parsed.query and not parsed.fragment
                 and value == "https://" + parsed.hostname)
    except ValueError:
        valid = False
    if not valid:
        raise CloudAuthError("Storage origin is invalid.")
    return value


def _text(value, limit=MAX_VALUE_CHARS, nonempty=False):
    if not isinstance(value, str) or len(value) > limit or (nonempty and not value):
        raise CloudAuthError("Storage text is invalid or too large.")


def _json_tree(value, depth=0, budget=None):
    if budget is None:
        budget = [200000]
    budget[0] -= 1
    if depth > 64 or budget[0] < 0:
        raise CloudAuthError("Storage structure is too large or deeply nested.")
    if value is None or isinstance(value, bool):
        return
    if isinstance(value, str):
        _text(value)
    elif isinstance(value, (int, float)):
        try:
            finite = math.isfinite(value)
        except OverflowError:
            finite = False
        if not finite:
            raise CloudAuthError("Storage numbers are invalid.")
    elif isinstance(value, list):
        for item in value:
            _json_tree(item, depth + 1, budget)
    elif isinstance(value, dict):
        for key, item in value.items():
            _text(key, 4096)
            _json_tree(item, depth + 1, budget)
    else:
        raise CloudAuthError("Storage structure must contain JSON values only.")


def _cookie(cookie):
    allowed = {"name", "value", "domain", "path", "expires", "httpOnly", "secure", "sameSite", "partitionKey"}
    if not isinstance(cookie, dict) or not set(cookie).issubset(allowed):
        raise CloudAuthError("Cookie structure is invalid.")
    _text(cookie.get("name"), 4096, nonempty=True)
    _text(cookie.get("value"), 65536)
    domain = cookie.get("domain")
    if (not _target_domain(domain) or len(domain) > 253
            or not re.fullmatch(r"\.?[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?", domain)):
        raise CloudAuthError("Cookie domain is invalid.")
    path = cookie.get("path")
    _text(path, 4096, nonempty=True)
    if not path.startswith("/"):
        raise CloudAuthError("Cookie path is invalid.")
    expiry = cookie.get("expires", -1)
    if (isinstance(expiry, bool) or not isinstance(expiry, (int, float))
            or not -1 <= expiry <= 100000000000 or not math.isfinite(expiry)):
        raise CloudAuthError("Cookie expiration is invalid.")
    for key in ("httpOnly", "secure"):
        if key in cookie and not isinstance(cookie[key], bool):
            raise CloudAuthError("Cookie flags are invalid.")
    if "sameSite" in cookie and cookie["sameSite"] not in ("Strict", "Lax", "None"):
        raise CloudAuthError("Cookie same-site setting is invalid.")
    if "partitionKey" in cookie:
        _text(cookie["partitionKey"], 512, nonempty=True)


def validate_bundle(bundle, account_id=None, expected_uid=None):
    allowed = {"version", "account_id", "expected_uid", "fingerprint", "storage_state", "session_storage"}
    if not isinstance(bundle, dict) or set(bundle) != allowed:
        raise CloudAuthError("Authentication state structure is invalid.")
    if type(bundle["version"]) is not int or bundle["version"] != VERSION:
        raise CloudAuthError("Authentication state version is unsupported.")
    actual_id = _account_id(bundle["account_id"])
    actual_uid = _uid(bundle["expected_uid"])
    _fingerprint(bundle["fingerprint"])
    if account_id is not None and actual_id != _account_id(account_id):
        raise CloudAuthError("Authentication state belongs to a different account.")
    if expected_uid is not None and actual_uid != _uid(expected_uid):
        raise CloudAuthError("Authentication state has an unexpected account UID.")

    state = bundle["storage_state"]
    if not isinstance(state, dict) or set(state) != {"cookies", "origins"}:
        raise CloudAuthError("Browser storage state is invalid.")
    if not isinstance(state["cookies"], list) or len(state["cookies"]) > MAX_COOKIES:
        raise CloudAuthError("Cookie collection is invalid or too large.")
    for cookie in state["cookies"]:
        _cookie(cookie)
    origins = state["origins"]
    if not isinstance(origins, list) or len(origins) > MAX_ORIGINS:
        raise CloudAuthError("Origin collection is invalid or too large.")
    seen = set()
    for origin in origins:
        if not isinstance(origin, dict) or not set(origin).issubset({"origin", "localStorage", "indexedDB"}):
            raise CloudAuthError("Origin storage structure is invalid.")
        name = _origin(origin.get("origin"))
        if name in seen:
            raise CloudAuthError("Duplicate storage origin.")
        seen.add(name)
        entries = origin.get("localStorage", [])
        if not isinstance(entries, list) or len(entries) > MAX_STORAGE_ENTRIES:
            raise CloudAuthError("Local storage collection is invalid or too large.")
        keys = set()
        for entry in entries:
            if not isinstance(entry, dict) or set(entry) != {"name", "value"}:
                raise CloudAuthError("Local storage entry is invalid.")
            _text(entry["name"], 4096)
            _text(entry["value"])
            if entry["name"] in keys:
                raise CloudAuthError("Duplicate local storage entry.")
            keys.add(entry["name"])
        if "indexedDB" in origin and not isinstance(origin["indexedDB"], list):
            raise CloudAuthError("IndexedDB collection is invalid.")

    session = bundle["session_storage"]
    if not isinstance(session, dict) or len(session) > MAX_ORIGINS:
        raise CloudAuthError("Session storage collection is invalid or too large.")
    for origin, entries in session.items():
        _origin(origin)
        if not isinstance(entries, dict) or len(entries) > MAX_STORAGE_ENTRIES:
            raise CloudAuthError("Session storage entries are invalid or too large.")
        for key, value in entries.items():
            _text(key, 4096)
            _text(value)
            if key.startswith(_MARKER_PREFIX):
                raise CloudAuthError("Internal storage markers cannot be imported.")
    _json_tree(bundle)
    try:
        payload = json.dumps(bundle, ensure_ascii=True, allow_nan=False, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise CloudAuthError("Authentication state cannot be serialized.") from None
    if len(payload) > MAX_PLAINTEXT_BYTES:
        raise CloudAuthError("Authentication state is too large.")
    return json.loads(payload)


def _fernet(key):
    if key is None:
        key = os.getenv("CLOUD_AUTH_KEY", "")
    if isinstance(key, str):
        try:
            key = key.encode("ascii")
        except UnicodeError:
            raise CloudAuthError("Encryption key is invalid.") from None
    if not isinstance(key, bytes):
        raise CloudAuthError("Encryption key is invalid.")
    try:
        return Fernet(key)
    except (ValueError, TypeError):
        raise CloudAuthError("Encryption key is invalid.") from None


def _purpose_prefix(purpose):
    if not isinstance(purpose, str) or purpose not in _PURPOSES:
        raise CloudAuthError("Encryption purpose is unsupported.")
    return b"DouYinSparkFlow/v1/" + purpose.encode("ascii") + b"\x00"


def seal_bytes(payload, key, *, purpose="auth"):
    if not isinstance(payload, bytes) or len(payload) > MAX_PLAINTEXT_BYTES:
        raise CloudAuthError("Encryption payload is invalid or too large.")
    return _fernet(key).encrypt(_purpose_prefix(purpose) + payload)


def open_bytes(token, key, *, purpose="auth"):
    if not isinstance(token, bytes) or len(token) > MAX_ENCRYPTED_BYTES:
        raise CloudAuthError("Encrypted state is invalid or too large.")
    cipher = _fernet(key)
    prefix = _purpose_prefix(purpose)
    try:
        payload = cipher.decrypt(token)
    except (InvalidToken, ValueError, TypeError):
        raise CloudAuthError("Encrypted state could not be authenticated.") from None
    if not payload.startswith(prefix) or len(payload) - len(prefix) > MAX_PLAINTEXT_BYTES:
        raise CloudAuthError("Encrypted state has an unexpected purpose or size.")
    return payload[len(prefix):]


def atomic_write_encrypted(path, encrypted_bytes):
    if not isinstance(encrypted_bytes, bytes) or len(encrypted_bytes) > MAX_ENCRYPTED_BYTES:
        raise CloudAuthError("Encrypted output is invalid or too large.")
    try:
        decoded = base64.b64decode(encrypted_bytes, altchars=b"-_", validate=True)
    except (ValueError, TypeError):
        raise CloudAuthError("Only encrypted output may be written.") from None
    if len(decoded) < 73 or decoded[0] != 0x80:
        raise CloudAuthError("Only encrypted output may be written.")
    path = Path(path)
    temporary = None
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".cloud-auth-", suffix=".tmp", delete=False) as handle:
            temporary = Path(handle.name)
            os.chmod(temporary, 0o600)
            handle.write(encrypted_bytes)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
    except (OSError, ValueError):
        raise CloudAuthError("Encrypted output could not be saved.") from None
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
    return path


def auth_path(account_id, directory=None):
    account_id = _account_id(account_id)
    directory = directory if directory is not None else os.getenv("CLOUD_AUTH_DIR", "")
    if not isinstance(directory, (str, os.PathLike)) or not str(directory).strip():
        raise CloudAuthError("Authentication state directory is not configured.")
    return Path(directory) / (account_id + ".bin")


def save_bundle(bundle, *, key=None, directory=None):
    bundle = validate_bundle(bundle)
    payload = json.dumps(bundle, ensure_ascii=True, allow_nan=False, separators=(",", ":")).encode("utf-8")
    encrypted = seal_bytes(payload, key, purpose="auth")
    return atomic_write_encrypted(auth_path(bundle["account_id"], directory), encrypted)


def load_bundle(account_id, *, expected_uid=None, key=None, directory=None):
    path = auth_path(account_id, directory)
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_ENCRYPTED_BYTES:
            raise CloudAuthError("Authentication state file is missing or invalid.")
        encrypted = path.read_bytes()
    except OSError:
        raise CloudAuthError("Authentication state file could not be read.") from None
    payload = open_bytes(encrypted, key, purpose="auth")
    try:
        bundle = json.loads(payload)
    except (ValueError, UnicodeError, RecursionError):
        raise CloudAuthError("Authentication state JSON is invalid.") from None
    return validate_bundle(bundle, account_id=account_id, expected_uid=expected_uid)


def create_context(browser, bundle):
    bundle = validate_bundle(bundle)
    context = None
    try:
        context = browser.new_context(storage_state=bundle["storage_state"],
                                      locale="zh-CN", timezone_id="Asia/Shanghai")
        marker = _MARKER_PREFIX + hashlib.sha256(bundle["account_id"].encode("ascii")).hexdigest()[:16]
        snapshots = json.dumps(json.dumps(bundle["session_storage"], ensure_ascii=True), ensure_ascii=True)
        script = """(() => {
          const snapshots = JSON.parse(SNAPSHOTS);
          const entries = snapshots[window.location.origin];
          if (!entries) return;
          const marker = MARKER;
          try {
            if (window.sessionStorage.getItem(marker) === '1') return;
            for (const [key, value] of Object.entries(entries)) window.sessionStorage.setItem(key, value);
            window.sessionStorage.setItem(marker, '1');
          } catch (_) {}
        })();""".replace("MARKER", json.dumps(marker)).replace("SNAPSHOTS", snapshots)
        context.add_init_script(script=script)
    except Exception:
        if context is not None:
            try:
                context.close()
            except Exception:
                pass
        raise CloudAuthError("Browser authentication state could not be restored.") from None
    return context


def capture_context(context, account_id, expected_uid, fingerprint):
    _account_id(account_id)
    _uid(expected_uid)
    _fingerprint(fingerprint)
    try:
        state = context.storage_state(indexed_db=True)
        state = {"cookies": [c for c in state.get("cookies", []) if _target_domain(c.get("domain"))],
                 "origins": [origin for origin in state.get("origins", [])
                             if _target_domain(urlsplit(origin.get("origin", "")).hostname)]}
        session = {}
        for page in context.pages:
            frames = getattr(page, "frames", [page])
            for frame in frames:
                parsed = urlsplit(frame.url)
                if parsed.scheme != "https" or not _target_domain(parsed.hostname):
                    continue
                origin = parsed.scheme + "://" + parsed.netloc
                values = frame.evaluate("() => Object.fromEntries(Object.entries(window.sessionStorage))")
                values = {k: v for k, v in values.items() if not k.startswith(_MARKER_PREFIX)}
                if origin in session and session[origin] != values:
                    raise CloudAuthError("Conflicting session storage snapshots cannot be exported.")
                session[origin] = values
    except CloudAuthError:
        raise
    except Exception:
        raise CloudAuthError("Browser authentication state could not be captured.") from None
    return validate_bundle({"version": VERSION, "account_id": account_id, "expected_uid": expected_uid,
                            "fingerprint": fingerprint, "storage_state": state, "session_storage": session})
