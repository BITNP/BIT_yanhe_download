"""Default terminal authentication, with isolated files and no real network."""

import io
import os
from contextlib import ExitStack, redirect_stdout
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import main
import requests
import sso_login
import utils


TOKEN = "0123456789abcdef0123456789abcdef"  # gitleaks:allow -- synthetic fixture
OLD_TOKEN = "fedcba9876543210fedcba9876543210"  # gitleaks:allow -- synthetic fixture


class TerminalAuthTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.directory = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        previous = Path.cwd()
        os.chdir(self.directory)
        self.stack.callback(os.chdir, previous)
        self.stack.enter_context(patch.dict(utils.headers, utils.headers.copy()))
        self.stack.enter_context(patch.dict(os.environ, {}, clear=True))
        self.stack.enter_context(patch("socket.socket.connect", side_effect=AssertionError("Network forbidden")))
        self.get = self.stack.enter_context(patch("utils.requests.get", return_value=self.user_response()))

    def test_missing_cache_prompts_and_persists_for_the_downloader(self):
        prompt = Mock(return_value=TOKEN)
        self.assertTrue(utils.ensure_auth(prompt))
        self.assertEqual(Path("auth.txt").read_text(), TOKEN)
        self.assertEqual(utils.headers["Authorization"], "Bearer " + TOKEN)
        prompt.assert_called_once_with()

    def test_login_failure_preserves_existing_token(self):
        Path("auth.txt").write_text(OLD_TOKEN)
        prompt = Mock(side_effect=sso_login.LoginError("Login rejected"))
        with patch("utils.requests.get", return_value=self.user_response(code=61101113)), self.assertRaises(sso_login.LoginError):
            utils.ensure_auth(prompt)
        self.assertEqual(Path("auth.txt").read_text(), OLD_TOKEN)
        self.assertEqual(utils.headers["Authorization"], "Bearer " + OLD_TOKEN)

    def test_save_failure_does_not_replace_authorization_header(self):
        utils.headers["Authorization"] = "Bearer " + OLD_TOKEN
        with patch("utils.save_auth", side_effect=OSError("read only")), self.assertRaises(OSError):
            utils.ensure_auth(lambda: TOKEN)
        self.assertEqual(utils.headers["Authorization"], "Bearer " + OLD_TOKEN)
        self.assertFalse(Path("auth.txt").exists())

    @staticmethod
    def user_response(code=0, data=None, status=200):
        result = Mock(spec=requests.Response)
        result.status_code = status
        result.json.return_value = {"code": code, "data": {"id": 1} if data is None else data}
        return result

    def test_valid_cache_skips_credentials_and_course_auth_probe(self):
        Path("auth.txt").write_text(TOKEN)
        prompt = Mock(side_effect=AssertionError("Cache is valid"))
        with patch("utils.requests.get", return_value=self.user_response()) as get, \
             patch("utils.test_auth", side_effect=AssertionError("Not a course permission check")):
            self.assertTrue(utils.ensure_auth(prompt))
        self.assertEqual(get.call_args.args[0], "https://cbiz.yanhekt.cn/v1/user")
        self.assertEqual(get.call_args.kwargs["headers"]["Authorization"], "Bearer " + TOKEN)
        self.assertGreater(get.call_args.kwargs["timeout"], 0)
        self.assertFalse(get.call_args.kwargs["allow_redirects"])
        prompt.assert_not_called()

    def test_expired_cache_uses_sso_and_replaces_token(self):
        Path("auth.txt").write_text(OLD_TOKEN)
        with patch("utils.requests.get", side_effect=[self.user_response(code=61101113), self.user_response()]):
            self.assertTrue(utils.ensure_auth(lambda: TOKEN))
        self.assertEqual(Path("auth.txt").read_text(), TOKEN)

    def test_uncertain_cache_validation_never_prompts_or_overwrites(self):
        Path("auth.txt").write_text(OLD_TOKEN)
        failures = [self.user_response(code=61101210, data={"error": "course id"}),
                    self.user_response(data=[]), self.user_response(status=503),
                    self.user_response(status=302)]
        for result in failures:
            with self.subTest(status=result.status_code, payload=result.json.return_value), \
                 patch("utils.requests.get", return_value=result), self.assertRaises(sso_login.LoginError):
                utils.ensure_auth(lambda: self.fail("Cannot distinguish an expired session"))
            self.assertEqual(Path("auth.txt").read_text(), OLD_TOKEN)
        with patch("utils.requests.get", side_effect=requests.Timeout("synthetic")), \
             self.assertRaises(sso_login.LoginError):
            utils.ensure_auth(lambda: self.fail("Network failure must not prompt credentials"))

    def test_plain_cli_uses_explicit_sso_and_keeps_course_argument(self):
        with patch("sys.argv", ["main.py", "40524"]), \
             patch("sso_login.prompt_login", return_value=TOKEN) as prompt, \
             patch("utils.get_course_info", return_value=([], "test course", "teacher")) as course, \
             patch("builtins.input", side_effect=["2", "", "1", "1"]) as read, \
             redirect_stdout(io.StringIO()):
            main.main()
        prompt.assert_called_once_with()
        course.assert_called_once_with(courseID="40524")
        self.assertEqual(Path("auth.txt").read_text(), TOKEN)
        self.assertEqual(read.call_count, 4, "Choose SSO, then the original download choices")

    def test_plain_cli_cancel_never_loads_course_or_downloads(self):
        with patch("sys.argv", ["main.py", "40524"]), \
             patch("sso_login.prompt_login", side_effect=KeyboardInterrupt), \
             patch("builtins.input", return_value="2"), \
             patch("utils.get_course_info") as course, redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit) as exit_status:
                main.main()
        self.assertNotEqual(exit_status.exception.code, 0)
        course.assert_not_called()
        self.assertFalse(Path("auth.txt").exists())

    def test_rejected_manual_token_does_not_replace_old_auth(self):
        Path("auth.txt").write_text(OLD_TOKEN)
        self.get.return_value = self.user_response(code=61101113)
        with self.assertRaises(utils.AuthError):
            utils.ensure_auth(lambda: TOKEN)
        self.assertEqual(Path("auth.txt").read_text(), OLD_TOKEN)
        self.assertEqual(utils.headers["Authorization"], "Bearer " + OLD_TOKEN)

    def test_token_format_is_validated_by_yanhe_not_the_sso_parser(self):
        value = "synthetic.future.token-format"
        self.assertTrue(utils.ensure_auth(lambda: value))
        self.assertEqual(Path("auth.txt").read_text(), value)

    def test_candidate_validation_uses_copy_of_headers(self):
        utils.headers["Authorization"] = "Bearer " + OLD_TOKEN
        self.get.side_effect = requests.Timeout("synthetic")
        with self.assertRaises(utils.AuthError):
            utils.ensure_auth(lambda: TOKEN)
        self.assertEqual(utils.headers["Authorization"], "Bearer " + OLD_TOKEN)
        self.assertFalse(Path("auth.txt").exists())

    def test_plain_prompt_reads_hidden_password_and_returns_token_without_saving(self):
        with patch("sys.stdin.isatty", return_value=True), \
             patch("builtins.input", return_value="student") as read, \
             patch("getpass.getpass", return_value="test password") as password, \
             patch("sso_login.login", return_value=TOKEN) as login:
            self.assertEqual(sso_login.prompt_login(), TOKEN)
        self.assertEqual(login.call_args.args, ("student", "test password"))
        read.assert_called_once()
        password.assert_called_once()
        self.assertFalse(Path("auth.txt").exists())

    def test_empty_account_input_is_cancellation_not_failed_sso(self):
        for username, password in (("", "unused"), ("student", "")):
            with self.subTest(empty_username=not username), \
                 patch("sys.stdin.isatty", return_value=True), \
                 patch("builtins.input", return_value=username), \
                 patch("getpass.getpass", return_value=password), \
                 patch("sso_login.login") as login, self.assertRaises(EOFError):
                sso_login.prompt_login()
            login.assert_not_called()


if __name__ == "__main__":
    unittest.main()
