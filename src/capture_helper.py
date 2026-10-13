#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
本地抓包辅助脚本(自抓包替代方案的配套工具)
==========================================

背景:
  GitHub Actions 是无界面的 CI 环境, 无法直接运行抓包工具;
  本脚本在你的本地电脑上运行, 用 Python 直接"替你做抓包"——
  完成两件事:
    1) 自动登录百度账号并导出会话 Cookie(等同抓包拿 Cookie);
    2) 对候选签到接口做连通性/特征探测, 找出真实签到接口地址。

用法:
  python src/capture_helper.py --username 你的百度账号 --password 你的密码
  python src/capture_helper.py --cookie "手动Cookie字符串"           # 已有 Cookie 模式

输出:
  屏幕打印登录结果、候选接口探测结果, 并生成 dummy_capture_result.json
  供你对照填入 GitHub Secrets(DUMATE_CHECKIN_URL 等)。

注意:
  - 账号密码仅在本地处理, 不会上传; 请勿把输出文件提交到仓库;
  - 百度可能要求验证码, 此时请改用手动 Cookie 模式。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Optional

import requests

# 保证可导入同目录模块
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from baidu_login import BaiduLoginError, cookies_to_header, login_with_password  # noqa: E402

CANDIDATE_CHECKIN_URLS = [
    "https://api.dumate.cn/api/v1/user/checkin",
    "https://api.dumate.cn/api/v1/user/check-in",
    "https://api.dumate.cn/api/v1/user/sign",
    "https://dumate.baidu.com/api/v1/user/checkin",
]

HEADERS_TEMPLATE = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://www.dumate.cn/",
}


def probe(cookie: str, method: str = "POST") -> list:
    """探测候选接口, 返回可用的接口地址列表."""
    found = []
    session = requests.Session()
    for url in CANDIDATE_CHECKIN_URLS:
        try:
            headers = dict(HEADERS_TEMPLATE)
            headers["Cookie"] = cookie
            if method.upper() == "GET":
                resp = session.get(url, headers=headers, timeout=15)
            else:
                resp = session.post(url, headers=headers, timeout=15)
            text = (resp.text or "")[:300]
            print(f"[probe] {method} {url} -> HTTP {resp.status_code} | {text}")
            if resp.status_code < 400:
                found.append(url)
        except requests.RequestException as exc:
            print(f"[probe] {url} 异常: {exc}")
    return found


def main() -> int:
    parser = argparse.ArgumentParser(description="百度搭子本地登录+接口探测辅助脚本")
    parser.add_argument("--username", help="百度账号(与 --password 或 -p 成对)")
    parser.add_argument("--password", "-p", help="百度密码")
    parser.add_argument("--cookie", help="手动 Cookie(跳过登录)")
    parser.add_argument(
        "--method",
        default="POST",
        choices=["GET", "POST"],
        help="候选接口探测使用的请求方法(默认 POST)",
    )
    args = parser.parse_args()

    cookie: Optional[str] = args.cookie
    if not cookie and args.username and args.password:
        print("[info] 尝试账号密码自动登录...")
        try:
            cookie = cookies_to_header(login_with_password(args.username, args.password))
            print(f"[info] 登录成功, Cookie 前缀: {cookie[:40]}...")
        except BaiduLoginError as exc:
            print(f"[error] 登录失败: {exc}", file=sys.stderr)
            return 1
    if not cookie:
        print(
            "[error] 需要提供 --cookie 或 --username/--password 之一",
            file=sys.stderr,
        )
        return 2

    print("[info] 开始探测候选签到接口...")
    found = probe(cookie, args.method)

    result = {
        "cookie_ready": True,
        "found_checkin_urls": found,
        "hint": (
            "将探测返回的第一个可用地址填入 GitHub Secret DUMATE_CHECKIN_URL;"
            " Cookie 对应填入 DUMATE_COOKIE(若需手动模式)"
            if found
            else "未探测到可用接口, 请在电脑上用 Fiddler/Charles 抓包确认后填写 DUMATE_CHECKIN_URL"
        ),
    }
    with open("dummy_capture_result.json", "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f"[info] 结果已写入 dummy_capture_result.json")
    if not found:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())