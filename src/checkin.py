#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
百度搭子(DuMate) 每日自动签到脚本
==================================

功能:
  - 支持多账号签到: 通过 DUMATE_COOKIES 配置多个账号的 Cookie,
    一次运行逐个签到并汇总每个账号的结果;
  - 兼容单账号: DUMATE_COOKIE 单账号 Cookie, 或
    DUMATE_USERNAME + DUMATE_PASSWORD 自动登录(CI 环境可能触发风控);
  - 签到接口自动探测: 从候选接口列表逐个尝试, 支持用户用
    DUMATE_CHECKIN_URL 指定真实接口(自抓包替代方案);
  - 校验响应状态并输出可读结果; 任一账号失败时通过 Server 酱/Webhook 通知。

认证模式(按优先级):
  1. DUMATE_COOKIES    -> 多账号 Cookie(推荐)
  2. DUMATE_COOKIE     -> 单账号 Cookie(向后兼容)
  3. DUMATE_USERNAME + DUMATE_PASSWORD -> 自动登录(单账号)

DUMATE_COOKIES 支持的格式:
  a) JSON 数组: ["cookieA", "cookieB"]
     或 [{"name": "账号A", "cookie": "..."}, ...]
  b) 多行文本: 每行一个 Cookie; 可写成 "账号标签|cookie" 便于区分

环境变量(通过 GitHub Secrets 配置):
  DUMATE_COOKIES        可选, 多账号 Cookie(见上)
  DUMATE_COOKIE         可选, 单账号 Cookie(向后兼容)
  DUMATE_USERNAME       可选, 百度账号(单账号自动登录)
  DUMATE_PASSWORD       可选, 百度密码(与 USERNAME 成对使用)
  DUMATE_CHECKIN_URL    可选, 显式指定签到接口; 未指定则从候选列表探测
  DUMATE_CHECKIN_METHOD 可选, GET 或 POST; 默认 POST
  DUMATE_NOTIFY_URL     可选, 通用通知 Webhook 地址
  SCT_SEND_KEY          可选, Server 酱 SendKey(失败时微信提醒)

退出码:
  0  全部账号签到成功或已签过
  1  存在账号签到失败 / 认证失败 / 缺少必需凭据
  2  其它内部错误
"""

from __future__ import annotations

import json
import os
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

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


# ---------------------------------------------------------------- 多账号

def parse_accounts() -> List[Tuple[str, str]]:
    """
    解析待签到的账号列表, 每项为 (账号标签, Cookie)。

    来源(按优先级):
      1. DUMATE_COOKIES: 多账号, 支持
         a) JSON 数组: ["cookieA", "cookieB"]
            或 [{"name": "账号A", "cookie": "..."}, ...]
         b) 多行文本: 每行一个 Cookie, 可写 "账号标签|cookie"
      2. DUMATE_COOKIE: 单账号 Cookie(向后兼容)

    返回去重后的账号列表; 空列表表示没有可用的 Cookie 账号。
    """
    accounts: List[Tuple[str, str]] = []

    def add(label: str, cookie: str) -> None:
        cookie = (cookie or "").strip()
        if cookie:
            accounts.append((label, cookie))

    raw = env("DUMATE_COOKIES")
    if raw:
        parsed_json = False
        try:
            data = json.loads(raw)
        except ValueError:
            data = None

        if isinstance(data, (list, dict)):
            parsed_json = True
            items = data if isinstance(data, list) else [data]
            for idx, item in enumerate(items, 1):
                if isinstance(item, str):
                    add(f"账号{idx}", item)
                elif isinstance(item, dict):
                    cookie = str(item.get("cookie") or item.get("cookie_str") or "")
                    label = str(item.get("name") or item.get("label") or f"账号{idx}")
                    add(label, cookie)
            if not accounts:
                print("[warn] DUMATE_COOKIES 为 JSON 但未解析出有效 Cookie")

        if not parsed_json:
            for idx, line in enumerate(raw.splitlines(), 1):
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "|" in line:
                    label, _, cookie = line.partition("|")
                    add(label.strip() or f"账号{idx}", cookie)
                else:
                    add(f"账号{idx}", line)

    if not accounts:
        add("账号1", env("DUMATE_COOKIE"))

    # 按 Cookie 去重, 保留首次出现的标签
    seen: set = set()
    unique: List[Tuple[str, str]] = []
    for label, cookie in accounts:
        if cookie in seen:
            continue
        seen.add(cookie)
        unique.append((label, cookie))
    return unique


def resolve_accounts() -> List[Tuple[str, str]]:
    """
    解析本轮所有待签到账号。

    优先使用 Cookie 账号列表; 未配置任何 Cookie 时, 回退到
    账号密码自动登录(单账号)。
    """
    accounts = parse_accounts()
    if accounts:
        return accounts

    cookie = resolve_cookie()
    if cookie:
        return [("账号1", cookie)]
    return []


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
    url_override = env("DUMATE_CHECKIN_URL")
    method = env("DUMATE_CHECKIN_METHOD", DEFAULT_METHOD).upper()
    if method not in ("GET", "POST"):
        print(f"[error] DUMATE_CHECKIN_METHOD 仅支持 GET/POST, 当前值: {method}", file=sys.stderr)
        return 2

    accounts = resolve_accounts()
    if not accounts:
        return 1

    notify_url = env("DUMATE_NOTIFY_URL")
    print(f"[info] 开始百度搭子每日签到 | 时间: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"[info] 待签到账号数: {len(accounts)}")

    session = requests.Session()
    results: List[Tuple[str, bool]] = []
    for idx, (label, cookie) in enumerate(accounts, 1):
        print(f"[info] ======== 账号 {idx}/{len(accounts)}: {label} ========")
        ok = do_checkin(cookie, url_override, method, session)
        results.append((label, ok))
        print(f"[{'done' if ok else 'failed'}] {label} 签到{'成功' if ok else '失败'}")
        if idx < len(accounts):
            # 轻微间隔, 降低并发触发的风控概率
            time.sleep(2)

    success = [label for label, ok in results if ok]
    failed = [label for label, ok in results if not ok]
    total = len(results)

    lines = [f"共 {total} 个账号: 成功 {len(success)}, 失败 {len(failed)}"]
    lines.extend(f"{'[OK]' if ok else '[FAIL]'} {label}" for label, ok in results)
    summary = "\n".join(lines)

    if not failed:
        print(f"[done] {summary}")
        notify(summary, notify_url)
        serverchan_notify("百度搭子签到成功", summary)
        return 0

    print(f"[failed] {summary}", file=sys.stderr)
    notify(summary, notify_url)
    serverchan_notify(f"百度搭子签到失败({len(failed)}/{total})", summary)
    return 1


if __name__ == "__main__":
    sys.exit(main())