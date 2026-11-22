"""Unit tests for security helpers: SSRF host validation and secret scanning.

Every credential-shaped string in this file is assembled at runtime so the
test source itself never contains a literal secret (and does not trip the
very scanner it tests).
"""

import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from llm_adapter import normalize_base_url

_spec = importlib.util.spec_from_file_location(
    "printops_secret_scan", Path(__file__).resolve().parents[1] / "tools" / "secret_scan.py")
secret_scan = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(secret_scan)

FILLER = "Ab1Cd2Ef3Gh4Ij5Kl6Mn7Op8Qr9St0"


def _fixture(kind: str) -> str:
    shapes = {
        "openai": "sk-" + FILLER[:20],
        "google": "AIza" + FILLER[:32],
        "github": "ghp_" + FILLER[:34],
        "groq": "gsk_" + FILLER[:24],
        "huggingface": "hf_" + FILLER[:33],
        "slack": "xoxb-" + "1" * 10 + "-" + FILLER[:14],
        "aws": "AKIA" + FILLER[:16].upper(),
        "generic": "super-secret-value-" + FILLER[:20],
    }
    return shapes[kind]


class SsrfHostValidationTest(unittest.TestCase):
    def test_rejects_loopback_private_and_reserved_literal_hosts(self):
        for url in ("http://localhost:11434/v1", "http://127.0.0.1:8080/v1", "http://[::1]/v1",
                    "http://0.0.0.0/v1", "http://10.1.2.3/v1", "http://192.168.1.1/v1",
                    "http://172.16.0.9/v1", "http://169.254.169.254/latest/v1",
                    "http://100.64.0.1/v1", "http://240.0.0.1/v1",
                    "http://api.internal.local/v1", "http://metadata.localhost/v1"):
            with self.assertRaises(ValueError, msg=url):
                normalize_base_url(url)

    def test_rejects_hostnames_that_resolve_to_internal_space(self):
        infos = [(2, 1, 6, "", ("10.0.0.5", 0))]
        with mock.patch("llm_adapter.socket.getaddrinfo", return_value=infos, create=True):
            with self.assertRaises(ValueError):
                normalize_base_url("https://looks-public.example/v1")

    def test_unresolvable_hostname_is_allowed_and_fails_at_request_time(self):
        with mock.patch("llm_adapter.socket.getaddrinfo",
                        side_effect=OSError("name resolution failed"), create=True):
            self.assertEqual(normalize_base_url("https://example.test/v1"),
                             "https://example.test/v1")

    def test_public_url_still_normalizes(self):
        self.assertEqual(normalize_base_url(" https://api.example.com/v1/ "),
                         "https://api.example.com/v1")

    def test_private_hosts_require_explicit_opt_in(self):
        self.assertEqual(
            normalize_base_url("http://10.1.2.3/v1", allow_private_hosts=True),
            "http://10.1.2.3/v1",
        )
        infos = [(2, 1, 6, "", ("10.0.0.5", 0))]
        with mock.patch("llm_adapter.socket.getaddrinfo", return_value=infos, create=True):
            self.assertEqual(
                normalize_base_url("https://llm.internal.example/v1", allow_private_hosts=True),
                "https://llm.internal.example/v1",
            )

    def test_opt_in_keeps_structural_url_rules(self):
        with self.assertRaises(ValueError):
            normalize_base_url("ftp://10.1.2.3/v1", allow_private_hosts=True)
        with self.assertRaises(ValueError):
            normalize_base_url("https://user:pass@10.1.2.3/v1", allow_private_hosts=True)
        with self.assertRaises(ValueError):
            normalize_base_url("https://10.1.2.3/v1?key=1", allow_private_hosts=True)

    def test_structural_rules_are_kept(self):
        with self.assertRaises(ValueError):
            normalize_base_url("ftp://api.example.com/v1")
        with self.assertRaises(ValueError):
            normalize_base_url("https://user:pass@api.example.com/v1")
        with self.assertRaises(ValueError):
            normalize_base_url("https://api.example.com/v1?key=1")
        self.assertEqual(normalize_base_url(""), "")


