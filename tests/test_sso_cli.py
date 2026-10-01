"""CLI contracts; login is replaced, but token storage uses real temporary files."""

import getpass
import io
import os
from pathlib import Path
import stat
import tempfile
import unittest
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from unittest.mock import patch

import sso_login
import utils


USERNAME = "test-student"
PASSWORD = "test-password-do-not-print"  # gitleaks:allow -- synthetic test credential
SMS_CODE = "738291"
TOKEN = "0123456789abcdef0123456789abcdef"  # gitleaks:allow -- synthetic test token
OLD_TOKEN = "fedcba9876543210fedcba9876543210"  # gitleaks:allow -- synthetic test token


class SsoCliTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.directory = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        previous_cwd = Path.cwd()
        os.chdir(self.directory)
        self.stack.callback(os.chdir, previous_cwd)
        self.stack.enter_context(patch.dict(os.environ, {}, clear=True))
        self.stack.enter_context(patch.dict(utils.headers, utils.headers.copy()))
        self.stack.enter_context(
            patch("socket.socket.connect", side_effect=AssertionError("Network forbidden"))
        )
        self.auth_file = self.directory / "auth.txt"

    def invoke(self, argv=(), *, login=None, env=None, tty=False,
               password=PASSWORD, sms=SMS_CODE):
        stdout, stderr = io.StringIO(), io.StringIO()
        if login is None:
            def login(username, supplied_password, *, sms_code=None,
                      code_provider=None, timeout=15):
                return TOKEN

        with ExitStack() as stack:
            stack.enter_context(patch.dict(os.environ, env or {}, clear=True))
            stack.enter_context(patch("sys.stdin.isatty", return_value=tty))
            stack.enter_context(patch.object(sso_login, "login", side_effect=login))
            password_options = (
                {"side_effect": password} if isinstance(password, BaseException)
                else {"return_value": password}
            )
            stack.enter_context(patch.object(getpass, "getpass", **password_options))
            # Support both `import getpass` and `from getpass import getpass`.
            if callable(getattr(sso_login, "getpass", None)):
                stack.enter_context(patch.object(sso_login, "getpass", **password_options))
            sms_options = (
                {"side_effect": sms} if isinstance(sms, BaseException)
                else {"return_value": sms}
            )
            stack.enter_context(patch("builtins.input", **sms_options))
            stack.enter_context(redirect_stdout(stdout))
            stack.enter_context(redirect_stderr(stderr))
            # Valid invocations must return an int, rather than exit the process.
            result = sso_login.main(list(argv))
        self.assertIsInstance(result, int)
        output = stdout.getvalue() + stderr.getvalue()
        for secret in (PASSWORD, SMS_CODE, TOKEN, OLD_TOKEN):
            self.assertNotIn(secret, output)
        return result

    def credentials(self, **extra):
        return {"STUDENT_ID": USERNAME, "PASSWORD": PASSWORD, **extra}

    def test_environment_login_saves_default_file_readable_by_upstream(self):
        def login(username, password, *, sms_code=None, code_provider=None, timeout=15):
            self.assertEqual((username, password, timeout), (USERNAME, PASSWORD, 15))
            self.assertIsNone(sms_code)
            return TOKEN

        self.assertEqual(self.invoke(
            login=login, env=self.credentials(),
            password=AssertionError("Environment password must avoid getpass"),
        ), 0)
        self.assertEqual(utils.read_auth(), TOKEN)
        self.assertEqual(utils.headers["Authorization"], "Bearer " + TOKEN)
        if os.name == "posix":
            self.assertEqual(stat.S_IMODE(self.auth_file.stat().st_mode), 0o600)

    def test_cli_username_and_sms_override_environment_and_custom_path(self):
        target = self.directory / "selected-token.txt"

        def login(username, password, *, sms_code=None, code_provider=None, timeout=15):
            self.assertEqual(username, "cli-student")
            self.assertEqual(password, PASSWORD)
            self.assertEqual(sms_code, SMS_CODE)
            self.assertEqual(timeout, 2.5)
            return TOKEN

        result = self.invoke(
            ["--username", "cli-student", "--sms-code", SMS_CODE,
             "--auth-file", str(target), "--timeout", "2.5"],
            login=login, env=self.credentials(SMS_CODE="111111"),
        )
        self.assertEqual(result, 0)
        self.assertEqual(target.read_text().strip(), TOKEN)
        self.assertFalse(self.auth_file.exists())

    def test_sms_environment_is_forwarded_without_prompting(self):
        def login(username, password, *, sms_code=None, code_provider=None, timeout=15):
            self.assertEqual(sms_code, SMS_CODE)
            return TOKEN

        self.assertEqual(self.invoke(
            login=login, env=self.credentials(SMS_CODE=SMS_CODE),
            sms=AssertionError("Unexpected SMS prompt"),
        ), 0)

    def test_interactive_password_uses_getpass(self):
        def login(username, password, *, sms_code=None, code_provider=None, timeout=15):
            self.assertEqual((username, password), (USERNAME, PASSWORD))
            return TOKEN

        self.assertEqual(self.invoke(
            ["--username", USERNAME], login=login, tty=True,
            sms=AssertionError("Password must not use input"),
        ), 0)

    def test_sms_prompt_runs_inside_one_login_call(self):
        calls = []

        def login(username, password, *, sms_code=None, code_provider=None, timeout=15):
            calls.append((username, password))
            self.assertIsNone(sms_code)
            self.assertTrue(callable(code_provider))
            self.assertEqual(code_provider(), SMS_CODE)
            return TOKEN

        self.assertEqual(self.invoke(login=login, env=self.credentials(), tty=True), 0)
        self.assertEqual(calls, [(USERNAME, PASSWORD)])
        self.assertEqual(self.auth_file.read_text().strip(), TOKEN)

    def test_missing_credentials_in_non_tty_never_prompt_or_login(self):
        for env in ({}, {"STUDENT_ID": USERNAME}, {"PASSWORD": PASSWORD}):
            with self.subTest(env_keys=tuple(env)):
                def forbidden(*args, **kwargs):
                    self.fail("Incomplete credentials must not start login")

                self.assertNotEqual(self.invoke(
                    login=forbidden, env=env,
                    password=AssertionError("Non-TTY password prompt"),
                    sms=AssertionError("Non-TTY input prompt"),
                ), 0)
        self.assertFalse(self.auth_file.exists())

    def test_dotenv_is_not_loaded_implicitly(self):
        (self.directory / ".env").write_text(
            f"STUDENT_ID={USERNAME}\nPASSWORD={PASSWORD}\n", encoding="utf-8"
        )

        def forbidden(*args, **kwargs):
            self.fail("Local .env must not supply credentials")

        self.assertNotEqual(self.invoke(
            login=forbidden, password=AssertionError("Unexpected prompt"),
            sms=AssertionError("Unexpected prompt"),
        ), 0)
        self.assertFalse(self.auth_file.exists())

    def test_sms_challenge_in_non_tty_never_waits_for_input(self):
        def login(username, password, *, sms_code=None, code_provider=None, timeout=15):
            if code_provider is None:
                raise sso_login.LoginError("SMS required")
            code_provider()
            raise sso_login.LoginError("SMS unavailable")

        self.assertNotEqual(self.invoke(
            login=login, env=self.credentials(),
            sms=AssertionError("Non-TTY SMS prompt"),
        ), 0)
        self.assertFalse(self.auth_file.exists())

    def test_safe_login_failure_preserves_existing_token(self):
        self.auth_file.write_text(OLD_TOKEN)

        def login(*args, **kwargs):
            # LoginError messages are sanitized by the protocol layer.
            raise sso_login.LoginError("Authentication failed")

        self.assertNotEqual(self.invoke(login=login, env=self.credentials()), 0)
        self.assertEqual(self.auth_file.read_text(), OLD_TOKEN)

    def test_password_cancel_or_eof_returns_nonzero(self):
        for interruption in (KeyboardInterrupt(), EOFError()):
            with self.subTest(interruption=type(interruption).__name__):
                def forbidden(*args, **kwargs):
                    self.fail("Canceled password entry must not start login")

                self.assertNotEqual(self.invoke(
                    ["--username", USERNAME], login=forbidden,
                    tty=True, password=interruption,
                ), 0)
        self.assertFalse(self.auth_file.exists())

    def test_sms_cancel_or_eof_preserves_existing_token(self):
        self.auth_file.write_text(OLD_TOKEN)
        for interruption in (KeyboardInterrupt(), EOFError()):
            with self.subTest(interruption=type(interruption).__name__):
                def login(username, password, *, sms_code=None,
                          code_provider=None, timeout=15):
                    code_provider()
                    self.fail("Canceled SMS entry must not complete login")

                self.assertNotEqual(self.invoke(
                    login=login, env=self.credentials(), tty=True, sms=interruption,
                ), 0)
                self.assertEqual(self.auth_file.read_text(), OLD_TOKEN)

    def test_invalid_arguments_are_rejected_before_login(self):
        cases = [["--timeout", value] for value in
                 ("0", "-1", "nan", "inf", "invalid")]
        cases += [["--timeout"], ["--password", "not-supported"], ["--unknown"]]
        for argv in cases:
            with self.subTest(argv=argv):
                def forbidden(*args, **kwargs):
                    self.fail("Invalid arguments must not start login")

                # argparse may raise SystemExit for usage errors.
                try:
                    result = self.invoke(argv, login=forbidden, env=self.credentials())
                except SystemExit as error:
                    result = error.code
                self.assertIsInstance(result, int)
                self.assertNotEqual(result, 0)
                self.assertFalse(self.auth_file.exists())

    def test_save_token_replaces_file_without_mutating_old_inode(self):
        self.auth_file.write_text(OLD_TOKEN)
        self.auth_file.chmod(0o644)
        old_link = self.directory / "old-inode"
        os.link(self.auth_file, old_link)

        sso_login._save_token(TOKEN, self.auth_file)

        self.assertEqual(self.auth_file.read_text().strip(), TOKEN)
        self.assertEqual(old_link.read_text(), OLD_TOKEN)
        if os.name == "posix":
            self.assertEqual(stat.S_IMODE(self.auth_file.stat().st_mode), 0o600)

    @unittest.skipUnless(os.name == "posix" and os.geteuid() != 0,
                         "Requires POSIX permissions enforced for a non-root user")
    def test_real_save_failure_preserves_old_file(self):
        self.auth_file.write_text(OLD_TOKEN)
        self.auth_file.chmod(0o600)
        self.directory.chmod(0o500)
        try:
            with self.assertRaises(OSError):
                sso_login._save_token(TOKEN, self.auth_file)
            self.assertEqual(self.auth_file.read_text(), OLD_TOKEN)
        finally:
            self.directory.chmod(0o700)

    @unittest.skipUnless(os.name == "posix" and os.geteuid() != 0,
                         "Requires POSIX permissions enforced for a non-root user")
    def test_cli_save_failure_returns_nonzero_and_preserves_old_file(self):
        self.auth_file.write_text(OLD_TOKEN)
        self.directory.chmod(0o500)
        try:
            self.assertNotEqual(self.invoke(env=self.credentials()), 0)
            self.assertEqual(self.auth_file.read_text(), OLD_TOKEN)
        finally:
            self.directory.chmod(0o700)


if __name__ == "__main__":
    unittest.main()
