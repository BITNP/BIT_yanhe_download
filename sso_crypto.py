"""Small wire-format helpers matching BIT's official CAS JavaScript.

Sources (password AES, request RSA/AES, response unpacking and CSRF):
https://sso.bit.edu.cn/gate/public/cas-login/main-es2015.f081d394156237abc751.js
https://sso.bit.edu.cn/gate/public/cas-gateway/main-es2015.8ff24c896b1bba185a22.js

ECB, PKCS7, RSA PKCS1_v1_5 and MD5 are used for protocol compatibility,
not as recommendations for new cryptographic protocols. No network or storage.
"""

import base64
import hashlib
import json
import secrets
import string

from Crypto.Cipher import AES, PKCS1_v1_5
from Crypto.PublicKey import RSA
from Crypto.Util.Padding import pad, unpad


# Verbatim public value passed to setPublicKey in both official bundles above.
SSO_PUBLIC_KEY = """-----BEGIN PUBLIC KEY-----
MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAjVr1zKwohU3xA0afprWLSQvIymaSH/V27MedFc+CecXSnORIFMAp4uEIb4taDq/2X4eMeTI66Mu/rB5GKSFDbExF2Gu4NaO/CNDpf1gHMScUrIFCh4CDqzBnx17kclvezLkIK0T8FVa4cRsINvzjbnA6jUSMaf6Fm1n9wTAtW6QYBjssGOEtCj+c38PTBdFMmJbXp3brt1tEBesz6lb3Fjp76FGvDZ08xtYG8fxYPuiMwKU04eS+mcX/BunwgpU3zwekHYB+PWRIvq0lBry9Wms25sJE5T/RAv5fEuMLbBkfcZK3+7ivSZthTmPpr2Ap/ji70ZZ6u2jvR5VJq+LJHQIDAQAB
-----END PUBLIC KEY-----"""


def _decode_base64(value: str) -> bytes:
    if not isinstance(value, str) or not value:
        raise ValueError("Expected nonempty Base64 text")
    try:
        return base64.b64decode(value, validate=True)
    except (ValueError, TypeError):
        raise ValueError("Invalid Base64 text") from None


def _validate_key(key: bytes) -> None:
    if not isinstance(key, bytes) or len(key) not in (16, 24, 32):
        raise ValueError("Invalid AES key")


def _encrypt(text: str, key: bytes) -> str:
    ciphertext = AES.new(key, AES.MODE_ECB).encrypt(pad(text.encode("utf-8"), 16))
    return base64.b64encode(ciphertext).decode("ascii")


def encrypt_password(text: str, encoded_key: str) -> str:
    """Encrypt a password with the page's Base64 login-croypto value.

    The caller submits that same value as croypto. Missing keys are rejected;
    choosing whether a page requires encryption belongs to the login caller.
    """
    if not isinstance(text, str):
        raise ValueError("Password must be text")
    key = _decode_base64(encoded_key)
    _validate_key(key)
    try:
        return _encrypt(text, key)
    except UnicodeError:
        raise ValueError("Password is not valid UTF-8 text") from None


def protected_csrf_headers() -> dict[str, str]:
    """Generate the frontend's independent CSRF pair for a protected request."""
    key = "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(32))
    encoded = base64.b64encode(key.encode("ascii")).decode("ascii")
    half = len(encoded) // 2
    mixed = encoded[:half] + encoded + encoded[half:]
    return {"Csrf-Key": key, "Csrf-Value": hashlib.md5(mixed.encode("ascii")).hexdigest()}


def encrypt_sms_body(payload: dict) -> tuple[str, dict[str, str], bytes]:
    """Wrap the phone lookup JSON; return body, HTTP headers and response key.

    Each request uses a fresh 128-bit AES key. The privateKey header contains
    RSA-encrypted Base64 key text (despite its name, it is not a private key).
    Send the returned body using requests' data=, not json=.
    """
    if not isinstance(payload, dict):
        raise ValueError("SMS payload must be a JSON object")
    try:
        text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        key = secrets.token_bytes(16)
        body = _encrypt(text, key)
        wrapped = PKCS1_v1_5.new(RSA.import_key(SSO_PUBLIC_KEY)).encrypt(base64.b64encode(key))
    except (ValueError, TypeError, RecursionError):
        raise ValueError("Invalid SMS payload or encryption key") from None
    headers = {
        "hasCrypto": "true",
        "privateKey": base64.b64encode(wrapped).decode("ascii"),
        "Content-Type": "application/json",
        **protected_csrf_headers(),
    }
    return body, headers, key


def _reject_json_constant(value: str):
    raise ValueError("Non-finite JSON number")


def decrypt_sms_response(response_text: str, aes_key: bytes) -> dict:
    """Unwrap JSON, JSON-quoted ciphertext or nested AES layers into an object.

    Unlike the frontend's recursive unpacker, this stops after eight layers
    and rejects malformed data rather than returning undecoded response text.
    """
    _validate_key(aes_key)
    if not isinstance(response_text, str) or not response_text:
        raise ValueError("SMS response must be nonempty text")
    current = response_text
    try:
        for _ in range(8):
            try:
                parsed = json.loads(current, parse_constant=_reject_json_constant)
            except json.JSONDecodeError:
                ciphertext = _decode_base64(current)
                current = unpad(AES.new(aes_key, AES.MODE_ECB).decrypt(ciphertext), 16).decode("utf-8")
                continue
            if isinstance(parsed, dict):
                return parsed
            if not isinstance(parsed, str):
                break
            current = parsed
    except (ValueError, TypeError, RecursionError):
        raise ValueError("Invalid encrypted SMS response") from None
    raise ValueError("SMS response is not an object or exceeds the nesting limit")
