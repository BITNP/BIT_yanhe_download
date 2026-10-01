"""Optional plain-CLI SSO, with isolated auth files and no live login."""

import builtins
from contextlib import ExitStack, redirect_stdout
import importlib.util
import io
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import main
import utils


TOKEN = "0123456789abcdef0123456789abcdef"  # gitleaks:allow -- synthetic fixture


class LoginChoiceTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.directory = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        previous = Path.cwd()
        os.chdir(self.directory)
        self.stack.callback(os.chdir, previous)
        self.stack.enter_context(patch.dict(utils.headers, utils.headers.copy()))
        self.stack.enter_context(patch("socket.socket.connect", side_effect=AssertionError("Network forbidden")))
        self.output = self.stack.enter_context(redirect_stdout(io.StringIO()))

    def prompt(self):
        callback = getattr(main, "login_prompt", None)
        self.assertTrue(callable(callback), "CLI needs an explicit login-choice callback")
        return callback()

    def fake_sso(self, **kwargs):
        prompt = Mock(**kwargs)
        self.stack.enter_context(patch.dict("sys.modules", {"sso_login": SimpleNamespace(prompt_login=prompt)}))
        return prompt

    def test_default_and_explicit_token_are_hidden_stripped_and_unsaved(self):
        sso = self.fake_sso(side_effect=AssertionError("SSO requires explicit consent"))
        for choice in ("", "1", " 1 "):
            with self.subTest(choice=choice), patch("builtins.input", return_value=choice), \
                 patch("getpass.getpass", return_value="  " + TOKEN + "\n") as hidden:
                self.assertEqual(self.prompt(), TOKEN)
                hidden.assert_called_once()
                self.assertFalse(Path("auth.txt").exists(), "The helper owns persistence")
        sso.assert_not_called()
        self.assertNotIn(TOKEN, self.output.getvalue())

    def test_only_explicit_sso_calls_login_once_and_does_not_read_token(self):
        sso = self.fake_sso(return_value=TOKEN)
        with patch("builtins.input", return_value="2"), \
             patch("getpass.getpass", side_effect=AssertionError("No token prompt")):
            self.assertEqual(self.prompt(), TOKEN)
        sso.assert_called_once_with()
        self.assertFalse(Path("auth.txt").exists())

    def test_auth_failure_offers_token_without_retrying_credentials(self):
        self.assertTrue(callable(getattr(main, "login_prompt", None)))
        sso = self.fake_sso(side_effect=utils.AuthError("用户名或密码错误。"))
        with patch("builtins.input", side_effect=["2", ""]), \
             patch("getpass.getpass", return_value=TOKEN):
            self.assertEqual(self.prompt(), TOKEN)
        sso.assert_called_once_with()
        self.assertIn("用户名或密码错误", self.output.getvalue())

    def test_import_failure_has_safe_message_and_token_fallback(self):
        sso = self.fake_sso(side_effect=ImportError("private-module-path-secret"))
        with patch("builtins.input", side_effect=["2", ""]), \
             patch("getpass.getpass", return_value=TOKEN):
            self.assertEqual(self.prompt(), TOKEN)
        sso.assert_called_once_with()
        self.assertNotIn("private-module-path-secret", self.output.getvalue())
        self.assertIn("uv sync --extra sso", self.output.getvalue())

    def test_invalid_menu_choice_never_selects_sso(self):
        sso = self.fake_sso(side_effect=AssertionError("Invalid choice is not consent"))
        with patch("builtins.input", side_effect=["unexpected", "q"]), \
             patch("getpass.getpass", side_effect=AssertionError("No token chosen")):
            with self.assertRaises((EOFError, KeyboardInterrupt)):
                self.prompt()
        sso.assert_not_called()

    def test_cancel_or_empty_token_never_logs_in(self):
        sso = self.fake_sso(side_effect=AssertionError("Cancelled"))
        for choice, token in (("q", TOKEN), (" Q ", TOKEN), ("", " \n")):
            with self.subTest(choice=choice), patch("builtins.input", return_value=choice), \
                 patch("getpass.getpass", return_value=token):
                with self.assertRaises((EOFError, KeyboardInterrupt)):
                    self.prompt()
                self.assertFalse(Path("auth.txt").exists())
        sso.assert_not_called()

    def test_interrupts_at_menu_token_and_sso_cancel_without_fallback(self):
        for error in (EOFError, KeyboardInterrupt):
            for stage in ("menu", "token", "sso"):
                with self.subTest(error=error, stage=stage), ExitStack() as stack:
                    sso = Mock(side_effect=error if stage == "sso" else AssertionError("No SSO"))
                    stack.enter_context(patch.dict("sys.modules", {"sso_login": SimpleNamespace(prompt_login=sso)}))
                    read = stack.enter_context(patch("builtins.input", side_effect=error if stage == "menu" else ["2" if stage == "sso" else ""]))
                    hidden = stack.enter_context(patch("getpass.getpass", side_effect=error if stage == "token" else AssertionError("No token")))
                    with self.assertRaises(error):
                        self.prompt()
                    self.assertEqual(read.call_count, 1, "Cancellation must not offer fallback")
                    self.assertEqual(hidden.call_count, int(stage == "token"))

    def test_fallback_cancel_and_interrupt_never_read_token_or_retry(self):
        for answer in ("q", EOFError(), KeyboardInterrupt()):
            with self.subTest(answer=answer):
                sso = self.fake_sso(side_effect=ImportError("unavailable"))
                with patch("builtins.input", side_effect=["2", answer]), \
                     patch("getpass.getpass", side_effect=AssertionError("Cancelled")):
                    with self.assertRaises((EOFError, KeyboardInterrupt)):
                        self.prompt()
                sso.assert_called_once_with()

    def test_main_cancel_stops_before_course_lookup(self):
        with patch("sys.argv", ["main.py", "40524"]), \
             patch("builtins.input", return_value="q"), \
             patch("utils.get_course_info") as course:
            with self.assertRaises(SystemExit) as stopped:
                main.main()
        self.assertEqual(stopped.exception.code, 1)
        course.assert_not_called()
        self.assertFalse(Path("auth.txt").exists())

    def test_cli_token_works_without_sso_or_crypto_and_keeps_download_flow(self):
        """Catch eager imports, credential-env reads, and lost CLI/download inputs."""
        real_import = builtins.__import__
        real_getitem = os._Environ.__getitem__

        def blocked_import(name, *args, **kwargs):
            if name.split(".")[0] in {"sso_login", "Crypto"}:
                raise ImportError("unavailable-private-path")
            return real_import(name, *args, **kwargs)

        def guarded_environment(environ, key):
            if key in {"STUDENT_ID", "PASSWORD", "SMS_CODE"}:
                raise AssertionError("Default token must not read credentials")
            return real_getitem(environ, key)

        course_data = ([{"title": "lesson", "videos": [{"main": "camera", "vga": "screen"}], "video_ids": []}], "course", "teacher")
        response = SimpleNamespace(status_code=200, json=lambda: {"code": 0, "data": {"id": 1}})
        for choices in (["", "0", "2", "1"], ["2", "", "0", "2", "1"]):
            with self.subTest(choices=choices), ExitStack() as stack:
                Path("auth.txt").unlink(missing_ok=True)
                stack.enter_context(patch("builtins.__import__", side_effect=blocked_import))
                stack.enter_context(patch.object(os._Environ, "__getitem__", guarded_environment))
                spec = importlib.util.spec_from_file_location("isolated_plain_cli", main.__file__)
                isolated_main = importlib.util.module_from_spec(spec)
                try:
                    spec.loader.exec_module(isolated_main)
                except ImportError as error:
                    self.fail(f"Token CLI must import without SSO: {error}")
                stack.enter_context(patch("sys.argv", ["main.py", "40524"]))
                stack.enter_context(patch("builtins.input", side_effect=choices))
                stack.enter_context(patch("getpass.getpass", return_value=" " + TOKEN + " "))
                stack.enter_context(patch("utils.requests.get", return_value=response))
                course = stack.enter_context(patch("utils.get_course_info", return_value=course_data))
                download = stack.enter_context(patch("m3u8dl.M3u8Download"))
                isolated_main.main()
                self.assertEqual(Path("auth.txt").read_text(), TOKEN)
                self.assertEqual(utils.headers["Authorization"], "Bearer " + TOKEN)
                course.assert_called_once_with(courseID="40524")
                download.assert_called_once_with("screen", "output/course-screen", "course-teacher-lesson")
                self.assertNotIn(TOKEN, self.output.getvalue())
                self.assertNotIn("unavailable-private-path", self.output.getvalue())


if __name__ == "__main__":
    unittest.main()
