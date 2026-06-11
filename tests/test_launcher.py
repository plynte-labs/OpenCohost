# SPDX-License-Identifier: MIT
"""Unit tests for packaging/launcher.py — pure/testable functions only.

All tests are offline and do not touch the filesystem, subprocesses, or
tkinter.  UI glue and subprocess plumbing remain untested by design (kept
thin in the launcher).
"""

import importlib.util
import io
import json
import os
import sys
import types
import unittest
import zipfile as _zipfile
from unittest.mock import MagicMock, patch, call

# ---------------------------------------------------------------------------
# Import the launcher without executing __main__ side-effects.
# The launcher lives in packaging/, which is NOT a package — import by path.
# ---------------------------------------------------------------------------

_LAUNCHER_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "packaging",
    "launcher.py",
)


def _load_launcher():
    spec = importlib.util.spec_from_file_location("opencohost_launcher", _LAUNCHER_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_launcher = _load_launcher()


# ---------------------------------------------------------------------------
# Helper aliases
# ---------------------------------------------------------------------------

_version_tuple = _launcher._version_tuple
installed_version_satisfies = _launcher.installed_version_satisfies
parse_args = _launcher.parse_args
resolve_install_root = _launcher.resolve_install_root
build_wheel_spec = _launcher.build_wheel_spec
check_ollama = _launcher.check_ollama
parse_checksums = _launcher.parse_checksums
OllamaStatus = _launcher.OllamaStatus


# ===========================================================================
# 1. Version comparison / fast-path decision
# ===========================================================================


class TestVersionTuple(unittest.TestCase):
    def test_simple(self):
        self.assertEqual(_version_tuple("1.2.3"), (1, 2, 3))

    def test_leading_v(self):
        self.assertEqual(_version_tuple("v1.2.3"), (1, 2, 3))

    def test_leading_V(self):
        self.assertEqual(_version_tuple("V2.0.0"), (2, 0, 0))

    def test_single_component(self):
        self.assertEqual(_version_tuple("3"), (3,))

    def test_two_components(self):
        self.assertEqual(_version_tuple("1.5"), (1, 5))

    def test_empty_string(self):
        self.assertIsNone(_version_tuple(""))

    def test_none_input(self):
        self.assertIsNone(_version_tuple(None))

    def test_non_string(self):
        self.assertIsNone(_version_tuple(123))

    def test_non_numeric(self):
        self.assertIsNone(_version_tuple("1.x.3"))

    def test_whitespace_stripped(self):
        self.assertEqual(_version_tuple("  1.2.3  "), (1, 2, 3))


class TestInstalledVersionSatisfies(unittest.TestCase):
    def test_equal_versions(self):
        self.assertTrue(installed_version_satisfies("1.0.0", "1.0.0"))

    def test_installed_newer(self):
        self.assertTrue(installed_version_satisfies("1.1.0", "1.0.0"))

    def test_installed_older(self):
        self.assertFalse(installed_version_satisfies("0.9.0", "1.0.0"))

    def test_installed_has_leading_v(self):
        self.assertTrue(installed_version_satisfies("v1.0.0", "1.0.0"))

    def test_meta_has_leading_v(self):
        self.assertTrue(installed_version_satisfies("1.0.0", "v1.0.0"))

    def test_both_have_leading_v(self):
        self.assertTrue(installed_version_satisfies("v1.0.0", "v1.0.0"))

    def test_major_bump(self):
        self.assertFalse(installed_version_satisfies("0.1.0", "1.0.0"))

    def test_minor_bump(self):
        self.assertFalse(installed_version_satisfies("0.1.0", "0.2.0"))

    def test_patch_bump(self):
        self.assertFalse(installed_version_satisfies("0.1.0", "0.1.1"))

    def test_malformed_installed_falls_back_to_string_equality_match(self):
        # Both malformed + equal -> True
        self.assertTrue(installed_version_satisfies("dev", "dev"))

    def test_malformed_installed_falls_back_to_string_equality_mismatch(self):
        # Malformed -> falls back to string compare, "dev" != "1.0.0"
        self.assertFalse(installed_version_satisfies("dev", "1.0.0"))

    def test_installed_patch_newer(self):
        self.assertTrue(installed_version_satisfies("0.1.1", "0.1.0"))


# ===========================================================================
# 2. CLI argument parsing
# ===========================================================================


class TestParseArgs(unittest.TestCase):
    def test_no_args(self):
        args = parse_args([])
        self.assertIsNone(args.update)
        self.assertFalse(args.self_test)
        self.assertFalse(args.reinstall)
        self.assertFalse(args.headless)
        self.assertFalse(args.debug)
        self.assertIsNone(args.src_dir)
        self.assertFalse(args.no_launch)

    def test_update_flag_no_version(self):
        args = parse_args(["--update"])
        self.assertEqual(args.update, "latest")

    def test_update_flag_with_version(self):
        args = parse_args(["--update", "v0.2.0"])
        self.assertEqual(args.update, "v0.2.0")

    def test_self_test(self):
        args = parse_args(["--self-test"])
        self.assertTrue(args.self_test)

    def test_reinstall(self):
        args = parse_args(["--reinstall"])
        self.assertTrue(args.reinstall)

    def test_headless(self):
        args = parse_args(["--headless"])
        self.assertTrue(args.headless)

    def test_debug_sets_flag(self):
        args = parse_args(["--debug"])
        self.assertTrue(args.debug)

    def test_src_dir(self):
        args = parse_args(["--src-dir", "/some/path"])
        self.assertEqual(args.src_dir, "/some/path")

    def test_no_launch_hidden(self):
        args = parse_args(["--no-launch"])
        self.assertTrue(args.no_launch)


# ===========================================================================
# 3. Wheel spec construction
# ===========================================================================


class TestBuildWheelSpec(unittest.TestCase):
    """build_wheel_spec() must always install cloud-tts and local-tts extras."""

    def test_default_extras(self):
        spec = build_wheel_spec("opencohost")
        self.assertIn("cloud-tts", spec)
        self.assertIn("local-tts", spec)

    def test_returns_string(self):
        self.assertIsInstance(build_wheel_spec("opencohost"), str)

    def test_contains_package_name(self):
        spec = build_wheel_spec("opencohost")
        self.assertIn("opencohost", spec)

    def test_format_pep508(self):
        # Must be in PEP 508 extras notation: pkg[extra1,extra2]
        spec = build_wheel_spec("opencohost")
        self.assertRegex(spec, r"opencohost\[.*cloud-tts.*\]")
        self.assertRegex(spec, r"opencohost\[.*local-tts.*\]")

    def test_custom_extras(self):
        spec = build_wheel_spec("opencohost", extras=["cloud-tts", "local-tts", "integrations"])
        self.assertIn("integrations", spec)


# ===========================================================================
# 4. Ollama preflight — decision logic (mocked subprocess + HTTP)
# ===========================================================================


class TestCheckOllama(unittest.TestCase):
    """check_ollama() returns an OllamaStatus namedtuple (or similar) with
    at least .available (bool) and .message (str).
    """

    def _run(self, cli_returncode=0, http_ok=True, cli_raises=None):
        """Helper: mock subprocess and HTTP, return OllamaStatus."""
        with patch("subprocess.run") as mock_sub, \
             patch("urllib.request.urlopen") as mock_http:
            if cli_raises is not None:
                mock_sub.side_effect = cli_raises
            else:
                proc = MagicMock()
                proc.returncode = cli_returncode
                proc.stdout = "ollama version 0.3.0\n"
                mock_sub.return_value = proc

            if http_ok:
                response = MagicMock()
                response.__enter__ = lambda s: s
                response.__exit__ = MagicMock(return_value=False)
                response.read.return_value = b'{"models": []}'
                mock_http.return_value = response
            else:
                mock_http.side_effect = OSError("connection refused")

            return check_ollama()

    def test_both_ok_is_available(self):
        status = self._run(cli_returncode=0, http_ok=True)
        self.assertTrue(status.available)

    def test_cli_missing_http_ok_still_available(self):
        # If the HTTP ping responds, Ollama is running even if CLI lookup failed.
        status = self._run(cli_raises=FileNotFoundError("ollama not on PATH"), http_ok=True)
        self.assertTrue(status.available)

    def test_cli_ok_http_fails_not_available(self):
        # CLI found but server not responding -> not available.
        status = self._run(cli_returncode=0, http_ok=False)
        self.assertFalse(status.available)

    def test_both_fail_not_available(self):
        status = self._run(cli_raises=FileNotFoundError(), http_ok=False)
        self.assertFalse(status.available)

    def test_message_is_string(self):
        status = self._run(cli_returncode=0, http_ok=True)
        self.assertIsInstance(status.message, str)

    def test_not_available_message_contains_url(self):
        status = self._run(cli_raises=FileNotFoundError(), http_ok=False)
        self.assertIn("ollama.com/download", status.message)

    def test_available_message_not_empty(self):
        status = self._run(cli_returncode=0, http_ok=True)
        self.assertGreater(len(status.message), 0)

    def test_cli_nonzero_http_ok_still_available(self):
        # Edge case: CLI returns nonzero (path broken) but HTTP ping responds.
        status = self._run(cli_returncode=1, http_ok=True)
        self.assertTrue(status.available)


# ===========================================================================
# 5. Install root resolution
# ===========================================================================


class TestResolveInstallRoot(unittest.TestCase):
    def test_env_var_override(self):
        fake_env = {"OPENCOHOST_INSTALL_ROOT": "/tmp/oc-test"}
        root, portable = resolve_install_root(platform="win32", environ=fake_env)
        self.assertEqual(root, os.path.abspath("/tmp/oc-test"))
        self.assertFalse(portable)

    def test_default_windows_path(self):
        fake_env = {"LOCALAPPDATA": "C:\\Users\\Test\\AppData\\Local"}
        root, portable = resolve_install_root(platform="win32", environ=fake_env)
        self.assertIn("OpenCohost", root)
        self.assertIn("AppData", root)

    def test_portable_marker(self, tmp_path=None):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            marker = os.path.join(td, "portable.marker")
            open(marker, "w").close()
            root, portable = resolve_install_root(
                platform="win32", environ={}, launcher_dir=td
            )
            self.assertTrue(portable)
            self.assertEqual(root, os.path.join(td, "data"))

    def test_env_var_portable_false(self):
        fake_env = {"OPENCOHOST_INSTALL_ROOT": "C:\\some\\path"}
        _, portable = resolve_install_root(platform="win32", environ=fake_env)
        self.assertFalse(portable)


# ===========================================================================
# 6. parse_checksums (inherited from LiveAudio, must still work)
# ===========================================================================


class TestParseChecksums(unittest.TestCase):
    def test_sha256sums_format(self):
        text = "abc123def  opencohost-src-0.1.0.zip\n"
        result = parse_checksums(text, "opencohost-src-0.1.0.zip")
        self.assertEqual(result, "abc123def")

    def test_bare_digest(self):
        digest = "a" * 64
        result = parse_checksums(digest + "\n", "anything.zip")
        self.assertEqual(result, digest)

    def test_no_match(self):
        text = "abc123  other-file.zip\n"
        result = parse_checksums(text, "opencohost-src-0.1.0.zip")
        self.assertIsNone(result)

    def test_star_prefix(self):
        text = "abc123  *opencohost-src-0.1.0.zip\n"
        result = parse_checksums(text, "opencohost-src-0.1.0.zip")
        self.assertEqual(result, "abc123")

    def test_empty(self):
        result = parse_checksums("", "opencohost-src-0.1.0.zip")
        self.assertIsNone(result)

    def test_multiple_lines(self):
        text = (
            "deadbeef  other.zip\n"
            "cafebabe  opencohost-src-0.1.0.zip\n"
            "11223344  third.zip\n"
        )
        result = parse_checksums(text, "opencohost-src-0.1.0.zip")
        self.assertEqual(result, "cafebabe")


# ===========================================================================
# 7. GITHUB_REPO and APP_WINDOW_TITLE branding smoke-check
# ===========================================================================


class TestBranding(unittest.TestCase):
    def test_github_repo_is_opencohost(self):
        self.assertIn("opencohost", _launcher.GITHUB_REPO.lower())
        self.assertIn("plynte-labs", _launcher.GITHUB_REPO.lower())

    def test_app_window_title_contains_opencohost(self):
        self.assertIn("OpenCohost", _launcher.APP_WINDOW_TITLE)

    def test_ollama_download_url_in_constants(self):
        self.assertIn("ollama.com", _launcher.OLLAMA_DOWNLOAD_URL)


# ===========================================================================
# 8. Pre-release version string parsing
# ===========================================================================


class TestVersionTuplePreRelease(unittest.TestCase):
    def test_beta_suffix_stripped(self):
        # "1.2.0b1" must parse as (1, 2, 0) — the beta label is a PEP 440
        # pre-release marker, not a malformed version.
        self.assertEqual(_version_tuple("1.2.0b1"), (1, 2, 0))

    def test_rc_suffix_stripped(self):
        self.assertEqual(_version_tuple("2.0.0rc1"), (2, 0, 0))

    def test_alpha_suffix_stripped(self):
        self.assertEqual(_version_tuple("1.0.0a3"), (1, 0, 0))

    def test_plain_version_unchanged(self):
        # Stripping logic must not alter plain numeric versions.
        self.assertEqual(_version_tuple("1.2.3"), (1, 2, 3))

    def test_leading_v_with_prerelease(self):
        self.assertEqual(_version_tuple("v1.2.0b1"), (1, 2, 0))

    def test_all_non_numeric_segment_is_none(self):
        # A segment with no leading digits is still malformed.
        self.assertIsNone(_version_tuple("1.x.3"))


class TestInstalledVersionSatisfiesPreRelease(unittest.TestCase):
    def test_prerelease_installed_satisfies_release(self):
        # "1.2.0b1" installed, meta says "1.2.0" — same base version, satisfies.
        self.assertTrue(installed_version_satisfies("1.2.0b1", "1.2.0"))

    def test_release_installed_satisfies_prerelease_meta(self):
        # "1.2.0" installed, meta says "1.2.0rc1" — installed is newer, satisfies.
        self.assertTrue(installed_version_satisfies("1.2.0", "1.2.0rc1"))

    def test_prerelease_both_same_base(self):
        # Both parse to the same tuple — equal, so satisfies.
        self.assertTrue(installed_version_satisfies("1.2.0b1", "1.2.0rc2"))

    def test_older_prerelease_does_not_satisfy_newer(self):
        # "1.1.0b1" -> (1, 1, 0) < (1, 2, 0) — does not satisfy.
        self.assertFalse(installed_version_satisfies("1.1.0b1", "1.2.0"))


# ===========================================================================
# 9. Zip-slip guard
# ===========================================================================


def _make_zip_bytes(*members):
    """Return in-memory bytes of a ZIP archive with the given member names (empty content)."""
    buf = io.BytesIO()
    with _zipfile.ZipFile(buf, "w") as zf:
        for name in members:
            zf.writestr(name, "evil content")
    return buf.getvalue()


class TestZipSlipGuard(unittest.TestCase):
    """unpack_src_zip() must reject archives with path-traversal member names."""

    def _call_unpack(self, zip_bytes, tmp_dir):
        import tempfile
        zip_path = os.path.join(tmp_dir, "test.zip")
        with open(zip_path, "wb") as fh:
            fh.write(zip_bytes)
        target = os.path.join(tmp_dir, "app")
        _launcher.unpack_src_zip(zip_path, target)

    def test_dotdot_member_raises_launcher_error(self):
        import tempfile
        bad_zip = _make_zip_bytes("../evil.txt")
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(_launcher.LauncherError) as ctx:
                self._call_unpack(bad_zip, td)
            self.assertIn("Zip-slip", str(ctx.exception))

    def test_nested_dotdot_raises_launcher_error(self):
        import tempfile
        bad_zip = _make_zip_bytes("subdir/../../../evil.txt")
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(_launcher.LauncherError):
                self._call_unpack(bad_zip, td)

    def test_safe_zip_does_not_raise(self):
        import tempfile
        safe_zip = _make_zip_bytes("topdir/main.py", "topdir/README.md")
        with tempfile.TemporaryDirectory() as td:
            # Should not raise — just complete normally.
            try:
                self._call_unpack(safe_zip, td)
            except _launcher.LauncherError as exc:
                self.fail("Safe zip raised LauncherError: %s" % exc)


# ===========================================================================
# 10. wait_for_app_window — injectable callables (no real sleeping)
# ===========================================================================


class TestWaitForAppWindow(unittest.TestCase):
    """wait_for_app_window() must return the correct outcome string for each
    scenario.  All callables are injected so the tests are instant and offline.
    """

    _wait = staticmethod(_launcher.wait_for_app_window)

    def test_found_immediately(self):
        # Window appears on the very first poll.
        result = self._wait(
            find_window=lambda: True,
            proc_alive=lambda: True,
            timeout=5.0,
            interval=0.5,
            sleep=lambda _: None,
        )
        self.assertEqual(result, "found")

    def test_found_after_several_ticks(self):
        calls = [0]

        def find_window():
            calls[0] += 1
            return calls[0] >= 3  # found on 3rd poll

        result = self._wait(
            find_window=find_window,
            proc_alive=lambda: True,
            timeout=10.0,
            interval=0.5,
            sleep=lambda _: None,
        )
        self.assertEqual(result, "found")

    def test_died_when_proc_exits(self):
        # Process dies on the first check after the window is not found.
        result = self._wait(
            find_window=lambda: False,
            proc_alive=lambda: False,
            timeout=10.0,
            interval=0.5,
            sleep=lambda _: None,
        )
        self.assertEqual(result, "died")

    def test_timeout_when_window_never_appears(self):
        # No window, proc alive, but timeout is reached.
        elapsed = [0.0]

        def fake_sleep(interval):
            elapsed[0] += interval

        result = self._wait(
            find_window=lambda: False,
            proc_alive=lambda: True,
            timeout=1.0,
            interval=0.5,
            sleep=fake_sleep,
        )
        self.assertEqual(result, "timeout")

    def test_no_sleep_called_when_found_immediately(self):
        sleep_calls = []
        self._wait(
            find_window=lambda: True,
            proc_alive=lambda: True,
            timeout=5.0,
            interval=0.5,
            sleep=sleep_calls.append,
        )
        self.assertEqual(sleep_calls, [])

    def test_proc_alive_none_does_not_return_died(self):
        # When proc_alive is not provided, "died" must never be returned.
        elapsed = [0.0]

        def fake_sleep(interval):
            elapsed[0] += interval

        result = self._wait(
            find_window=lambda: False,
            proc_alive=None,
            timeout=0.5,
            interval=0.5,
            sleep=fake_sleep,
        )
        self.assertEqual(result, "timeout")


if __name__ == "__main__":
    unittest.main()
