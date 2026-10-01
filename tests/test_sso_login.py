import base64
import contextlib
import io
import json
import unittest
from unittest.mock import Mock, patch
from urllib.parse import urlencode

import requests
from Crypto.Cipher import AES
from Crypto.Util.Padding import unpad

import sso_login


TOKEN = "0123456789abcdef0123456789abcdef"  # gitleaks:allow -- synthetic test token
LOGIN_PAGE = """<form method="post"><input value="flow-1" name="execution"
type="hidden"><input name="username"><input name="password"></form>"""
SMS_PAGE = """<p id="login-page-flowkey">flow-2</p>
<p id="user-object-id">opaque-user</p><p id="second-auth-user-id">verified-user</p>
<form id="secondSmsLoginForm" action="login"></form>"""


def callback_url(host="www.yanhekt.cn", scheme="https", field="token"):
    return f"{scheme}://{host}/login?" + urlencode({field: TOKEN})


def response(status=200, body="", location=None):
    result = requests.Response()
    result.status_code = status
    result._content = body.encode()
    result.encoding = "utf-8"
    result.url = "https://sso.bit.edu.cn/cas/login"
    if location is not None:
        result.headers["Location"] = location
    return result


class LoginTests(unittest.TestCase):
    def setUp(self):
        self.session = Mock(spec=requests.Session)
        self.session.headers = {}
        self.session.__enter__ = Mock(return_value=self.session)
        self.session.__exit__ = Mock(return_value=False)
        self.factory = patch("sso_login.requests.Session", return_value=self.session)
        self.factory.start()
        self.addCleanup(self.factory.stop)

    def success(self, page=LOGIN_PAGE):
        self.session.get.side_effect = [
            response(body=page),
            response(302, location=callback_url()),
        ]
        self.session.post.return_value = response(
            302, location="https://cbiz.yanhekt.cn/v1/cas/callback?ticket=example"
        )

    def test_password_login_returns_callback_token_without_logging_secrets(self):
        self.success()
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            self.assertEqual(sso_login.login("student", " secret "), TOKEN)
        self.assertEqual(output.getvalue(), "")
        form = self.session.post.call_args.kwargs["data"]
        self.assertEqual(form["username"], "student")
        self.assertEqual(form["password"], " secret ")
        self.assertEqual(form["execution"], "flow-1")
        self.assertEqual(form["type"], "UsernamePassword")
        self.assertFalse(self.session.post.call_args.kwargs["allow_redirects"])

    def test_server_rendered_flowkey_and_html_entities(self):
        self.success("<p id='login-page-flowkey'>flow&amp;2</p>")
        self.assertEqual(sso_login.login("student", "password"), TOKEN)
        self.assertEqual(self.session.post.call_args.kwargs["data"]["execution"], "flow&2")

    def test_current_page_encrypts_password_and_empty_captcha_payload(self):
        key = "AAECAwQFBgcICQoLDA0ODw=="  # gitleaks:allow -- bytes 00..0f test vector
        self.success(LOGIN_PAGE + f'<p id="login-croypto">{key}</p>')
        self.assertEqual(sso_login.login("student", " secret "), TOKEN)
        form = self.session.post.call_args.kwargs["data"]
        cipher = AES.new(bytes(range(16)), AES.MODE_ECB)
        self.assertEqual(unpad(cipher.decrypt(base64.b64decode(form["password"])), 16), b" secret ")
        self.assertEqual(unpad(cipher.decrypt(base64.b64decode(form["captcha_payload"])), 16), b"{}")
        self.assertEqual(form["croypto"], key)

    def test_invalid_crypto_config_does_not_submit_plaintext_password(self):
        self.session.get.return_value = response(body=LOGIN_PAGE + '<p id="login-croypto">invalid</p>')
        with self.assertRaises(sso_login.LoginError):
            sso_login.login("student", "password")
        self.session.post.assert_not_called()

    def test_risk_information_unavailable_uses_official_failure_payload(self):
        self.success(LOGIN_PAGE + '''<p id="login-croypto">AAECAwQFBgcICQoLDA0ODw==</p>
            <p id="riskSystemSwitch">USTC</p><p id="targetSystem">sso</p><p id="siteId">sourceId</p>''')
        self.assertEqual(sso_login.login("student", "password"), TOKEN)
        form = self.session.post.call_args.kwargs["data"]
        cipher = AES.new(bytes(range(16)), AES.MODE_ECB)
        self.assertEqual(unpad(cipher.decrypt(base64.b64decode(form["risk_payload"])), 16), b'{"error":true}')
        self.assertEqual((form["riskEngine"], form["targetSystem"], form["siteId"]),
                         ("true", "sso", "sourceId"))

    def test_sms_api_redirect_is_not_followed(self):
        self.sms()
        self.session.post.side_effect = [response(body=SMS_PAGE),
                                        response(302, location="https://evil.example/")]
        with self.assertRaises(sso_login.LoginError):
            sso_login.login("student", "password", sms_code="123456")
        self.assertEqual(self.session.get.call_count, 1)
        self.assertFalse(self.session.post.call_args.kwargs["allow_redirects"])

    def test_non_sms_second_factor_does_not_send_sms(self):
        self.session.get.return_value = response(body=LOGIN_PAGE)
        self.session.post.return_value = response(body=SMS_PAGE.replace(
            'id="secondSmsLoginForm"', 'id="secondEmailLoginForm"'))
        with self.assertRaises(sso_login.LoginError):
            sso_login.login("student", "password", code_provider=lambda: self.fail("wrong factor"))
        self.assertEqual(self.session.post.call_count, 1)

    def test_other_form_cannot_replace_sms_action_or_execution(self):
        self.sms()
        extra_form = '<form action="/wrong"><input name="execution" value="wrong"></form>'
        replies = list(self.session.post.side_effect)
        replies[0] = response(body=SMS_PAGE + extra_form)
        self.session.post.side_effect = replies
        self.assertEqual(sso_login.login("student", "password", code_provider=lambda: "123456"), TOKEN)
        call = self.session.post.call_args
        self.assertEqual(call.args[0], "https://sso.bit.edu.cn/cas/login")
        self.assertEqual(call.kwargs["data"]["execution"], "flow-2")

    def test_duplicate_sms_forms_are_rejected_before_sending(self):
        self.session.get.return_value = response(body=LOGIN_PAGE)
        self.session.post.return_value = response(body=SMS_PAGE + '<form id="secondSmsLoginForm"></form>')
        with self.assertRaises(sso_login.LoginError):
            sso_login.login("student", "password", code_provider=lambda: self.fail("ambiguous form"))
        self.assertEqual(self.session.post.call_count, 1)

    def test_password_error_does_not_retry_or_request_sms(self):
        self.session.get.return_value = response(body=LOGIN_PAGE)
        self.session.post.return_value = response(
            body=LOGIN_PAGE + '<span id="showErrorTip">用户名或密码错误</span>'
        )
        with self.assertRaises(sso_login.LoginError):
            sso_login.login("student", "password", code_provider=lambda: self.fail("SMS requested"))
        self.assertEqual(self.session.post.call_count, 1)

    def test_unsupported_challenge_stops_without_browser_or_retry(self):
        self.session.get.return_value = response(body=LOGIN_PAGE)
        self.session.post.return_value = response(
            body=LOGIN_PAGE + '<p id="netEaseCaptchaId">slider-id</p>'
        )
        with self.assertRaises(sso_login.LoginError):
            sso_login.login("student", "password")
        self.assertEqual(self.session.post.call_count, 1)

    def test_network_error_is_redacted(self):
        self.session.get.side_effect = requests.ConnectionError("secret URL?token=" + TOKEN)
        with self.assertRaises(sso_login.LoginError) as error:
            sso_login.login("student", "password")
        self.assertNotIn(TOKEN, str(error.exception))

    def test_missing_flowkey_is_a_clear_failure(self):
        self.session.get.return_value = response(body="<html>maintenance</html>")
        with self.assertRaises(sso_login.LoginError):
            sso_login.login("student", "password")
        self.session.post.assert_not_called()

    def test_relative_cas_redirect_is_followed_in_same_session(self):
        self.session.get.side_effect = [
            response(302, location="/cas/login?step=1"),
            response(body=LOGIN_PAGE),
            response(302, location=callback_url()),
        ]
        self.session.post.return_value = response(
            302, location="https://cbiz.yanhekt.cn/v1/cas/callback?ticket=example"
        )
        self.assertEqual(sso_login.login("student", "password"), TOKEN)
        self.assertEqual(self.session.get.call_args_list[1].args[0],
                         "https://sso.bit.edu.cn/cas/login?step=1")

    def test_untrusted_redirects_are_not_requested(self):
        for location in (
            callback_url(host="evil.example"),
            callback_url(scheme="http"),
            callback_url(host="www.yanhekt.cn.evil.example"),
            callback_url(host="user@www.yanhekt.cn"),
        ):
            with self.subTest(location=location):
                self.session.get.reset_mock()
                self.session.get.side_effect = None
                self.session.get.return_value = response(302, location=location)
                with self.assertRaises(sso_login.LoginError):
                    sso_login.login("student", "password")
                self.assertEqual(self.session.get.call_count, 1)

    def test_hex_in_non_token_query_is_not_accepted(self):
        self.session.get.return_value = response(
            302, location=callback_url(field="session")
        )
        with self.assertRaises(sso_login.LoginError):
            sso_login.login("student", "password")

    def test_redirect_loops_are_bounded(self):
        self.session.get.return_value = response(302, location="/cas/login")
        with self.assertRaises(sso_login.LoginError):
            sso_login.login("student", "password")
        self.assertLessEqual(self.session.get.call_count, 10)

    def test_http_failure_is_not_treated_as_login_challenge(self):
        self.session.get.return_value = response(503, "unavailable")
        with self.assertRaises(sso_login.LoginError):
            sso_login.login("student", "password")
        self.session.post.assert_not_called()

    def sms(self, send_code=200, check_code=200, *, provided=False):
        self.session.get.side_effect = [
            response(body=LOGIN_PAGE),
            response(302, location=callback_url()),
        ]
        replies = [
            response(body=SMS_PAGE),
            response(body=json.dumps({"code": 200, "data": {"tel": "opaque-phone", "maskTel": "138****0000"}})),
            response(body=json.dumps({"code": send_code})),
            response(body=json.dumps({"code": check_code})),
            response(302, location="https://cbiz.yanhekt.cn/v1/cas/callback?ticket=example"),
        ]
        if provided:
            del replies[2]
        self.session.post.side_effect = replies

    def test_sms_keeps_session_and_uses_challenge_execution_and_password_field(self):
        self.sms()

        def code_provider():
            self.assertEqual(self.session.post.call_count, 3, "send SMS before prompting")
            return "123456"

        self.assertEqual(sso_login.login("student", "password", code_provider=code_provider), TOKEN)
        calls = self.session.post.call_args_list
        self.assertEqual(calls[2].kwargs["json"], {"phone": "opaque-phone", "businessNo": "0008"})
        self.assertEqual(calls[3].kwargs["json"], {
            "phone": "opaque-phone", "token": "123456", "delete": False, "trustDevice": False,
        })
        form = calls[-1].kwargs["data"]
        self.assertEqual(form["execution"], "flow-2")
        self.assertEqual(form["username"], "student")
        self.assertEqual(form["password"], "123456")
        self.assertEqual(form["captcha_code"], "")
        self.assertEqual(form["type"], "smsLogin")
        self.assertEqual(self.session.get.call_count, 2, "must not reload the login page")
        self.assertIn("Csrf-Key", calls[1].kwargs["headers"])
        self.assertEqual(calls[1].kwargs["headers"]["hasCrypto"], "true")
        for call in calls[1:]:
            self.assertEqual(call.kwargs["headers"]["Origin"], "https://sso.bit.edu.cn")
            self.assertEqual(call.kwargs["headers"]["Referer"], "https://sso.bit.edu.cn/cas/")

    def test_explicit_sms_code_skips_prompt(self):
        self.sms(provided=True)
        self.assertEqual(sso_login.login("student", "password", sms_code="123456",
                                        code_provider=lambda: self.fail("unexpected prompt")), TOKEN)
        self.assertEqual(self.session.post.call_count, 4, "provided code must not trigger a new SMS")

    def test_missing_code_source_does_not_send_sms(self):
        self.sms()
        with self.assertRaises(sso_login.LoginError):
            sso_login.login("student", "password")
        self.assertEqual(self.session.post.call_count, 1)

    def test_bad_sms_code_is_rejected_locally_before_check(self):
        for code in ("", "123", "１２３４５６", "abcdef"):
            with self.subTest(code=code):
                self.session.post.reset_mock()
                self.sms()
                with self.assertRaises(sso_login.LoginError):
                    sso_login.login("student", "password", code_provider=lambda: code)
                self.assertEqual(self.session.post.call_count, 3)

    def test_failed_sms_send_stops_before_prompt(self):
        self.sms(send_code=500)
        with self.assertRaises(sso_login.LoginError):
            sso_login.login("student", "password", code_provider=lambda: self.fail("send failed"))
        self.assertEqual(self.session.post.call_count, 3)

    def test_rejected_sms_does_not_retry_password_or_resend(self):
        self.sms(check_code=400, provided=True)
        with self.assertRaises(sso_login.LoginError):
            sso_login.login("student", "password", sms_code="123456")
        self.assertEqual(self.session.post.call_count, 3)

    def test_sms_network_error_does_not_leak_phone_or_code(self):
        self.sms()
        self.session.post.side_effect = [response(body=SMS_PAGE), requests.Timeout("opaque-phone 123456")]
        with self.assertRaises(sso_login.LoginError) as error:
            sso_login.login("student", "password", sms_code="123456")
        self.assertNotIn("opaque-phone", str(error.exception))
        self.assertNotIn("123456", str(error.exception))


if __name__ == "__main__":
    unittest.main()
