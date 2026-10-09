"""Portable encrypted state: synthetic values, fake contexts, no account or network."""
import copy
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from cryptography.fernet import Fernet

from utils import cloud_auth as auth


def fixture():
    return {
        "version": 1, "account_id": "10000000001", "expected_uid": "20000000002",
        "fingerprint": "23456",
        "storage_state": {
            "cookies": [{"name": "synthetic-session", "value": "synthetic-private-value",
                         "domain": ".douyin.com", "path": "/", "expires": -1,
                         "secure": True, "httpOnly": True, "sameSite": "None"}],
            "origins": [{"origin": "https://www.douyin.com",
                         "localStorage": [{"name": "synthetic-auth", "value": "本地状态"}],
                         "indexedDB": [{"name": "synthetic-db", "version": 1,
                                        "stores": [{"name": "auth", "autoIncrement": False,
                                                    "keyPath": "id", "indexes": [],
                                                    "records": [{"value": {"id": 1,
                                                                            "token": "synthetic-idb"}}]}]}]}],
        },
        "session_storage": {"https://www.douyin.com": {"synthetic-session": "会话状态"}},
    }


class EncryptedStateTests(unittest.TestCase):
    def setUp(self):
        self.key = Fernet.generate_key()
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.bundle = fixture()

    def test_full_state_round_trip_has_ciphertext_only_on_disk(self):
        path = auth.save_bundle(self.bundle, key=self.key, directory=self.directory.name)
        self.assertEqual(path.name, "10000000001.bin")
        encoded = path.read_bytes()
        self.assertNotIn(b"synthetic-private-value", encoded)
        self.assertNotIn(b"synthetic-idb", encoded)
        restored = auth.load_bundle("10000000001", expected_uid="20000000002",
                                    key=self.key, directory=self.directory.name)
        self.assertEqual(restored, self.bundle)
        self.assertEqual(list(Path(self.directory.name).iterdir()), [path])

    def test_environment_key_and_directory_are_supported(self):
        with patch.dict(os.environ, {"CLOUD_AUTH_KEY": self.key.decode("ascii"),
                                     "CLOUD_AUTH_DIR": self.directory.name}):
            auth.save_bundle(self.bundle)
            self.assertEqual(auth.load_bundle("10000000001"), self.bundle)

    def test_invalid_or_missing_key_never_writes_a_file(self):
        for key in (b"", b"invalid", "非ASCII", object()):
            with self.subTest(key_type=type(key).__name__):
                with self.assertRaises(auth.CloudAuthError):
                    auth.save_bundle(self.bundle, key=key, directory=self.directory.name)
        self.assertEqual(list(Path(self.directory.name).iterdir()), [])
        with patch.dict(os.environ, {"CLOUD_AUTH_KEY": ""}):
            with self.assertRaises(auth.CloudAuthError):
                auth.save_bundle(self.bundle, directory=self.directory.name)

    def test_wrong_key_or_tampered_token_is_rejected_without_payload_details(self):
        token = auth.seal_bytes(b"synthetic-private-value", self.key)
        for corrupted, key in ((token, Fernet.generate_key()),
                               (token[:-12] + b"ABCDEFGHIJKL", self.key),
                               (b"plain synthetic-private-value", self.key)):
            with self.assertRaises(auth.CloudAuthError) as caught:
                auth.open_bytes(corrupted, key)
            self.assertNotIn("synthetic-private-value", str(caught.exception))

    def test_login_view_and_auth_are_cryptographically_purpose_bound(self):
        token = auth.seal_bytes(b"synthetic-PNG", self.key, purpose="login-view")
        self.assertEqual(auth.open_bytes(token, self.key, purpose="login-view"), b"synthetic-PNG")
        with self.assertRaises(auth.CloudAuthError):
            auth.open_bytes(token, self.key, purpose="auth")
        token = auth.seal_bytes(b"synthetic-state", self.key, purpose="auth")
        with self.assertRaises(auth.CloudAuthError):
            auth.open_bytes(token, self.key, purpose="login-view")

    def test_invalid_purpose_and_payload_types_fail_safely(self):
        for purpose in ("other", [], None):
            with self.assertRaises(auth.CloudAuthError):
                auth.seal_bytes(b"payload", self.key, purpose=purpose)
        for payload in ("plaintext", bytearray(b"payload"), None):
            with self.assertRaises(auth.CloudAuthError):
                auth.seal_bytes(payload, self.key)
        with self.assertRaises(auth.CloudAuthError):
            auth.open_bytes("token", self.key)

    def test_wrong_account_and_uid_are_rejected_even_with_valid_encryption(self):
        path = auth.save_bundle(self.bundle, key=self.key, directory=self.directory.name)
        (Path(self.directory.name) / "30000000003.bin").write_bytes(path.read_bytes())
        with self.assertRaises(auth.CloudAuthError):
            auth.load_bundle("30000000003", key=self.key, directory=self.directory.name)
        with self.assertRaises(auth.CloudAuthError):
            auth.load_bundle("10000000001", expected_uid="30000000003",
                             key=self.key, directory=self.directory.name)

    def test_authenticated_but_non_json_or_wrong_version_is_rejected(self):
        path = auth.auth_path("10000000001", self.directory.name)
        for payload in (b"not-json", b"[]", b'{"version":2}'):
            auth.atomic_write_encrypted(path, auth.seal_bytes(payload, self.key))
            with self.assertRaises(auth.CloudAuthError):
                auth.load_bundle("10000000001", key=self.key, directory=self.directory.name)

    def test_missing_state_and_missing_directory_fail_without_fallback(self):
        with self.assertRaises(auth.CloudAuthError):
            auth.load_bundle("10000000001", key=self.key, directory=self.directory.name)
        with patch.dict(os.environ, {"CLOUD_AUTH_DIR": ""}):
            with self.assertRaises(auth.CloudAuthError):
                auth.auth_path("10000000001")

    def test_atomic_replace_failure_preserves_previous_ciphertext_and_cleans_temp(self):
        path = auth.save_bundle(self.bundle, key=self.key, directory=self.directory.name)
        original = path.read_bytes()
        next_state = copy.deepcopy(self.bundle)
        next_state["session_storage"]["https://www.douyin.com"]["new-key"] = "new-value"
        with patch.object(auth.os, "replace", side_effect=OSError("synthetic-private-error")):
            with self.assertRaises(auth.CloudAuthError) as caught:
                auth.save_bundle(next_state, key=self.key, directory=self.directory.name)
        self.assertNotIn("synthetic-private-error", str(caught.exception))
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(list(Path(self.directory.name).iterdir()), [path])

    def test_writer_refuses_plaintext(self):
        path = Path(self.directory.name) / "qr.bin"
        with self.assertRaises(auth.CloudAuthError):
            auth.atomic_write_encrypted(path, b"synthetic-plaintext")
        self.assertFalse(path.exists())

    def test_payload_and_ciphertext_bounds_are_enforced_before_processing(self):
        with patch.object(auth, "MAX_PLAINTEXT_BYTES", 3):
            with self.assertRaises(auth.CloudAuthError):
                auth.seal_bytes(b"four", self.key)
        with patch.object(auth, "MAX_ENCRYPTED_BYTES", 3):
            with self.assertRaises(auth.CloudAuthError):
                auth.open_bytes(b"four", self.key)


