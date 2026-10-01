"""Curses login behavior without a terminal, credentials or network."""

import builtins
import importlib.util
import os
import unittest
from collections import deque
from contextlib import ExitStack
from unittest.mock import Mock, patch

import gui
import sso_login


TOKEN = "0123456789abcdef0123456789abcdef"  # gitleaks:allow -- synthetic token
PASSWORD = "tui-test-password"  # gitleaks:allow -- synthetic test credential
SMS_CODE = "123456"


class Window:
    def __init__(self, owner, entries=(), keys=()):
        self.owner = owner
        self.entries = deque(entries)
        self.keys = deque(keys)
        self.read_echo = []
        self.display = []
        self.refreshes = 0

    def getmaxyx(self):
        return 30, 120

    def clear(self):
        pass

    def refresh(self):
        self.refreshes += 1

    def addnstr(self, row, column, text, width):
        self.display.append(text)

    def getstr(self, *args):
        self.read_echo.append(self.owner.echoing)
        if not self.entries:
            self.owner.fail("Unexpected extra input prompt")
        value = self.entries.popleft()
        if isinstance(value, BaseException):
            raise value
        return value.encode("utf-8")

    def getch(self):
        if not self.keys:
            self.owner.fail("Unexpected extra key prompt (retry or fallback)")
        value = self.keys.popleft()
        if isinstance(value, BaseException):
            raise value
        return value


class SsoTuiTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.echoing = False
        self.stack.enter_context(patch.dict(os.environ, {}, clear=True))
        self.stack.enter_context(patch("curses.echo", side_effect=self.echo))
        self.stack.enter_context(patch("curses.noecho", side_effect=self.noecho))
        self.stack.enter_context(patch("curses.start_color"))
        self.stack.enter_context(patch("curses.init_pair"))
        self.stack.enter_context(patch("builtins.input", side_effect=AssertionError("terminal input")))
        self.stack.enter_context(patch("getpass.getpass", side_effect=AssertionError("terminal getpass")))
        self.stack.enter_context(patch("builtins.print", side_effect=AssertionError("terminal print")))
        self.stack.enter_context(patch("socket.socket.connect", side_effect=AssertionError("network")))
        self.stack.enter_context(patch.object(gui.utils, "read_auth",
                                             side_effect=AssertionError("legacy token prompt")))
        self.stack.enter_context(patch.object(gui.utils, "write_auth",
                                             side_effect=AssertionError("TUI must delegate token storage")))
        self.stack.enter_context(patch("requests.Session.request",
                                      side_effect=AssertionError("network")))
        self.login = self.stack.enter_context(patch("sso_login.login", return_value=TOKEN))
        for name in ("videoList", "courseName", "professor", "selected_videos",
                     "selected_signal", "download_audio", "align"):
            self.stack.enter_context(patch.object(gui, name, getattr(gui, name)))

    def echo(self):
        self.echoing = True

    def noecho(self):
        self.echoing = False

    def assert_hidden(self, window):
        shown = "\n".join(window.display)
        for secret in (PASSWORD, SMS_CODE, TOKEN):
            self.assertNotIn(secret, shown)
        self.assertFalse(self.echoing)

    def test_credentials_and_sms_are_read_inside_one_login_with_correct_echo(self):
        window = Window(self, ["student", PASSWORD, SMS_CODE])

        def login(username, password, *, sms_code=None, code_provider=None, timeout=15):
            self.assertEqual((username, password), ("student", PASSWORD))
            self.assertIsNone(sms_code)
            self.assertIn("正在登录", "".join(window.display).replace(" ", ""))
            self.assertGreater(window.refreshes, 0)
            self.assertFalse(self.echoing)
            self.assertEqual(code_provider(), SMS_CODE)
            self.assertFalse(self.echoing)
            return TOKEN

        self.login.side_effect = login
        self.assertEqual(gui.login_tui(window), TOKEN)
        self.login.assert_called_once()
        self.assertEqual(window.read_echo, [True, False, False])
        self.assertFalse(window.entries)
        self.assert_hidden(window)

    def test_environment_credentials_and_sms_do_not_prompt(self):
        os.environ.update(STUDENT_ID="student", PASSWORD=PASSWORD, SMS_CODE=SMS_CODE)
        window = Window(self)

        def login(username, password, *, sms_code=None, code_provider=None, timeout=15):
            self.assertEqual((username, password, sms_code), ("student", PASSWORD, SMS_CODE))
            return TOKEN

        self.login.side_effect = login
        self.assertEqual(gui.login_tui(window), TOKEN)
        self.assertEqual(window.read_echo, [])
        self.assert_hidden(window)

    def test_only_missing_password_is_prompted_and_whitespace_is_preserved(self):
        os.environ["STUDENT_ID"] = "student"
        window = Window(self, [" " + PASSWORD + " "])
        self.assertEqual(gui.login_tui(window), TOKEN)
        self.assertEqual(self.login.call_args.args, ("student", " " + PASSWORD + " "))
        self.assertEqual(window.read_echo, [False])
        self.assert_hidden(window)

    def test_only_missing_username_is_echoed(self):
        os.environ["PASSWORD"] = PASSWORD
        window = Window(self, ["student"])
        self.assertEqual(gui.login_tui(window), TOKEN)
        self.assertEqual(self.login.call_args.args, ("student", PASSWORD))
        self.assertEqual(window.read_echo, [True])
        self.assert_hidden(window)

    def test_empty_or_interrupted_credentials_cancel_and_restore_noecho(self):
        for entries in ([""], [KeyboardInterrupt()], [EOFError()], ["\x03"],
                        ["student", ""], ["student", KeyboardInterrupt()]):
            with self.subTest(entries_kind=[type(value).__name__ for value in entries]):
                self.login.reset_mock()
                window = Window(self, entries)
                with self.assertRaises((sso_login.LoginError, KeyboardInterrupt, EOFError)):
                    gui.login_tui(window)
                self.login.assert_not_called()
                self.assert_hidden(window)

    def test_empty_or_interrupted_sms_cancels_without_second_login(self):
        os.environ.update(STUDENT_ID="student", PASSWORD=PASSWORD)
        for answer in ("", KeyboardInterrupt(), EOFError(), "\x03"):
            with self.subTest(answer_kind=type(answer).__name__):
                self.login.reset_mock()
                window = Window(self, [answer])

                def login(username, password, *, sms_code=None, code_provider=None, timeout=15):
                    code_provider()
                    self.fail("Canceled SMS must not complete login")

                self.login.side_effect = login
                with self.assertRaises((sso_login.LoginError, KeyboardInterrupt, EOFError)):
                    gui.login_tui(window)
                self.login.assert_called_once()
                self.assertEqual(window.read_echo, [False])
                self.assert_hidden(window)

    def course_fixture(self):
        return self.stack.enter_context(patch.object(gui.utils, "get_course_info", return_value=(
            [{"title": "lesson"}], "course", "teacher",
        )))

    def test_config_reuses_valid_auth_without_prompting_and_keeps_menu_choices(self):
        window = Window(self, ["12345"], [ord(" "), 10, ord(" "), 10, 10])
        info = self.course_fixture()
        ensure = self.stack.enter_context(patch.object(gui.utils, "ensure_auth", create=True,
                                                       return_value=True))
        gui.config(window)
        ensure.assert_called_once()
        self.assertEqual(len(ensure.call_args.args), 1)
        self.assertTrue(callable(ensure.call_args.args[0]))
        self.login.assert_not_called()
        info.assert_called_once_with(courseID="12345")
        self.assertEqual(gui.selected_videos, [0])
        self.assertEqual(gui.selected_signal, [0])
        self.assertEqual(gui.download_audio, [0])
        self.assertEqual(window.read_echo, [True])
        self.assert_hidden(window)

    def test_config_passes_default_hidden_token_prompt_after_course_id(self):
        window = Window(self, ["12345", TOKEN],
                        [10, ord(" "), 10, ord(" "), 10, 10])
        info = self.course_fixture()

        def ensure(login_prompt):
            self.assertEqual(window.read_echo, [True])
            self.assertEqual(login_prompt(), TOKEN)
            return True

        self.stack.enter_context(patch.object(gui.utils, "ensure_auth", create=True,
                                              side_effect=ensure))
        gui.config(window)
        self.login.assert_not_called()
        info.assert_called_once_with(courseID="12345")
        self.assertEqual(window.read_echo, [True, False])
        self.assert_hidden(window)

    def test_config_passes_explicit_sso_prompt_to_ensure_auth_after_course_id(self):
        window = Window(self, ["12345", "student", PASSWORD],
                        [ord("2"), 10, ord(" "), 10, ord(" "), 10, 10])
        info = self.course_fixture()

        def ensure(login_prompt):
            self.assertEqual(window.read_echo, [True])
            self.assertEqual(login_prompt(), TOKEN)
            return True

        self.stack.enter_context(patch.object(gui.utils, "ensure_auth", create=True,
                                              side_effect=ensure))
        gui.config(window)
        info.assert_called_once_with(courseID="12345")
        self.assertEqual(window.read_echo, [True, True, False])
        self.assert_hidden(window)

    def test_login_failure_exits_before_course_selection_or_download(self):
        window = Window(self, ["12345"], [10])
        info = self.course_fixture()
        self.stack.enter_context(patch.object(gui.utils, "ensure_auth", create=True,
                                              side_effect=sso_login.LoginError("登录失败")))
        download = self.stack.enter_context(patch.object(gui.m3u8dl, "M3u8Download"))
        wrapper = self.stack.enter_context(patch("curses.wrapper", side_effect=lambda fn: fn(window)))
        with self.assertRaises(SystemExit):
            gui.main()
        wrapper.assert_called_once_with(gui.config)
        info.assert_not_called()
        download.assert_not_called()
        self.assertTrue(window.display)
        self.assert_hidden(window)

    def test_cancel_in_config_shows_safe_message_and_does_not_select_course(self):
        for entries in (["12345", ""], ["12345", KeyboardInterrupt()],
                        ["12345", EOFError()]):
            with self.subTest(stage=len(entries)):
                window = Window(self, entries, [10, 10])
                with patch.object(gui.utils, "ensure_auth", create=True,
                                  side_effect=lambda login_prompt: bool(login_prompt())), \
                        patch.object(gui.utils, "get_course_info") as info:
                    with self.assertRaises(SystemExit):
                        gui.config(window)
                info.assert_not_called()
                self.assert_hidden(window)

    def test_token_works_when_sso_imports_and_credential_environment_are_blocked(self):
        original_import = builtins.__import__
        original_getitem = os._Environ.__getitem__

        def guarded_import(name, *args, **kwargs):
            if name.split(".")[0] in ("sso_login", "sso_crypto", "Crypto"):
                raise ImportError("SSO deliberately unavailable")
            return original_import(name, *args, **kwargs)

        def guarded_getitem(environ, name):
            if name in ("STUDENT_ID", "PASSWORD", "SMS_CODE"):
                raise AssertionError("Token login must not read credentials")
            return original_getitem(environ, name)

        spec = importlib.util.spec_from_file_location("isolated_gui", gui.__file__)
        isolated_gui = importlib.util.module_from_spec(spec)
        window = Window(self, [TOKEN], [10])
        with patch("builtins.__import__", side_effect=guarded_import), \
                patch.object(os._Environ, "__getitem__", guarded_getitem):
            spec.loader.exec_module(isolated_gui)
            self.assertEqual(isolated_gui.login_prompt_tui(window), TOKEN)
        self.assertEqual(window.read_echo, [False])
        self.assert_hidden(window)

    def test_explicit_sso_via_arrows_or_number_uses_existing_login(self):
        for keys in ([gui.curses.KEY_DOWN, 10], [gui.curses.KEY_UP, 13],
                     [ord("2"), gui.curses.KEY_ENTER]):
            with self.subTest(keys=keys):
                self.login.reset_mock()
                window = Window(self, ["student", PASSWORD], keys)
                self.assertEqual(gui.login_prompt_tui(window), TOKEN)
                self.assertEqual(self.login.call_args.args, ("student", PASSWORD))
                self.assertEqual(window.read_echo, [True, False])
                self.assert_hidden(window)

    def test_number_one_can_return_selection_to_token(self):
        window = Window(self, [TOKEN], [ord("2"), ord("1"), 10])
        self.assertEqual(gui.login_prompt_tui(window), TOKEN)
        self.login.assert_not_called()
        self.assertEqual(window.read_echo, [False])
        self.assert_hidden(window)

    def test_sso_failure_offers_safe_manual_token_fallback_without_retry(self):
        for error in (sso_login.LoginError(PASSWORD + TOKEN), ImportError(PASSWORD + TOKEN)):
            with self.subTest(error_kind=type(error).__name__):
                window = Window(self, ["student", PASSWORD, TOKEN], [ord("2"), 10, 10])
                self.login.reset_mock()
                self.login.side_effect = error
                self.assertEqual(gui.login_prompt_tui(window), TOKEN)
                self.login.assert_called_once()
                self.assertEqual(window.read_echo, [True, False, False])
                self.assertIn("token", "".join(window.display).lower())
                self.assert_hidden(window)

    def test_import_failure_before_credentials_can_fall_back_to_token(self):
        original_import = builtins.__import__

        def guarded_import(name, *args, **kwargs):
            if name == "sso_login":
                raise ImportError(PASSWORD)
            return original_import(name, *args, **kwargs)

        window = Window(self, [TOKEN], [ord("2"), 10, 10])
        with patch("builtins.__import__", side_effect=guarded_import):
            self.assertEqual(gui.login_prompt_tui(window), TOKEN)
        self.assertIn("uv sync --extra sso", "\n".join(window.display))
        self.assertEqual(window.read_echo, [False])
        self.assert_hidden(window)

    def test_selection_and_fallback_cancel_restore_noecho(self):
        for key in (ord("q"), 27, KeyboardInterrupt(), EOFError()):
            for fallback in (False, True):
                with self.subTest(key_kind=type(key).__name__, fallback=fallback):
                    self.echoing = True
                    self.login.reset_mock()
                    self.login.side_effect = sso_login.LoginError("safe failure")
                    window = Window(self, ["student", PASSWORD] if fallback else [],
                                    [ord("2"), 10, key] if fallback else [key])
                    with self.assertRaises((gui.utils.AuthError, KeyboardInterrupt, EOFError)):
                        gui.login_prompt_tui(window)
                    self.assertEqual(self.login.call_count, int(fallback))
                    self.assertFalse(window.entries)
                    self.assert_hidden(window)

    def test_token_empty_or_interrupted_input_cancels_without_sso(self):
        for entry in ("", "  ", "\x03", KeyboardInterrupt(), EOFError()):
            with self.subTest(entry_kind=type(entry).__name__):
                window = Window(self, [entry], [10])
                with self.assertRaises((gui.utils.AuthError, KeyboardInterrupt, EOFError)):
                    gui.login_prompt_tui(window)
                self.login.assert_not_called()
                self.assertEqual(window.read_echo, [False])
                self.assert_hidden(window)

    def test_sso_cancellation_does_not_offer_token_or_retry(self):
        for entries in ([""], [KeyboardInterrupt()], [EOFError()],
                        ["student", ""], ["student", KeyboardInterrupt()]):
            with self.subTest(entries_kind=[type(entry).__name__ for entry in entries]):
                window = Window(self, entries, [ord("2"), 10])
                with self.assertRaises((gui.utils.AuthError, KeyboardInterrupt, EOFError)):
                    gui.login_prompt_tui(window)
                self.login.assert_not_called()
                self.assertFalse(window.keys)
                self.assert_hidden(window)

    def test_sso_keyboard_or_eof_interruption_never_offers_fallback(self):
        for error in (KeyboardInterrupt(), EOFError()):
            with self.subTest(error_kind=type(error).__name__):
                self.login.reset_mock()
                self.login.side_effect = error
                window = Window(self, ["student", PASSWORD], [ord("2"), 10])
                with self.assertRaises((gui.utils.AuthError, KeyboardInterrupt, EOFError)):
                    gui.login_prompt_tui(window)
                self.login.assert_called_once()
                self.assertFalse(window.keys)
                self.assert_hidden(window)


if __name__ == "__main__":
    unittest.main()
