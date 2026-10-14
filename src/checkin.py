#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
百度搭子(DuMate) 每日自动签到脚本
==================================

功能:
  - 优先使用百度账号密码自动登录(DUMATE_USERNAME + DUMATE_PASSWORD),
    全程免手动抓包, 登录后自动携带会话 Cookie 调用签到接口;
  - 兼容手动 Cookie 模式(DUMATE_COOKIE), 账号密码登录不可用时回退;
  - 签到接口自动探测: 从候选接口列表逐个尝试, 支持用户用
    DUMATE_CHECKIN_URL 指定真实接口(自抓包替代方案);
  - 校验响应状态并输出可读结果; 可选 Webhook 通知。

认证模式(按优先级):
  1. DUMATE_USERNAME + DUMATE_PASSWORD -> 自动登录
  2. DUMATE_COOKIE -> 使用手动 Cookie

环境变量(通过 GitHub Secrets 配置):
  DUMATE_USERNAME       可选, 百度账号
  DUMATE_PASSWORD       可选, 百度密码(与 USERNAME 成对使用)
  DUMATE_COOKIE         可选, 手动 Cookie(账号密码不可用时回退)
  DUMATE_CHECKIN_URL    可选, 显式指定签到接口; 未指定则从候选列表探测
  DUMATE_CHECKIN_METHOD 可选, GET 或 POST; 默认 POST
  DUMATE_NOTIFY_URL     可选, 通知 Webhook 地址

退出码:
  0  签到成功或已签过
  1  认证失败 / 签到请求失败 / 鉴权失效 / 缺少必需凭据
  2  其它内部错误
