# -*- coding: utf-8 -*-
"""checkin.py 行为测试: 账号解析 + Cookie 形态校验 + 请求一致性 + 401 诊断.

全部离线执行(不访问真实接口), 只依赖标准库 unittest 与 requests。

运行方式:
    python -m unittest discover -s tests -v
"""
from __future__ import annotations

import contextlib
import http.server
import io
import json
import os
import sys
import threading
import unittest

import requests

SRC_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

import checkin  # noqa: E402

FULL_COOKIE = (
    "BIDUPSID=98287E9E56B36DB84ED8BF89B53092C6; PSTM=1780579093; "
    "BAIDUID=98287E9E56B36DB8FE5873E5A567E6F6:FG=1; BDUSS=" + "a" * 192 + "; "
    "COOKIE_SESSION=22_2_6_9_1_27_0_0%7C9%235441074_13; H_WISE_SIDS=63146_74741"
)
BDUSS_ONLY = "BDUSS=" + "a" * 192
ENV_KEYS = (
    "DUMATE_COOKIES", "DUMATE_COOKIE", "DUMATE_USERNAME", "DUMATE_PASSWORD",
    "DUMATE_CHECKIN_URL", "DUMATE_CHECKIN_METHOD",
    "DUMATE_NOTIFY_URL", "SCT_SEND_KEY",
)


class EnvMixin(unittest.TestCase):
    """用例级环境变量隔离, 避免真实配置串味到测试里."""

    def setUp(self) -> None:
        self._saved = {key: os.environ.get(key) for key in ENV_KEYS}
        for key in ENV_KEYS:
            os.environ.pop(key, None)
        self.addCleanup(self._restore)

    def _restore(self) -> None:
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


class ParseAccountsTest(EnvMixin):
    def test_single_cookie(self):
        os.environ["DUMATE_COOKIE"] = FULL_COOKIE
        self.assertEqual(checkin.parse_accounts(), [("单账号", FULL_COOKIE)])

    def test_multiline_with_labels(self):
        os.environ["DUMATE_COOKIES"] = f"主号|{FULL_COOKIE}\n小号|{BDUSS_ONLY}"
        self.assertEqual(
            checkin.parse_accounts(), [("主号", FULL_COOKIE), ("小号", BDUSS_ONLY)]
        )

    def test_multiline_without_labels(self):
        os.environ["DUMATE_COOKIES"] = f"{FULL_COOKIE}\n{BDUSS_ONLY}"
        self.assertEqual(
            [label for label, _ in checkin.parse_accounts()], ["账号1", "账号2"]
        )

    def test_separator_inside_cookie_value_not_truncated(self):
        value = "BIDUPSID=abc; BAIDUID=abc; BDUSS=xyz|tail"
        os.environ["DUMATE_COOKIES"] = f"主号|{value}"
        self.assertEqual(checkin.parse_accounts(), [("主号", value)])

    def test_blank_and_comment_lines_skipped(self):
        os.environ["DUMATE_COOKIES"] = f"\n# 注释行\n\n主号|{FULL_COOKIE}\n"
        self.assertEqual(checkin.parse_accounts(), [("主号", FULL_COOKIE)])

    def test_crlf_line_endings(self):
        os.environ["DUMATE_COOKIES"] = f"主号|{FULL_COOKIE}\r\n小号|{BDUSS_ONLY}\r\n"
        self.assertEqual(
            checkin.parse_accounts(), [("主号", FULL_COOKIE), ("小号", BDUSS_ONLY)]
        )

    def test_json_array_of_strings(self):
        os.environ["DUMATE_COOKIES"] = json.dumps([FULL_COOKIE, BDUSS_ONLY])
        self.assertEqual(
            [cookie for _, cookie in checkin.parse_accounts()], [FULL_COOKIE, BDUSS_ONLY]
        )

    def test_json_array_of_objects(self):
        payload = [{"name": "主号", "cookie": FULL_COOKIE}]
        os.environ["DUMATE_COOKIES"] = json.dumps(payload)
        self.assertEqual(checkin.parse_accounts(), [("主号", FULL_COOKIE)])

    def test_duplicate_cookie_deduped_keeping_first_label(self):
        os.environ["DUMATE_COOKIES"] = f"主号|{FULL_COOKIE}\n重复|{FULL_COOKIE}"
        self.assertEqual(checkin.parse_accounts(), [("主号", FULL_COOKIE)])

    def test_cookies_secret_takes_precedence(self):
        os.environ["DUMATE_COOKIES"] = f"主号|{FULL_COOKIE}"
        os.environ["DUMATE_COOKIE"] = BDUSS_ONLY
        self.assertEqual(checkin.parse_accounts(), [("主号", FULL_COOKIE)])

    def test_cookie_value_kept_verbatim(self):
        raw = "BIDUPSID=abc; BAIDUID=abc:FG=1; BDUSS=xyz; COOKIE_SESSION=1%7C9%23x"
        os.environ["DUMATE_COOKIES"] = f"主号|{raw}"
        self.assertEqual(checkin.parse_accounts()[0][1], raw)

    def test_no_credentials(self):
        self.assertEqual(checkin.parse_accounts(), [])

    # ---- 两条来源同时配置时的行为: 不允许静默丢掉其中一份 ----

    def test_resolve_keeps_both_sources(self):
        os.environ["DUMATE_COOKIES"] = f"主号|{BDUSS_ONLY}"
        os.environ["DUMATE_COOKIE"] = FULL_COOKIE
        self.assertEqual(
            checkin.resolve_accounts_detailed(),
            [
                ("主号", BDUSS_ONLY, checkin.SOURCE_MULTI),
                ("单账号", FULL_COOKIE, checkin.SOURCE_SINGLE),
            ],
        )

    def test_resolve_dedupes_identical_cookie_across_sources(self):
        os.environ["DUMATE_COOKIES"] = f"主号|{FULL_COOKIE}"
        os.environ["DUMATE_COOKIE"] = FULL_COOKIE
        self.assertEqual(
            checkin.resolve_accounts_detailed(), [("主号", FULL_COOKIE, checkin.SOURCE_MULTI)]
        )

    def test_resolve_single_source_only(self):
        os.environ["DUMATE_COOKIE"] = FULL_COOKIE
        self.assertEqual(
            checkin.resolve_accounts_detailed(), [("单账号", FULL_COOKIE, checkin.SOURCE_SINGLE)]
        )


