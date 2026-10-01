"""Offline compatibility contracts for the BIT-Login-Python 5d537ca SMS flow."""

import json
import unittest
from unittest.mock import Mock, patch
from urllib.parse import urlencode

import requests

import sso_login


CAS = "https://sso.bit.edu.cn/cas/login"
SMS_API = "https://sso.bit.edu.cn/cas/api/protected/sms/"
PHONE = SMS_API + "getPhoneNumberByUserId"
SEND = SMS_API + "publicNoToken/sendSmsCode"
CHECK = SMS_API + "checkToken"
USERNAME = "original-student"
PASSWORD = "synthetic-password-not-for-logs"  # gitleaks:allow -- synthetic test credential
CODE = "123456"
TOKEN = "0123456789abcdef0123456789abcdef"  # gitleaks:allow -- synthetic fixture
LOGIN_PAGE = '<form><input name="execution" value="initial-flow"></form>'
SMS_PAGE = '''<p id="login-page-flowkey">second-flow</p>
<p id="user-object-id">opaque-user</p>
<p id="second-auth-user-id">page-alias</p>
<form id="secondSmsLoginForm" action="login"></form>'''
GATEWAY_PAGE = '''<p id="login-page-flowkey">second-flow</p>
<p id="user-object-id">opaque-user</p><p id="second-auth-tip">SMS</p>
<script src="/gate/public/cas-gateway/main.js"></script>
<form action="login"></form>'''
STILL_VALID = "短信验证码仍在有效期内，请勿重复发送"


def response(body="", *, status=200, url=CAS, location=None):
    result = requests.Response()
    result.status_code = status
    result.url = url
    result._content = body.encode("utf-8")
    result.encoding = "utf-8"
    if location is not None:
        result.headers["Location"] = location
    return result


class SsoCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.session = Mock(spec=requests.Session)
        self.session.headers = {}
        self.session.__enter__ = Mock(return_value=self.session)
        self.session.__exit__ = Mock(return_value=False)
        factory = patch("sso_login.requests.Session", return_value=self.session)
        factory.start()
        self.addCleanup(factory.stop)
        network = patch("socket.socket.connect", side_effect=AssertionError("Network forbidden"))
        network.start()
        self.addCleanup(network.stop)

    def script(self, *, page=SMS_PAGE, phone=None, phone_status=200,
               password_status=200, password_url=CAS, sent=None,
               final_status=302, final_body=""):
        self.session.reset_mock()
        self.session.headers = {}
        self.read_code = Mock(return_value=CODE)
        if phone is None:
            phone = {"code": 200, "data": {"tel": "opaque-phone"}}
        if sent is None:
            sent = {"code": 200}
        callback = "https://cbiz.yanhekt.cn/v1/cas/callback?ticket=synthetic"
        self.session.get.side_effect = [
            response(LOGIN_PAGE),
            response(status=302, url=callback, location=(
                "https://www.yanhekt.cn/login?" + urlencode({"token": TOKEN})
            )),
        ]
        self.session.post.side_effect = [
            response(page, status=password_status, url=password_url),
            response(json.dumps(phone), status=phone_status, url=PHONE),
            response(json.dumps(sent), url=SEND),
            response('{"code":200}', url=CHECK),
            response(final_body, status=final_status,
                     location=callback if final_status == 302 else None),
        ]

    def complete(self):
        token = sso_login.login(USERNAME, PASSWORD, code_provider=self.read_code)
        self.assertEqual(token, TOKEN)
        self.assertEqual([call.args[0] for call in self.session.post.call_args_list],
                         [CAS, PHONE, SEND, CHECK, CAS])
        self.assertEqual(self.session.get.call_count, 2, "Do not reload the initial page")
        self.read_code.assert_called_once_with()
        final = self.session.post.call_args.kwargs["data"]
        self.assertEqual(final["type"], "smsLogin")
        self.assertEqual(final["password"], CODE)
        self.assertEqual(final["execution"], "second-flow")

    def test_sms_apis_use_gateway_origin_and_referer(self):
        self.script()
        self.complete()
        for call in self.session.post.call_args_list[1:4]:
            with self.subTest(endpoint=call.args[0]):
                headers = requests.structures.CaseInsensitiveDict(self.session.headers)
                headers.update(call.kwargs.get("headers", {}))
                self.assertEqual(headers.get("Origin"), "https://sso.bit.edu.cn")
                self.assertEqual(headers.get("Referer"), "https://sso.bit.edu.cn/cas/")

    def test_final_sms_form_uses_gateway_origin_and_referer(self):
        self.script()
        self.complete()
        headers = requests.structures.CaseInsensitiveDict(self.session.headers)
        headers.update(self.session.post.call_args.kwargs.get("headers", {}))
        self.assertEqual(headers.get("Origin"), "https://sso.bit.edu.cn")
        self.assertEqual(headers.get("Referer"), "https://sso.bit.edu.cn/cas/")

    def test_gateway_without_second_auth_user_id_can_finish_sms(self):
        self.script(page=GATEWAY_PAGE)
        self.complete()
        self.assertEqual(self.session.post.call_args.kwargs["data"]["username"], USERNAME)

    def test_final_username_is_original_account_even_if_page_alias_differs(self):
        self.script()
        self.complete()
        self.assertEqual(self.session.post.call_args.kwargs["data"]["username"], USERNAME)

    def test_empty_lookup_tel_falls_back_to_challenge_phone(self):
        self.script(page=SMS_PAGE + '<p id="phone-number">page-phone</p>',
                    phone={"code": 200, "data": {"tel": ""}})
        self.complete()
        for call in self.session.post.call_args_list[2:4]:
            self.assertEqual(call.kwargs["json"]["phone"], "page-phone")

    def test_phone_lookup_data_without_code_is_accepted(self):
        self.script(phone={"data": {"tel": "lookup-phone"}})
        self.complete()
        for call in self.session.post.call_args_list[2:4]:
            self.assertEqual(call.kwargs["json"]["phone"], "lookup-phone")

    def test_explicit_phone_lookup_failure_blocks_sms_despite_fallback(self):
        for code in (400, 401, 403, 500):
            with self.subTest(code=code):
                self.script(page=SMS_PAGE + '<p id="phone-number">page-phone</p>',
                            phone={"code": code, "data": {"tel": "lookup-phone"}})
                with self.assertRaises(sso_login.LoginError):
                    sso_login.login(USERNAME, PASSWORD, code_provider=self.read_code)
                self.assertEqual([c.args[0] for c in self.session.post.call_args_list],
                                 [CAS, PHONE])
                self.read_code.assert_not_called()

    def test_non_200_phone_lookup_blocks_sms_despite_success_json(self):
        for status in (201, 302, 400, 401, 403, 429, 500):
            with self.subTest(status=status):
                self.script(page=SMS_PAGE + '<p id="phone-number">page-phone</p>',
                            phone_status=status)
                with self.assertRaises(sso_login.LoginError):
                    sso_login.login(USERNAME, PASSWORD, code_provider=self.read_code)
                self.assertEqual([c.args[0] for c in self.session.post.call_args_list],
                                 [CAS, PHONE])
                self.read_code.assert_not_called()

    def test_trusted_password_rejection_status_can_contain_sms_challenge(self):
        for status in (400, 401, 403):
            with self.subTest(status=status):
                self.script(password_status=status)
                self.complete()

    def test_other_response_urls_cannot_enable_http_error_challenge(self):
        urls = (
            "http://sso.bit.edu.cn/cas/login",
            "https://evil.example/cas/login",
            "https://sso.bit.edu.cn.evil.example/cas/login",
            "https://user@sso.bit.edu.cn/cas/login",
            "https://sso.bit.edu.cn:444/cas/login",
            "https://sso.bit.edu.cn/cas/api/protected/sms/checkToken",
            "https://sso.bit.edu.cn/other/cas/login",
            "https://cbiz.yanhekt.cn/cas/login",
        )
        for status in (400, 401, 403):
            for url in urls:
                with self.subTest(status=status, url=url):
                    self.script(password_status=status, password_url=url)
                    with self.assertRaises(sso_login.LoginError):
                        sso_login.login(USERNAME, PASSWORD, code_provider=self.read_code)
                    self.assertEqual(self.session.post.call_count, 1)
                    self.read_code.assert_not_called()

    def test_other_password_http_errors_do_not_parse_sms_challenge(self):
        for status in (404, 429, 500, 503):
            with self.subTest(status=status):
                self.script(password_status=status)
                with self.assertRaises(sso_login.LoginError):
                    sso_login.login(USERNAME, PASSWORD, code_provider=self.read_code)
                self.assertEqual(self.session.post.call_count, 1)
                self.read_code.assert_not_called()

    def test_still_valid_message_in_all_supported_locations_continues_without_resend(self):
        for nested in (False, True):
            for field in ("message", "msg", "errorMessage"):
                with self.subTest(nested=nested, field=field):
                    message = {field: STILL_VALID}
                    sent = {"code": 400, **({"data": message} if nested else message)}
                    self.script(sent=sent)
                    self.complete()  # Exact route sequence also forbids resending SMS.

    def test_still_valid_body_cannot_override_sms_api_http_error(self):
        for endpoint_index, endpoint in ((2, SEND), (3, CHECK)):
            for status in (400, 401, 403):
                with self.subTest(endpoint=endpoint, status=status):
                    self.script()
                    replies = list(self.session.post.side_effect)
                    replies[endpoint_index] = response(json.dumps({
                        "code": 200, "message": STILL_VALID,
                    }), status=status, url=endpoint)
                    self.session.post.side_effect = replies
                    with self.assertRaises(sso_login.LoginError):
                        sso_login.login(USERNAME, PASSWORD, code_provider=self.read_code)
                    self.assertEqual(self.session.post.call_count, endpoint_index + 1)

    def test_final_sms_rejection_is_safe_business_error_without_retry(self):
        for status in (400, 401, 403):
            with self.subTest(status=status):
                body = f'<p id="login-error-msg">{PASSWORD} {CODE} {TOKEN}</p>'
                self.script(final_status=status, final_body=body)
                with self.assertRaises(sso_login.LoginError) as caught:
                    sso_login.login(USERNAME, PASSWORD, code_provider=self.read_code)
                message = str(caught.exception)
                self.assertRegex(message, r"(?i)sms|短信|验证码")
                for secret in (PASSWORD, CODE, TOKEN):
                    self.assertNotIn(secret, message)
                self.assertEqual([c.args[0] for c in self.session.post.call_args_list],
                                 [CAS, PHONE, SEND, CHECK, CAS])


if __name__ == "__main__":
    unittest.main()