class SecretScanTest(unittest.TestCase):
    def scan(self, files: dict[str, str]) -> list[str]:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name, content in files.items():
                target = root / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8")
            return secret_scan.collect_hits(root)

    def test_known_secret_shapes_are_detected(self):
        plain = {"openai": "openai = ", "google": "google = ", "github": "github = ",
                 "groq": "groq = ", "huggingface": "hf = ", "slack": "slack = ",
                 "aws": "aws = "}
        for name, prefix in plain.items():
            hits = self.scan({"file.txt": prefix + _fixture(name)})
            self.assertTrue(hits, f"{name} 形态应被识别")

        generic = 'config = {"api_' + 'key": "' + _fixture("generic") + '"};'
        self.assertTrue(self.scan({"file.txt": generic}), "通用凭据字段应被识别")

    def test_bearer_and_authorization_header_forms_are_detected(self):
        # TD-07: credentials without a provider prefix travel as header values.
        self.assertTrue(self.scan({"file.txt": "curl -H 'Authorization: Bearer " + FILLER[:24] + "'"}),
                        "Bearer 令牌应被识别")
        self.assertTrue(self.scan({"file.txt": 'headers = {"Authorization": "' + FILLER[:24] + '"}'}),
                        "Authorization 头取值应被识别")
        # The repository's own interpolation shape stays clean: an f-string
        # placeholder or a too-short value is not a leaked credential.
        self.assertFalse(secret_scan.scan_text('{"Authorization": f"Bearer {self.api_key}"}'))
        self.assertFalse(secret_scan.scan_text("Authorization: Bearer short"))

    def test_audit_supplement_shapes_are_detected(self):
        # TD-28（安全体检 S8）：PEM 头、Stripe/GitLab/npm/JWT/阿里云形态补齐。
        # PEM 头在源码里拼装（完整字面量会被 secret_scan 本身命中）。
        pem_head = "-----BEGIN " + "OPENSSH " + "PRIVATE" + " KEY" + "-----"
        pem_rsa = "-----BEGIN " + "RSA " + "PRIVATE" + " KEY" + "-----"
        samples = {
            "PEM 私钥": pem_head + "\n",
            "PEM 私钥 RSA": pem_rsa,
            "Stripe 密钥": "sk_live_" + FILLER[:24],
            "Stripe 令牌": "rk_live_" + FILLER[:24],
            "GitLab 令牌": "glpat-" + FILLER[:24],
            "npm 令牌": "npm_" + FILLER[:40],
            "JWT 令牌": "eyJ" + FILLER[:30] + ".eyJ" + FILLER[:20] + "." + FILLER[:20],
            "阿里云 AccessKey": "LTAI" + FILLER[:20],
        }
        for name, text in samples.items():
            hits = self.scan({"file.txt": text})
            self.assertTrue(hits, f"{name} 形态应被识别")
        # 类似前缀但长度不足/非凭据语境不误报。
        self.assertFalse(secret_scan.scan_text("sk_live_short"), "过短值不是凭据")
        self.assertFalse(secret_scan.scan_text("describe eyJscheme labels"), "散列 eyJ 无 JWT 结构不误报")

    def test_designed_plaintext_key_file_does_not_trip_but_leaked_copies_do(self):
        secret = _fixture("openai")
        allowed = self.scan({"data/llm_config.json":
                             '{"url": "https://api.example.com/v1", "model": "m", "key": "' + secret + '"}'})
        self.assertEqual(allowed, [])

        leaked = self.scan({"data/llm_config.json":
                            '{"url": "https://' + secret + '.example.com/v1", "model": "m", "key": ""}'})
        self.assertTrue(leaked)

        leaked_note = self.scan({"data/llm_config.json":
                                 '{"url": "", "model": "' + secret + '", "key": "' + secret + '"}'})
        self.assertTrue(leaked_note)

    def test_google_shape_inside_encoded_payload_is_not_a_credential_token(self):
        candidate = "AIza" + "Z" * 35
        self.assertFalse(secret_scan.scan_text("AAAA" + candidate + "BBBB"))
        for text in (candidate, '"' + candidate + '"', "?key=" + candidate, "\n" + candidate):
            self.assertTrue(secret_scan.scan_text(text), "literal and URL credential tokens remain detected")

    def test_binary_and_ignored_dirs_are_skipped(self):
        self.assertEqual(self.scan({".git/config": _fixture("openai"),
                                    "blob.sqlite3": _fixture("openai")}), [])


if __name__ == "__main__":
    unittest.main()