class ValidateCookieTest(unittest.TestCase):
    def test_full_cookie_has_no_problem(self):
        self.assertEqual(checkin.validate_cookie(FULL_COOKIE), [])

    def test_bduss_only_reports_missing_fields_and_short_length(self):
        joined = " | ".join(checkin.validate_cookie(BDUSS_ONLY))
        self.assertIn("BIDUPSID", joined)
        self.assertIn("BAIDUID", joined)
        self.assertIn("偏短", joined)

    def test_missing_bduss_is_required_field_problem(self):
        problems = checkin.validate_cookie("BIDUPSID=abc; BAIDUID=abc; H_WISE_SIDS=1_2")
        self.assertTrue(any("缺少必需字段" in p and "BDUSS" in p for p in problems))

    def test_non_ascii_field_name_flagged(self):
        problems = checkin.validate_cookie(f"主号｜{FULL_COOKIE}")
        self.assertTrue(any("非 ASCII" in p for p in problems))

    def test_has_required_cookie(self):
        self.assertTrue(checkin.has_required_cookie(FULL_COOKIE))
        self.assertFalse(checkin.has_required_cookie(BDUSS_ONLY.replace("BDUSS", "OTHER")))

    def test_describe_cookie_exposes_names_not_values(self):
        described = checkin.describe_cookie(FULL_COOKIE)
        self.assertIn("BDUSS", described)
        self.assertIn(f"长度 {len(FULL_COOKIE)}", described)
        self.assertNotIn("a" * 20, described)

    def test_describe_cookie_reports_key_field_lengths(self):
        described = checkin.describe_cookie(FULL_COOKIE)
        self.assertIn("BDUSS=192", described)
        self.assertIn("BIDUPSID=32", described)

    def test_describe_key_lengths_omits_absent_fields(self):
        self.assertEqual(checkin.describe_key_lengths(BDUSS_ONLY), "BDUSS=192")


