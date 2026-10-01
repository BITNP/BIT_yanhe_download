"""Offline protocol fixtures; no login, SMS or network calls.

AES literals were calculated independently with Node's OpenSSL-backed crypto
and checked against the school's CryptoJS in an isolated Node VM (no browser):
https://sso.bit.edu.cn/gate/public/crypto/crypto-merged.min.js
Mode: CryptoJS.mode.ECB; padding: CryptoJS.pad.Pkcs7; Base64 key and output.
"""

import base64
import importlib
import json
import unittest
from unittest.mock import patch

from Crypto.Cipher import AES, PKCS1_v1_5
from Crypto.PublicKey import RSA
from Crypto.Util.Padding import pad, unpad


KEY = bytes.fromhex("000102030405060708090a0b0c0d0e0f")
ENCODED_KEY = "AAECAwQFBgcICQoLDA0ODw=="  # gitleaks:allow -- bytes 00..0f test vector


def server_encrypt(text, key=KEY):
    return base64.b64encode(
        AES.new(key, AES.MODE_ECB).encrypt(pad(text.encode(), 16))
    ).decode()


class CryptoTests(unittest.TestCase):
    def setUp(self):
        # An absent implementation is a meaningful red assertion, not an
        # import/collection error which would hide the protocol tests.
        self.assertIsNotNone(importlib.util.find_spec("sso_crypto"), "crypto module missing")
        self.crypto = importlib.import_module("sso_crypto")

    def test_password_matches_independent_ecb_pkcs7_vectors(self):
        vectors = {
            "": "lU9k8uTobp7ugtICFmhImQ==",
            "password": "wgyRPBoZm+SwYtwQURkJaQ==",  # gitleaks:allow -- known AES test vector
            "0123456789abcdef": "KBVnqy9M8Nc9MZgiW4uDk5VPZPLk6G6e7oLSAhZoSJk=",
            "密碼🙂": "1CPY9H2plK+LgrLS9lsTUw==",
        }
        for text, expected in vectors.items():
            with self.subTest(text=text):
                self.assertEqual(self.crypto.encrypt_password(text, ENCODED_KEY), expected)

    def test_password_rejects_bad_keys_and_text_without_echoing_input(self):
        for text, key in [("secret-value", ""), ("x", "%%%"), ("x", "YQ=="),
                          ("x", None), (None, ENCODED_KEY), ("\ud800", ENCODED_KEY)]:
            with self.subTest(text=text, key=key), self.assertRaises(ValueError) as caught:
                self.crypto.encrypt_password(text, key)
            self.assertNotIn("secret-value", str(caught.exception))

    def test_csrf_matches_javascript_base64_duplication_and_md5(self):
        # Node reference: b64(key)[:22] + b64(key) + b64(key)[22:].
        key = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdef"
        with patch("sso_crypto.secrets.choice", side_effect=key):
            headers = self.crypto.protected_csrf_headers()
        self.assertEqual(headers, {
            "Csrf-Key": key, "Csrf-Value": "edba2bc30129a58f5ea96a944d7c839e",
        })

    def test_csrf_keys_are_fresh_and_alphanumeric(self):
        first = self.crypto.protected_csrf_headers()
        second = self.crypto.protected_csrf_headers()
        self.assertRegex(first["Csrf-Key"], r"\A[A-Za-z0-9]{32}\Z")
        self.assertNotEqual(first["Csrf-Key"], second["Csrf-Key"])

    def test_request_wraps_base64_aes_key_with_rsa_and_compact_utf8_json(self):
        private_key = RSA.generate(2048)
        public_key = private_key.public_key().export_key().decode()
        with patch("sso_crypto.SSO_PUBLIC_KEY", public_key):
            body, headers, key = self.crypto.encrypt_sms_body({"userId": "用户"})
        self.assertEqual(len(key), 16)
        wrapped = PKCS1_v1_5.new(private_key).decrypt(
            base64.b64decode(headers["privateKey"]), b"invalid"
        )
        self.assertEqual(wrapped, base64.b64encode(key))
        plaintext = unpad(AES.new(key, AES.MODE_ECB).decrypt(base64.b64decode(body)), 16)
        self.assertEqual(plaintext.decode(), '{"userId":"用户"}')
        self.assertEqual(headers["hasCrypto"], "true")
        self.assertEqual(headers["Content-Type"], "application/json")
        self.assertRegex(headers["Csrf-Key"], r"\A[A-Za-z0-9]{32}\Z")
        self.assertRegex(headers["Csrf-Value"], r"\A[0-9a-f]{32}\Z")

    def test_official_key_is_2048_bit_rsa_and_requests_use_fresh_keys(self):
        key = RSA.import_key(self.crypto.SSO_PUBLIC_KEY)
        self.assertEqual(key.size_in_bits(), 2048)
        self.assertEqual(key.e, 65537)
        first = self.crypto.encrypt_sms_body({"userId": "synthetic-id"})
        second = self.crypto.encrypt_sms_body({"userId": "synthetic-id"})
        self.assertNotEqual(first[2], second[2])
        self.assertNotEqual(first[0], second[0])
        self.assertEqual(len(base64.b64decode(first[1]["privateKey"])), 256)

    def test_request_rejects_non_json_payloads_and_bad_public_key(self):
        for value in [None, [], {"x": object()}, {"x": float("nan")}, {"x": "\ud800"}]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.crypto.encrypt_sms_body(value)
        with patch("sso_crypto.SSO_PUBLIC_KEY", "invalid"), self.assertRaises(ValueError):
            self.crypto.encrypt_sms_body({"userId": "synthetic"})

    def test_response_unwraps_plain_encrypted_quoted_and_nested_bodies(self):
        expected = {"code": 200, "data": {"tel": "opaque", "maskTel": "***"}}
        plain = json.dumps(expected)
        encrypted = server_encrypt(plain)
        for body in [plain, encrypted, json.dumps(encrypted), server_encrypt(encrypted)]:
            with self.subTest(body=body):
                self.assertEqual(self.crypto.decrypt_sms_response(body, KEY), expected)

    def test_response_rejects_bad_base64_padding_utf8_json_keys_and_shape(self):
        bad_utf8 = base64.b64encode(AES.new(KEY, AES.MODE_ECB).encrypt(pad(b"\xff", 16))).decode()
        bad_padding = base64.b64encode(AES.new(KEY, AES.MODE_ECB).encrypt(b"x" * 16)).decode()
        for body, key in [("", KEY), ("%%%", KEY), ("YQ==", KEY), (bad_utf8, KEY),
                          (bad_padding, KEY), ("[]", KEY), ("null", KEY),
                          ('{"x":NaN}', KEY), (server_encrypt("not json"), KEY),
                          (server_encrypt('{"ok":true}'), b"x" * 16),
                          ("{}", b"short"), ("{}", None), (None, KEY)]:
            with self.subTest(body=body, key=key), self.assertRaises(ValueError):
                self.crypto.decrypt_sms_response(body, key)

    def test_response_nesting_is_bounded(self):
        body = "{}"
        for _ in range(12):
            body = server_encrypt(body)
        with self.assertRaises(ValueError):
            self.crypto.decrypt_sms_response(body, KEY)


if __name__ == "__main__":
    unittest.main()
