"""Log in to legacy Yanhe Classroom via BIT CAS, without a browser.

SMS compatibility reference: BIT101-dev/BIT-Login-Python, commit 5d537ca,
bit_login/sso/client.py (_complete_password_sms_factor and _request).
"""

import argparse
import getpass
import math
import os
import re
import sys
from html.parser import HTMLParser
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit

import requests
from utils import AuthError as LoginError, save_auth as _save_token


CAS_URL = "https://sso.bit.edu.cn/cas/login"
LOGIN_URL = CAS_URL + "?" + urlencode({"service": "https://cbiz.yanhekt.cn/v1/cas/callback"})
YANHE_HOSTS = {"www.yanhekt.cn", "yanhekt.cn"}
LOGIN_HOSTS = YANHE_HOSTS | {"sso.bit.edu.cn", "cbiz.yanhekt.cn"}
SMS_HEADERS = {"Origin": "https://sso.bit.edu.cn", "Referer": "https://sso.bit.edu.cn/cas/"}


class _LoginPage(HTMLParser):
    def __init__(self, html):
        super().__init__(convert_charrefs=True)
        self.text = {}
        self.forms = []
        self._active_form = None
        self._gateway = "cas-gateway" in html
        self._elements = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "form":
            self._active_form = {"id": attrs.get("id"), "action": attrs.get("action") or "login", "fields": {}}
            self.forms.append(self._active_form)
        if tag == "input" and attrs.get("name") and self._active_form is not None:
            self._active_form["fields"][attrs["name"]] = attrs.get("value", "")
        if tag not in {"input", "img", "br", "hr", "meta", "link"}:
            self._elements.append((tag, attrs.get("id")))

    def handle_endtag(self, tag):
        if tag == "form":
            self._active_form = None
        for index in range(len(self._elements) - 1, -1, -1):
            if self._elements[index][0] == tag:
                del self._elements[index:]
                break

    def handle_data(self, data):
        for _, element_id in self._elements:
            if element_id:
                self.text[element_id] = self.text.get(element_id, "") + data

    @property
    def is_sms(self):
        if any(form["id"] == "secondSmsLoginForm" for form in self.forms):
            return True
        # The server's gateway template precedes Angular rendering its form.
        return (self._gateway or "second-auth-tip" in self.text) and not any(
            form["id"] for form in self.forms
        )

    @property
    def form(self):
        wanted = "secondSmsLoginForm" if self.is_sms else "normalLoginForm"
        matches = [form for form in self.forms if form["id"] == wanted]
        if len(matches) == 1:
            return matches[0]
        if matches or len(self.forms) > 1:
            raise LoginError("登录页包含多个候选表单，无法确定当前登录会话。")
        return self.forms[0] if self.forms else {"action": "login", "fields": {}}

    @property
    def action(self):
        return self.form["action"]

    @property
    def execution(self):
        fields = self.form["fields"]
        return self.text.get("login-page-flowkey", "").strip() or fields.get("execution", "")


def _checked_url(url):
    parsed = urlsplit(url)
    if (parsed.scheme != "https" or parsed.hostname not in LOGIN_HOSTS
            or parsed.username is not None or parsed.password is not None
            or parsed.port not in (None, 443)):
        raise LoginError("登录重定向到了非预期地址，已停止。")
    return parsed


def _follow_redirects(session, response, timeout, *, allow_login_errors=False):
    for _ in range(8):
        if response.status_code not in (301, 302, 303):
            if response.status_code != 200:
                # CAS can return a challenge/error page with a rejection status.
                # This exception never applies to other hosts, paths or SMS APIs.
                if allow_login_errors and response.status_code in (400, 401, 403):
                    parsed = _checked_url(response.url)
                    if parsed.hostname == "sso.bit.edu.cn" and parsed.path.rstrip("/") == "/cas/login":
                        return response
                raise LoginError(f"登录服务返回 HTTP {response.status_code}，请稍后重试。")
            return response
        location = response.headers.get("Location")
        if not location:
            raise LoginError("登录服务返回了缺少目标地址的重定向。")
        target = urljoin(response.url, location)
        parsed = _checked_url(target)
        if parsed.hostname in YANHE_HOSTS:
            tokens = parse_qs(parsed.query).get("token", [])
            if (parsed.path.rstrip("/") != "/login" or len(tokens) != 1
                    or not re.fullmatch(r"[0-9a-fA-F]{32}", tokens[0])):
                raise LoginError("延河回调中没有有效的身份认证码。")
            return tokens[0]
        response = session.get(target, allow_redirects=False, timeout=timeout)
    raise LoginError("登录重定向次数过多，已停止。")