"""

from __future__ import annotations

import json
import os
import sys
import time
from typing import Any, Dict, List, Optional

import requests

from baidu_login import BaiduLoginError, cookies_to_header, login_with_password

# ---------------------------------------------------------------- 常量

# 签到接口候选列表(自抓包替代): 按顺序探测, 第一个返回成功特征者胜出。
# 说明: GitHub Actions 为无界面环境, 无法真正执行抓包; 工程通过
# "候选接口自动探测 + 用户可配置真实验收" 实现等价能力。
# 若以下候选均失效, 请用 DUMATE_CHECKIN_URL 填入抓包实测的真实接口。
CANDIDATE_CHECKIN_URLS: List[str] = [
    "https://api.dumate.cn/api/v1/user/checkin",
    "https://api.dumate.cn/api/v1/user/check-in",
    "https://api.dumate.cn/api/v1/user/sign",
    "https://dumate.baidu.com/api/v1/user/checkin",
]

DEFAULT_METHOD = "POST"
REQUEST_TIMEOUT = 30


# ---------------------------------------------------------------- 工具函数

def env(name: str, default: str = "") -> str:
    """读取环境变量并去掉首尾空白."""
    return os.environ.get(name, default).strip()


def notify(message: str, webhook: str) -> None:
    """通过通用 Webhook 推送文本消息(失败不影响流程)."""
    if not webhook:
        return
    try:
        payload: Dict[str, Any] = {"text": "百度搭子签到", "desp": message}
        requests.post(webhook, json=payload, timeout=REQUEST_TIMEOUT)
    except requests.RequestException as exc:
        print(f"[warn] 通知发送失败(不影响签到结果): {exc}")


def serverchan_notify(title: str, desp: str) -> None:
    """通过 Server 酱(新版 sctapi.ftqq.com)推送通知(失败不影响流程)."""
    sendkey = env("SCT_SEND_KEY")
    if not sendkey:
        return
    url = f"https://sctapi.ftqq.com/{sendkey}.send"
    try:
        resp = requests.post(url, data={"title": title, "desp": desp}, timeout=REQUEST_TIMEOUT)
        if resp.status_code == 200:
            print("[info] Server 酱推送成功")
        else:
            print(f"[warn] Server 酱推送返回 HTTP {resp.status_code}: {resp.text[:200]}")
    except requests.RequestException as exc:
        print(f"[warn] Server 酱推送失败(不影响签到结果): {exc}")


def summarize_response(resp: requests.Response) -> str:
    """把接口响应整理成可读摘要."""
    text = resp.text or ""
    try:
        return json.dumps(resp.json(), ensure_ascii=False)[:2000]
    except ValueError:
        return text[:2000]


def looks_success(resp: requests.Response) -> bool:
    """判断签到响应是否包含成功特征(供接口探测使用)."""
    text = (resp.text or "").lower()
    if resp.status_code == 401 or resp.status_code == 403:
        return False
    if resp.status_code >= 500:
        return False

    # 常见成功特征: errno=0 / code=0 / success / 已签到 / checkin ok 等
    success_markers = (
        '"errno":0', '"errno": 0', '"code":0', '"code": 0',
        "success", "ok", "已签到", "签到成功",
    )
    if any(m in text for m in success_markers):
        return True

    # 没有明确错误关键字且为 2xx, 视为可能成功(探测时不误杀)
    error_markers = ("\"errno\":-", '"errno": -', '"code":-', '"code": -', "error")
    if resp.status_code < 400 and not any(m in text for m in error_markers):
        return True
    return False


# ---------------------------------------------------------------- 认证

def resolve_cookie() -> Optional[str]:
    """
    解析签到请求所需的 Cookie。

    优先账号密码自动登录; 失败或未配置时回退手动 Cookie。
    返回 None 表示没有任何可用凭据。
    """
    username = env("DUMATE_USERNAME")
    password = env("DUMATE_PASSWORD")
    manual_cookie = env("DUMATE_COOKIE")

    if username and password:
        print("[info] 检测到账号密码, 开始自动登录百度账号...")
        try:
            cookies = login_with_password(username, password)
            cookie_str = cookies_to_header(cookies)
            print("[info] 百度账号登录成功, 已获取会话凭据")
            return cookie_str
        except BaiduLoginError as exc:
            print(f"[warn] 账号密码登录失败: {exc}")
            if manual_cookie:
                print("[warn] 回退到手动 Cookie 模式")
                return manual_cookie
            print("[error] 未配置手动 Cookie, 无法继续签到。", file=sys.stderr)
            return None
    elif manual_cookie:
        print("[info] 使用手动 Cookie 模式")
        return manual_cookie
    else:
        print(
            "[error] 缺少凭据: 请配置 DUMATE_USERNAME + DUMATE_PASSWORD "
            "(自动登录)或 DUMATE_COOKIE(手动 Cookie)。",
            file=sys.stderr,
        )
        return None


# ---------------------------------------------------------------- 签到

def try_checkin(cookie: str, url: str, method: str, session: requests.Session) -> bool:
    """对单个接口地址执行签到请求."""
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0 Safari/537.36"
        ),
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Referer": "https://www.dumate.cn/",
        "Origin": "https://www.dumate.cn",
        "Cookie": cookie,
    }

    try:
        if method == "GET":
            resp = session.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
        else:
            resp = session.post(url, headers=headers, timeout=REQUEST_TIMEOUT)
    except requests.RequestException as exc:
        print(f"[warn] 请求 {url} 异常: {exc}")
        return False

    summary = summarize_response(resp)
    print(f"[info] {method} {url} -> HTTP {resp.status_code}")

    if resp.status_code == 401 or resp.status_code == 403:
        print(f"[error] 鉴权失败(Http {resp.status_code}), 凭据可能已失效: {summary}")
        return False
    if resp.status_code >= 400:
        # 4xx/5xx 默认失败; 若响应体明确含成功标记则兜底通过(已签过等场景)
        if looks_success(resp):
            return True
        print(f"[warn] 请求失败(Http {resp.status_code}), 尝试下一个候选接口: {summary}")
        return False

    print(f"[info] 响应内容: {summary}")
    return True


def do_checkin(cookie: str, url_override: str, method: str, session: requests.Session) -> bool:
    """执行签到; 未显式指定接口时自动探测候选列表."""
    if url_override:
        print(f"[info] 使用用户指定接口: {url_override}")
        return try_checkin(cookie, url_override, method, session)

    print("[info] 未指定接口, 开始自动探测候选签到接口...")
    last_success = False
    for candidate in CANDIDATE_CHECKIN_URLS:
        print(f"[info] 尝试候选接口: {candidate}")
        ok = try_checkin(cookie, candidate, method, session)
        if ok:
            last_success = True
            print(f"[done] 候选接口生效: {candidate}")
            break
    else:
        print(
            "[error] 所有候选接口均未确认成功。请用抓包(本地执行 src/capture_helper.py) "
            "确认真实签到接口, 然后通过 DUMATE_CHECKIN_URL 配置。",
            file=sys.stderr,
        )
    return last_success


# ---------------------------------------------------------------- 入口

def main() -> int:
    cookie = resolve_cookie()
    if not cookie:
        return 1

    url_override = env("DUMATE_CHECKIN_URL")
    method = env("DUMATE_CHECKIN_METHOD", DEFAULT_METHOD).upper()
    if method not in ("GET", "POST"):
        print(f"[error] DUMATE_CHECKIN_METHOD 仅支持 GET/POST, 当前值: {method}", file=sys.stderr)
        return 2

    notify_url = env("DUMATE_NOTIFY_URL")
    print(f"[info] 开始百度搭子每日签到 | 时间: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"[info] 认证模式: {'账号密码自动登录' if env('DUMATE_USERNAME') and env('DUMATE_PASSWORD') else '手动 Cookie'}")

    session = requests.Session()
    ok = do_checkin(cookie, url_override, method, session)

    if ok:
        summary = "百度搭子签到成功 (HTTP 2xx)"
        print(f"[done] {summary}")
        notify(summary, notify_url)
        serverchan_notify("百度搭子签到成功", summary)
        return 0

    summary = "百度搭子签到失败, 请检查凭据是否有效或签到接口地址是否正确"
    print(f"[failed] {summary}", file=sys.stderr)
    notify(summary, notify_url)
    serverchan_notify("百度搭子签到失败", summary)
    return 1


if __name__ == "__main__":
    sys.exit(main())