class BundleValidationTests(unittest.TestCase):
    def test_validation_makes_a_detached_json_copy(self):
        source = fixture()
        result = auth.validate_bundle(source)
        source["storage_state"]["cookies"][0]["value"] = "changed"
        self.assertEqual(result["storage_state"]["cookies"][0]["value"], "synthetic-private-value")

    def test_account_uid_fingerprint_and_version_are_strict(self):
        for field, values in {
            "account_id": ("../escape", "one/two", "", "x" * 129, 123),
            "expected_uid": ("123", "000000", "other", 20000000002),
            "fingerprint": ("9999", "100000", "01234", 23456),
            "version": (True, 2, "1"),
        }.items():
            for value in values:
                bad = fixture()
                bad[field] = value
                with self.subTest(field=field, kind=type(value).__name__):
                    with self.assertRaises(auth.CloudAuthError):
                        auth.validate_bundle(bad)
        with self.assertRaises(auth.CloudAuthError):
            auth.auth_path("../escape", ".")

    def test_fingerprint_is_stable_and_in_supported_range(self):
        for account_id in ("10000000001", "abc_123", "99999999999"):
            value = auth.deterministic_fingerprint(account_id)
            self.assertEqual(value, auth.deterministic_fingerprint(account_id))
            self.assertTrue(10000 <= int(value) <= 99999)
        self.assertEqual(auth.deterministic_fingerprint("abc_123"),
                         auth.deterministic_fingerprint("ABC_123"))

    def test_foreign_insecure_and_noncanonical_origins_are_rejected(self):
        origins = ("https://douyin.com.evil.test", "http://www.douyin.com",
                   "https://www.douyin.com/chat", "https://user@www.douyin.com",
                   "https://www.douyin.com:444", "https://www.douyin.com:443",
                   "https://WWW.douyin.com", "https://www.douyin.com/",
                   "https://www.douyin.com?x=1")
        for origin in origins:
            for field in ("storage_state", "session_storage"):
                bad = fixture()
                if field == "storage_state":
                    bad[field]["origins"][0]["origin"] = origin
                else:
                    bad[field] = {origin: {"key": "value"}}
                with self.subTest(field=field, origin=origin):
                    with self.assertRaises(auth.CloudAuthError):
                        auth.validate_bundle(bad)

    def test_cookies_are_constrained_to_target_domains_and_valid_shapes(self):
        mutations = ({"domain": ".evil.test"}, {"domain": ".douyin.com.evil.test"},
                     {"expires": float("nan")}, {"expires": 10 ** 1000},
                     {"expires": True}, {"path": "chat"}, {"secure": 1},
                     {"sameSite": "unknown"}, {"unexpected": "field"})
        for mutation in mutations:
            bad = fixture()
            bad["storage_state"]["cookies"][0].update(mutation)
            with self.subTest(fields=list(mutation)):
                with self.assertRaises(auth.CloudAuthError):
                    auth.validate_bundle(bad)

    def test_duplicate_origins_and_local_storage_keys_are_rejected(self):
        bad = fixture()
        bad["storage_state"]["origins"].append(copy.deepcopy(bad["storage_state"]["origins"][0]))
        with self.assertRaises(auth.CloudAuthError):
            auth.validate_bundle(bad)
        bad = fixture()
        entries = bad["storage_state"]["origins"][0]["localStorage"]
        entries.append(copy.deepcopy(entries[0]))
        with self.assertRaises(auth.CloudAuthError):
            auth.validate_bundle(bad)

    def test_collection_shapes_and_non_json_values_are_rejected(self):
        mutations = [lambda b: b.update(unexpected="field"),
                     lambda b: b["storage_state"].update(cookies={}),
                     lambda b: b["storage_state"].update(origins={}),
                     lambda b: b["storage_state"]["origins"][0].update(indexedDB={}),
                     lambda b: b["session_storage"].update({"https://www.douyin.com": []}),
                     lambda b: b["session_storage"]["https://www.douyin.com"].update({"key": None}),
                     lambda b: b["storage_state"]["origins"][0]["indexedDB"][0].update(value=object()),
                     lambda b: b["storage_state"]["origins"][0]["indexedDB"][0].update(value=float("inf"))]
        for mutate in mutations:
            bad = fixture()
            mutate(bad)
            with self.assertRaises(auth.CloudAuthError):
                auth.validate_bundle(bad)

    def test_internal_restore_marker_cannot_be_imported(self):
        bad = fixture()
        bad["session_storage"]["https://www.douyin.com"][auth._MARKER_PREFIX + "other"] = "1"
        with self.assertRaises(auth.CloudAuthError):
            auth.validate_bundle(bad)

    def test_size_and_depth_limits_are_enforced(self):
        for bound in ("MAX_COOKIES", "MAX_ORIGINS", "MAX_STORAGE_ENTRIES", "MAX_PLAINTEXT_BYTES"):
            with patch.object(auth, bound, 0):
                with self.assertRaises(auth.CloudAuthError):
                    auth.validate_bundle(fixture())
        bad = fixture()
        value = []
        for _ in range(65):
            value = [value]
        bad["storage_state"]["origins"][0]["indexedDB"][0]["value"] = value
        with self.assertRaises(auth.CloudAuthError):
            auth.validate_bundle(bad)