class RequestIdentityTest(EnvMixin):
    """同一份 Cookie 下, 单账号与多账号两条路径发出的请求必须逐项一致."""

    records = []

    @classmethod
    def setUpClass(cls):
        outer = cls

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                outer.records.append({
                    "method": self.command,
                    "path": self.path,
                    "headers": {k.lower(): v for k, v in self.headers.items()},
                })
                payload = b'{"code":0,"success":true}'
                self.send_response(200)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, format, *args):  # noqa: A002
                pass

        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.url = f"http://127.0.0.1:{cls.server.server_address[1]}/api/dumate/points/loginBonus"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def _capture(self, **envs):
        self.records.clear()
        for key in ("DUMATE_COOKIE", "DUMATE_COOKIES"):
            os.environ.pop(key, None)
        os.environ.update(envs)
        os.environ["DUMATE_CHECKIN_URL"] = self.url
        os.environ["DUMATE_CHECKIN_METHOD"] = "POST"

        accounts = checkin.resolve_accounts()
        self.assertTrue(accounts, "应当解析出至少一个账号")
        _, cookie = accounts[0]
        ok = checkin.do_checkin(cookie, self.url, "POST", requests.Session())
        self.assertTrue(ok, "mock 返回成功特征时应判定为签到成功")
        self.assertEqual(len(self.records), 1, "应当只发出一次请求")
        return self.records[0]

    def test_single_and_multi_send_identical_requests(self):
        single = self._capture(DUMATE_COOKIE=FULL_COOKIE)
        multi = self._capture(DUMATE_COOKIES=f"主号|{FULL_COOKIE}")
        self.assertEqual(single, multi)
        self.assertEqual(single["headers"].get("cookie"), FULL_COOKIE)

    def test_only_bduss_sends_shorter_cookie(self):
        record = self._capture(DUMATE_COOKIES=f"主号|{BDUSS_ONLY}")
        self.assertEqual(record["headers"].get("cookie"), BDUSS_ONLY)
        self.assertNotEqual(record["headers"].get("cookie"), FULL_COOKIE)


class UnauthorizedDiagnosticsTest(EnvMixin):
    """401 时必须打印实际发送的 Cookie 形态与可读修复提示, 且不泄露取值."""

    records = []

    @classmethod
    def setUpClass(cls):
        outer = cls

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                outer.records.append(self.path)
                payload = '{"code":10001,"message":"登录已经过期"}'.encode("utf-8")
                self.send_response(401)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, format, *args):  # noqa: A002
                pass

        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.url = f"http://127.0.0.1:{cls.server.server_address[1]}/api/dumate/points/loginBonus"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def _run(self, label: str, cookie: str):
        self.records.clear()
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            ok = checkin.do_account_checkin(label, cookie, self.url, "POST", requests.Session())
        return ok, out.getvalue() + err.getvalue()

    def test_401_prints_shape_and_session_hint(self):
        ok, text = self._run("主号", FULL_COOKIE)
        self.assertFalse(ok)
        self.assertIn("本次实际发送的 Cookie 形态", text)
        self.assertIn("BDUSS=192", text)
        self.assertIn("形态完整", text)
        self.assertEqual(len(self.records), 1)

    def test_401_with_incomplete_cookie_gives_completion_hint(self):
        ok, text = self._run("主号", BDUSS_ONLY)
        self.assertFalse(ok)
        self.assertIn("本次实际发送的 Cookie 形态", text)
        self.assertIn("Cookie 形态问题", text)
        self.assertIn("BIDUPSID", text)
        self.assertIn("完整", text)
        self.assertEqual(len(self.records), 1)

    def test_no_value_leaked_in_diagnostics(self):
        _, text = self._run("主号", FULL_COOKIE)
        self.assertNotIn("a" * 20, text)

    def test_missing_bduss_skips_request_with_readable_error(self):
        ok, text = self._run("主号", "BIDUPSID=abc; BAIDUID=abc")
        self.assertFalse(ok)
        self.assertIn("缺少 BDUSS", text)
        self.assertEqual(self.records, [], "缺 BDUSS 时不应发出请求")

    def test_account_source_printed(self):
        os.environ["DUMATE_COOKIES"] = f"主号|{BDUSS_ONLY}"
        os.environ["DUMATE_COOKIE"] = FULL_COOKIE
        os.environ["DUMATE_CHECKIN_URL"] = self.url
        os.environ["DUMATE_CHECKIN_METHOD"] = "POST"
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = checkin.main()
        text = out.getvalue() + err.getvalue()
        self.assertEqual(code, 1)
        self.assertIn("凭据来源: DUMATE_COOKIES", text)
        self.assertIn("凭据来源: DUMATE_COOKIE", text)
        self.assertIn("待签到账号数: 2", text)
        self.assertIn("[FAIL] 主号 (凭据来源: DUMATE_COOKIES)", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)