def _login_error(response, default):
    page = _LoginPage(response.text)
    code = page.text.get("login-error-code", "").strip() or page.text.get("login-error-msg", "").strip()
    # Translate known codes, never print the response's potentially private text.
    return {
        "1030027": "用户名或密码错误。", "1030031": "用户名或密码错误。",
        "1030028": "账号已被锁定，请在学校官网处理。",
        "1320007": "短信验证码错误或已失效，请重新登录。",
        "1320010": "学校要求图形验证码，请使用手动认证码方式。",
        "1330001": "学校风控拒绝登录，请在官网完成验证。",
        "1410040": "账号状态无效，请在学校官网处理。",
        "1410041": "账号状态无效，请在学校官网处理。",
        "3910001": "账号已休眠，请先在学校官网激活。",
    }.get(code, default)


def _response_message(result):
    for source in (result, result.get("data")):
        if isinstance(source, dict):
            for key in ("message", "msg", "errorMessage"):
                if source.get(key):
                    return str(source[key])
    return ""


def _sms_api(session, method, payload, timeout, *, encrypted=False):
    from sso_crypto import decrypt_sms_response, encrypt_sms_body, protected_csrf_headers

    if encrypted:
        body, headers, key = encrypt_sms_body(payload)
        kwargs = {"data": body}
    else:
        headers = protected_csrf_headers()
        kwargs = {"json": payload}
    headers.update({**SMS_HEADERS,
                    "Accept": "application/json", "Sid-Language": "zh_CN"})
    response = session.post(
        "https://sso.bit.edu.cn/cas/api/protected/sms/" + method,
        headers=headers, allow_redirects=False, timeout=timeout, **kwargs,
    )
    if response.status_code != 200:
        raise LoginError("短信服务暂不可用；未重试发送，请稍后重新登录。")
    result = decrypt_sms_response(response.text, key) if encrypted else response.json()
    if not isinstance(result, dict):
        raise LoginError("短信服务返回了无法识别的数据。")
    return result


def _complete_sms(session, response, page, username, sms_code, code_provider, timeout):
    if sms_code is None and code_provider is None:
        raise LoginError("登录需要短信验证码；请在交互终端运行或提供验证码读取函数。")
    target = urljoin(response.url, page.action)
    if _checked_url(target).hostname != "sso.bit.edu.cn":
        raise LoginError("短信验证表单的目标地址不正确。")
    phone_result = _sms_api(session, "getPhoneNumberByUserId",
                            {"userId": page.text["user-object-id"].strip()}, timeout, encrypted=True)
    if phone_result.get("code") not in (None, 200):
        raise LoginError("获取绑定手机信息被拒绝；未发送短信。")
    phone_data = phone_result.get("data")
    phone = phone_data.get("tel") if isinstance(phone_data, dict) else None
    if not isinstance(phone, str) or not phone.strip():
        phone = page.text.get("phone-number", "").strip()
    if not phone:
        raise LoginError("无法获取本次短信验证所需的绑定手机信息。")
    if sms_code is None:
        sent = _sms_api(session, "publicNoToken/sendSmsCode",
                        {"phone": phone, "businessNo": "0008"}, timeout)
        message = _response_message(sent)
        still_valid = all(word in message for word in ("验证码", "有效期内", "重复发送"))
        if sent.get("code") != 200 and not still_valid:
            raise LoginError("短信发送失败或被限流；未重试发送，请稍后重新登录。")
    code = sms_code if sms_code is not None else code_provider()
    if not isinstance(code, str) or not re.fullmatch(r"[0-9]{6}", code.strip()):
        raise LoginError("短信验证码必须是六位数字；未提交验证码。")
    code = code.strip()
    checked = _sms_api(session, "checkToken", {
        "phone": phone, "token": code, "delete": False, "trustDevice": False,
    }, timeout)
    if checked.get("code") != 200:
        raise LoginError("短信验证码错误或已过期；未重试提交。")
    form = {
        "username": username, "password": code,
        "type": "smsLogin", "_eventId": "submit", "execution": page.execution,
        "geolocation": "", "captcha_code": "", "trustDevice": "false",
    }
    result = _follow_redirects(session, session.post(
        target, data=form, headers=dict(SMS_HEADERS), allow_redirects=False, timeout=timeout
    ), timeout, allow_login_errors=True)
    if not isinstance(result, str):
        raise LoginError(_login_error(result, "短信验证后登录仍未完成；原认证文件未修改。"))
    return result