class BrowserStateTests(unittest.TestCase):
    def test_create_context_restores_full_state_and_fixed_locale_before_any_page(self):
        browser = MagicMock()
        bundle = fixture()
        context = auth.create_context(browser, bundle)
        browser.new_context.assert_called_once_with(storage_state=bundle["storage_state"],
                                                    locale="zh-CN", timezone_id="Asia/Shanghai")
        context.add_init_script.assert_called_once()
        context.add_cookies.assert_not_called()
        context.new_page.assert_not_called()

    def test_invalid_state_never_creates_a_context(self):
        browser = MagicMock()
        bad = fixture()
        bad["version"] = 2
        with self.assertRaises(auth.CloudAuthError):
            auth.create_context(browser, bad)
        browser.new_context.assert_not_called()

    def test_context_is_closed_on_install_failure_and_error_is_sanitized(self):
        browser = MagicMock()
        browser.new_context.return_value.add_init_script.side_effect = RuntimeError("synthetic-private-value")
        with self.assertRaises(auth.CloudAuthError) as caught:
            auth.create_context(browser, fixture())
        self.assertNotIn("synthetic-private-value", str(caught.exception))
        browser.new_context.return_value.close.assert_called_once()

    @unittest.skipUnless(shutil.which("node"), "Node needed for isolated JavaScript semantics")
    def test_session_restore_is_origin_gated_once_and_preserves_arbitrary_string_values(self):
        browser = MagicMock()
        bundle = fixture()
        values = {"MARKER": "SNAPSHOTS MARKER ` ${literal} \u2028 \"\\", "__proto__": "safe",
                  "synthetic-session": "original"}
        bundle["session_storage"]["https://www.douyin.com"] = values
        auth.create_context(browser, bundle)
        script = browser.new_context.return_value.add_init_script.call_args.kwargs["script"]
        harness = r"""
          const vm = require('node:vm');
          let input = '';
          process.stdin.setEncoding('utf8');
          process.stdin.on('data', chunk => input += chunk);
          process.stdin.on('end', () => {
            const data = JSON.parse(input), stored = new Map();
            let writes = 0;
            const sessionStorage = {getItem: k => stored.get(k) ?? null,
              setItem: (k, v) => { writes++; stored.set(k, v); }};
            const window = {location: {origin: 'https://evil.test'}, sessionStorage};
            vm.runInNewContext(data.script, {window});
            const foreignWrites = writes;
            window.location.origin = 'https://www.douyin.com';
            vm.runInNewContext(data.script, {window});
            const afterFirst = writes;
            stored.set('synthetic-session', 'refreshed-by-app');
            vm.runInNewContext(data.script, {window});
            process.stdout.write(JSON.stringify({foreignWrites, afterFirst, writes,
              entries: Object.fromEntries(stored)}));
          });
        """
        result = subprocess.run([shutil.which("node"), "-e", harness],
                                input=json.dumps({"script": script}), text=True, encoding="utf-8",
                                capture_output=True, timeout=15, check=True)
        observed = json.loads(result.stdout)
        self.assertEqual(observed["foreignWrites"], 0)
        self.assertEqual(observed["afterFirst"], len(values) + 1)
        self.assertEqual(observed["writes"], observed["afterFirst"])
        self.assertEqual(observed["entries"]["synthetic-session"], "refreshed-by-app")
        self.assertEqual(observed["entries"]["MARKER"], values["MARKER"])
        self.assertEqual(observed["entries"]["__proto__"], "safe")

    def context(self, state=None, frames=None):
        frame = SimpleNamespace(url="https://www.douyin.com/chat",
                                evaluate=MagicMock(return_value={"session-key": "session-value"}))
        return SimpleNamespace(storage_state=MagicMock(return_value=state or fixture()["storage_state"]),
                               pages=[SimpleNamespace(frames=frames or [frame])])

    def test_capture_includes_indexeddb_and_all_supported_storage(self):
        context = self.context()
        captured = auth.capture_context(context, "10000000001", "20000000002", "23456")
        context.storage_state.assert_called_once_with(indexed_db=True)
        self.assertEqual(captured["storage_state"], fixture()["storage_state"])
        self.assertEqual(captured["session_storage"],
                         {"https://www.douyin.com": {"session-key": "session-value"}})

    def test_capture_filters_unrelated_sites_and_internal_markers(self):
        state = fixture()["storage_state"]
        state["cookies"].append({"name": "unrelated", "value": "unrelated", "domain": ".evil.test"})
        state["origins"].append({"origin": "https://evil.test", "localStorage": []})
        frame = SimpleNamespace(url="https://www.douyin.com/chat", evaluate=MagicMock(return_value={
            "key": "value", auth._MARKER_PREFIX + "current": "1"}))
        unrelated = SimpleNamespace(url="https://evil.test", evaluate=MagicMock())
        context = self.context(state, [frame, unrelated])
        captured = auth.capture_context(context, "10000000001", "20000000002", "23456")
        self.assertEqual(captured["storage_state"], fixture()["storage_state"])
        self.assertEqual(captured["session_storage"], {"https://www.douyin.com": {"key": "value"}})
        unrelated.evaluate.assert_not_called()

    def test_capture_same_origin_conflicts_fail_closed_instead_of_choosing_stale_tab(self):
        frames = [SimpleNamespace(url="https://www.douyin.com/chat", evaluate=lambda expr: {"key": "first"}),
                  SimpleNamespace(url="https://www.douyin.com/user", evaluate=lambda expr: {"key": "second"})]
        with self.assertRaises(auth.CloudAuthError):
            auth.capture_context(self.context(frames=frames), "10000000001", "20000000002", "23456")

    def test_capture_multiple_origins_and_identical_tabs(self):
        frames = [SimpleNamespace(url="https://www.douyin.com/chat", evaluate=lambda expr: {"key": "same"}),
                  SimpleNamespace(url="https://www.douyin.com/user", evaluate=lambda expr: {"key": "same"}),
                  SimpleNamespace(url="https://sso.douyin.com/login", evaluate=lambda expr: {"sso": "same"})]
        captured = auth.capture_context(self.context(frames=frames), "10000000001", "20000000002", "23456")
        self.assertEqual(captured["session_storage"], {
            "https://www.douyin.com": {"key": "same"}, "https://sso.douyin.com": {"sso": "same"}})

    def test_capture_error_is_sanitized_and_old_api_is_not_silently_used(self):
        context = self.context()
        context.storage_state.side_effect = TypeError("synthetic-private-value")
        with self.assertRaises(auth.CloudAuthError) as caught:
            auth.capture_context(context, "10000000001", "20000000002", "23456")
        self.assertNotIn("synthetic-private-value", str(caught.exception))
        context.storage_state.assert_called_once_with(indexed_db=True)


if __name__ == "__main__":
    unittest.main()