def login(username, password, *, sms_code=None, code_provider=None, timeout=15):
    """Return a Yanhe token; keep any verification in this one HTTP session."""
    if not username or not password:
        raise LoginError("学号和密码不能为空。")
    if not math.isfinite(timeout) or timeout <= 0:
        raise LoginError("请求超时必须是大于零的有限秒数。")
    try:
        with requests.Session() as session:
            session.headers.update({"User-Agent": "Mozilla/5.0", "Accept": "text/html"})
            result = _follow_redirects(
                session, session.get(LOGIN_URL, allow_redirects=False, timeout=timeout), timeout
            )
            if isinstance(result, str):
                return result
            page = _LoginPage(result.text)
            if not page.execution:
                raise LoginError("登录页缺少会话字段；学校登录流程可能已更新。")
            form = {
                "username": username, "password": password, "type": "UsernamePassword",
                "_eventId": "submit", "execution": page.execution,
                "geolocation": "", "captcha_code": "",
            }
            crypto_key = page.text.get("login-croypto", "").strip()
            if crypto_key:
                from sso_crypto import encrypt_password

                form.update({"croypto": crypto_key,
                             "password": encrypt_password(password, crypto_key),
                             "captcha_payload": encrypt_password("{}", crypto_key)})
                if page.text.get("riskSystemSwitch", "").strip().upper() == "USTC":
                    # Match the official frontend's fingerprint-unavailable branch.
                    # The server still decides whether to require SMS or refuse login.
                    form.update({
                        "risk_payload": encrypt_password('{"error":true}', crypto_key),
                        "riskEngine": "true",
                        "targetSystem": page.text.get("targetSystem", "").strip(),
                        "siteId": page.text.get("siteId", "").strip(),
                    })
            result = _follow_redirects(session, session.post(
                CAS_URL, data=form, headers={"Origin": "https://sso.bit.edu.cn", "Referer": LOGIN_URL},
                allow_redirects=False, timeout=timeout
            ), timeout, allow_login_errors=True)
            if isinstance(result, str):
                return result
            challenge = _LoginPage(result.text)
            if challenge.is_sms and challenge.execution and challenge.text.get("user-object-id", "").strip():
                return _complete_sms(session, result, challenge, username, sms_code, code_provider, timeout)
            raise LoginError(_login_error(result, "登录未完成，请检查账号密码或在官网完成所需验证后手动提供认证码。"))
    except requests.RequestException:
        raise LoginError("无法连接登录服务或请求超时，请检查网络后重试。") from None
    except ValueError:
        raise LoginError("登录服务的数据或加密配置无法识别；原认证文件未修改。") from None


def _timeout(value):
    try:
        seconds = float(value)
        if math.isfinite(seconds) and seconds > 0:
            return seconds
    except ValueError:
        pass
    raise argparse.ArgumentTypeError("必须为大于零的有限秒数")


def prompt_login(username=None, sms_code=None, *, timeout=15):
    """Read terminal credentials and return a token without saving it."""
    username = username or os.environ.get("STUDENT_ID", "")
    password = os.environ.get("PASSWORD", "")
    if not username or not password:
        if not sys.stdin.isatty():
            raise LoginError("非交互运行请设置 STUDENT_ID 和 PASSWORD，或用 --username 指定学号。")
        username = username or input("学号：").strip()
        if not username:
            raise EOFError
        password = password or getpass.getpass("密码：")
        if not password:
            raise EOFError

    def read_code():
        if not sys.stdin.isatty():
            raise LoginError("非交互运行无法等待短信输入；请在终端保持本次登录并输入验证码。")
        code = input("请输入本次登录收到的短信验证码：").strip()
        if not code:
            raise EOFError
        return code

    return login(username, password, sms_code=sms_code or os.environ.get("SMS_CODE"),
                 code_provider=read_code if sys.stdin.isatty() else None, timeout=timeout)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--username", help="学号；默认读取 STUDENT_ID 或交互输入")
    parser.add_argument("--sms-code", help="本次登录的短信验证码；默认读取 SMS_CODE 或交互输入")
    parser.add_argument("--auth-file", default="auth.txt", help="认证码保存位置（默认 auth.txt）")
    parser.add_argument("--timeout", type=_timeout, default=15, help="单次请求超时秒数（默认 15）")
    args = parser.parse_args(argv)
    try:
        token = prompt_login(args.username, args.sms_code, timeout=args.timeout)
        _save_token(token, args.auth_file)
        print("登录成功，认证码已保存。可继续使用原下载入口。")
        return 0
    except (LoginError, EOFError, KeyboardInterrupt, OSError) as error:
        message = str(error) if isinstance(error, LoginError) else "登录取消、输入不可用或认证文件无法保存。"
        print(message, file=sys.stderr)
        return 1
    except ImportError:
        print("账号密码登录依赖未安装，请运行 uv sync --extra sso；也可使用手动 token 登录。", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
