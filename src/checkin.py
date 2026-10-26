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
  - Cookie 形态自查: 缺 BDUSS 时直接给出可读错误(不再浪费一次请求),
    缺 BIDUPSID/BAIDUID 或长度明显偏短时给出告警; 401/403 时打印本次
    实际发送的 Cookie 字段名与各字段长度(只打印形态, 不打印取值),
    便于定位"单账号能签、多账号 401"这类配置问题;
  - 校验响应状态并输出可读结果; 任一账号失败时通过 Server 酱/Webhook 通知。

认证模式(按优先级):
  1. DUMATE_COOKIES    -> 多账号 Cookie(推荐)
  2. DUMATE_COOKIE     -> 单账号 Cookie(向后兼容)
  3. DUMATE_USERNAME + DUMATE_PASSWORD -> 自动登录(单账号)

  注意: DUMATE_COOKIES 与 DUMATE_COOKIE 同时配置时, 两份凭据都会参与
  签到(按 Cookie 去重), 不会静默丢掉其中一份 —— 避免"刚更新的单账号
  凭据被旧的多账号配置顶掉"这种隐蔽问题。只想用一份时请清空另一个。

DUMATE_COOKIES 支持的格式:
  a) JSON 数组: ["cookieA", "cookieB"]
     或 [{"name": "账号A", "cookie": "..."}, ...]
  b) 多行文本: 每行一个 Cookie; 可写成 "账号标签|cookie" 便于区分

Cookie 取值要求(很重要):
  必须是浏览器 F12 -> Network -> 某条 dumate.baidu.com 请求的
  Request Headers 里的【完整 Cookie 串】(含 BDUSS、BIDUPSID、BAIDUID 等)。
  只粘贴 BDUSS 一段时服务端会判为未登录(401 登录已经过期), 且必须与
  抓取它的浏览器/账号保持一致, 换设备或换账号重新登录后要重新抓取。

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

# 凭据来源标记: 会打印在日志里, 用于确认"这一轮到底用了哪份配置"
SOURCE_MULTI = "DUMATE_COOKIES"
SOURCE_SINGLE = "DUMATE_COOKIE"
SOURCE_LOGIN = "账号密码自动登录"


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


# ---------------------------------------------------------------- Cookie 形态

# 服务端判定登录态所必需的核心字段; 缺 BDUSS 时一定返回 401(登录已经过期)。
REQUIRED_COOKIE_NAMES: Tuple[str, ...] = ("BDUSS",)
# 建议一并携带的字段; 只带 BDUSS 一段时实测会被判为未登录。
RECOMMENDED_COOKIE_NAMES: Tuple[str, ...] = ("BIDUPSID", "BAIDUID")
# 明显偏短(像只粘了一段)的长度阈值: 单独的 BDUSS 约 198 字符, 完整 Cookie 约 1700 字符。
MIN_COOKIE_LENGTH = 260
# 401/403 诊断时按名列出长度的关键字段(只列长度, 不列取值)。
KEY_FIELDS_TO_REPORT: Tuple[str, ...] = (
    "BDUSS", "BDUSS_BFESS", "BIDUPSID", "BAIDUID", "STOKEN",
)


def cookie_pairs(cookie: str) -> List[Tuple[str, str]]:
    """把 Cookie 串解析成 (字段名, 值) 列表; 无法解析的片段直接忽略."""
    pairs: List[Tuple[str, str]] = []
    for part in (cookie or "").split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        name, _, value = part.partition("=")
        name = name.strip()
        if name:
            pairs.append((name, value.strip()))
    return pairs


def cookie_names(cookie: str) -> List[str]:
    """返回 Cookie 中出现过的所有字段名(按首次出现顺序去重)."""
    names: List[str] = []
    for name, _ in cookie_pairs(cookie):
        if name not in names:
            names.append(name)
    return names


def describe_key_lengths(cookie: str) -> str:
    """按字段名列出关键字段的取值长度(不输出取值), 用于定位 401."""
    lengths = {name: len(value) for name, value in cookie_pairs(cookie)}
    return ", ".join(f"{name}={lengths[name]}" for name in KEY_FIELDS_TO_REPORT if name in lengths)


def describe_cookie(cookie: str) -> str:
    """
    只描述 Cookie 的形态(字段名/数量/总长度/关键字段长度), 不输出任何取值。

    用于日志与报错: 既能定位问题, 又不会把凭据写进 Actions 日志。
    """
    names = cookie_names(cookie)
    if not names:
        return f"共 0 项, 长度 {len(cookie)} 字符, 未能解析出任何键值对"
    shown = ", ".join(names[:12])
    suffix = "" if len(names) <= 12 else f" 等共 {len(names)} 项"
    base = f"共 {len(names)} 项, 长度 {len(cookie)} 字符, 字段: {shown}{suffix}"
    detail = describe_key_lengths(cookie)
    return f"{base}; 关键字段长度: {detail}" if detail else base


def validate_cookie(cookie: str) -> List[str]:
    """校验 Cookie 形态, 返回问题描述列表; 空列表表示形态正常."""
    problems: List[str] = []
    names = set(cookie_names(cookie))

    missing = [name for name in REQUIRED_COOKIE_NAMES if name not in names]
    if missing:
        problems.append(f"缺少必需字段 {'、'.join(missing)}(登录态核心)")

    lack = [name for name in RECOMMENDED_COOKIE_NAMES if name not in names]
    if lack and not missing:
        problems.append(
            f"缺少 {'、'.join(lack)}(建议一并粘贴, 只带 BDUSS 一段时实测会被判未登录)"
        )

    if len(cookie) < MIN_COOKIE_LENGTH:
        problems.append(
            f"长度仅 {len(cookie)} 字符, 明显偏短, 疑似只粘贴了片段(如只有 BDUSS=...)"
        )

    weird = [name for name in names if not name.isascii()]
    if weird:
        problems.append(
            "字段名含非 ASCII 字符(可能把账号标签一起粘了进来): " + "、".join(weird[:3])
        )

    return problems


def has_required_cookie(cookie: str) -> bool:
    """Cookie 是否包含全部必需字段."""
    names = set(cookie_names(cookie))
    return all(name in names for name in REQUIRED_COOKIE_NAMES)


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

def collect_accounts(merge_single: bool = False) -> List[Tuple[str, str, str]]:
    """
    收集待签到账号, 每项为 (账号标签, Cookie, 凭据来源)。

    merge_single=False: 仅当 DUMATE_COOKIES 未解析出账号时才使用
        DUMATE_COOKIE(向后兼容的优先级, 供 parse_accounts 使用);
    merge_single=True: DUMATE_COOKIES 与 DUMATE_COOKIE 同时存在时两者都保留
        (按 Cookie 去重), 避免"刚更新好的单账号凭据被旧的多账号配置顶掉"。

    返回按 Cookie 去重后的列表; 空列表表示没有可用的 Cookie 账号。
    """
    accounts: List[Tuple[str, str, str]] = []

    def add(label: str, cookie: str, source: str) -> None:
        cookie = (cookie or "").strip()
        if cookie:
            accounts.append(((label or "").strip(), cookie, source))

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
                    add(f"账号{idx}", item, SOURCE_MULTI)
                elif isinstance(item, dict):
                    cookie = str(item.get("cookie") or item.get("cookie_str") or "")
                    label = str(item.get("name") or item.get("label") or f"账号{idx}")
                    add(label, cookie, SOURCE_MULTI)
            if not accounts:
                print("[warn] DUMATE_COOKIES 为 JSON 但未解析出有效 Cookie")

        if not parsed_json:
            for idx, line in enumerate(raw.splitlines(), 1):
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "|" in line:
                    label, _, cookie = line.partition("|")
                    add(label or f"账号{idx}", cookie, SOURCE_MULTI)
                else:
                    add(f"账号{idx}", line, SOURCE_MULTI)

    single = env("DUMATE_COOKIE")
    if single and (merge_single or not accounts):
        add("单账号", single, SOURCE_SINGLE)

    # 按 Cookie 去重, 保留首次出现的标签
    seen: set = set()
    unique: List[Tuple[str, str, str]] = []
    for label, cookie, source in accounts:
        if cookie in seen:
            continue
        seen.add(cookie)
        unique.append((label or f"账号{len(unique) + 1}", cookie, source))
    return unique


def parse_accounts() -> List[Tuple[str, str]]:
    """
    解析待签到的账号列表(对外兼容接口), 每项为 (账号标签, Cookie)。

    来源(按优先级):
      1. DUMATE_COOKIES: 多账号, 支持
         a) JSON 数组: ["cookieA", "cookieB"]
            或 [{"name": "账号A", "cookie": "..."}, ...]
         b) 多行文本: 每行一个 Cookie, 可写 "账号标签|cookie"
      2. DUMATE_COOKIE: 单账号 Cookie(向后兼容, 标签为 "单账号")

    返回去重后的账号列表; 空列表表示没有可用的 Cookie 账号。
    """
    return [(label, cookie) for label, cookie, _ in collect_accounts(merge_single=False)]


def resolve_accounts_detailed() -> List[Tuple[str, str, str]]:
    """
    解析本轮所有待签到账号, 每项为 (账号标签, Cookie, 凭据来源)。

    同时配置 DUMATE_COOKIES 与 DUMATE_COOKIE 时两份都会保留;
    没有任何 Cookie 时才回退到账号密码自动登录(单账号)。
    """
    accounts = collect_accounts(merge_single=True)
    if accounts:
        return accounts

    cookie = resolve_cookie()
    if cookie:
        return [("账号1", cookie, SOURCE_LOGIN)]
    return []


def resolve_accounts() -> List[Tuple[str, str]]:
    """
    解析本轮所有待签到账号(对外兼容接口), 每项为 (账号标签, Cookie)。

    优先使用 Cookie 账号列表; 未配置任何 Cookie 时, 回退到
    账号密码自动登录(单账号)。
    """
    return [(label, cookie) for label, cookie, _ in resolve_accounts_detailed()]


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
        print(f"[error] 本次实际发送的 Cookie 形态: {describe_cookie(cookie)}")
        problems = validate_cookie(cookie)
        if problems:
            for problem in problems:
                print(f"[error] Cookie 形态问题: {problem}")
            if not has_required_cookie(cookie):
                print(
                    "[error] 该 Cookie 缺 BDUSS, 服务端必然判为未登录: 请粘贴完整的 "
                    "Cookie 串(含 BDUSS)。"
                )
            else:
                print(
                    "[error] Cookie 形态不完整也会导致 401: 请在浏览器 F12 → Network 里"
                    "点一条 dumate.baidu.com 的请求, 复制 Request Headers 里的【完整】"
                    "Cookie 串(含 BDUSS、BIDUPSID、BAIDUID 等), 而不是只粘贴 BDUSS 一段。"
                )
        else:
            print(
                "[error] Cookie 形态完整, 说明是服务端不认可这个会话: 请在浏览器确认该账号"
                "仍是登录状态, 重新登录后重新抓取完整 Cookie(同一浏览器、同一账号)。"
            )
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


def do_account_checkin(
    label: str, cookie: str, url_override: str, method: str, session: requests.Session
) -> bool:
    """
    单个账号的签到流程: 先做 Cookie 形态闸门, 再调用签到接口。

    缺 BDUSS 时不发请求, 直接给出可读错误(省一次必然失败的 401)。
    """
    print(f"[info] 凭据形态: {describe_cookie(cookie)}")
    for problem in validate_cookie(cookie):
        print(f"[warn] {label} 的 Cookie 形态可疑: {problem}")

    if not has_required_cookie(cookie):
        print(
            f"[error] {label} 的 Cookie 缺少 BDUSS, 服务端一定判为未登录; "
            "请粘贴浏览器里的完整 Cookie 串(含 BDUSS)。",
            file=sys.stderr,
        )
        return False

    return do_checkin(cookie, url_override, method, session)


# ---------------------------------------------------------------- 入口

def main() -> int:
    url_override = env("DUMATE_CHECKIN_URL")
    method = env("DUMATE_CHECKIN_METHOD", DEFAULT_METHOD).upper()
    if method not in ("GET", "POST"):
        print(f"[error] DUMATE_CHECKIN_METHOD 仅支持 GET/POST, 当前值: {method}", file=sys.stderr)
        return 2

    accounts = resolve_accounts_detailed()
    if not accounts:
        return 1

    notify_url = env("DUMATE_NOTIFY_URL")
    print(f"[info] 开始百度搭子每日签到 | 时间: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    if env("DUMATE_COOKIES") and env("DUMATE_COOKIE"):
        print(
            "[info] 检测到 DUMATE_COOKIES 与 DUMATE_COOKIE 同时配置: 两份凭据都会签到"
            "(按 Cookie 去重); 若只想用其中一份, 请把另一个 Secret 清空。"
        )
    print(f"[info] 待签到账号数: {len(accounts)}")

    session = requests.Session()
    results: List[Tuple[str, str, bool]] = []
    for idx, (label, cookie, source) in enumerate(accounts, 1):
        print(f"[info] ======== 账号 {idx}/{len(accounts)}: {label} ========")
        print(f"[info] 凭据来源: {source}")
        ok = do_account_checkin(label, cookie, url_override, method, session)
        results.append((label, source, ok))
        print(f"[{'done' if ok else 'failed'}] {label} 签到{'成功' if ok else '失败'}")
        if idx < len(accounts):
            # 轻微间隔, 降低并发触发的风控概率
            time.sleep(2)

    success = [label for label, _, ok in results if ok]
    failed = [label for label, _, ok in results if not ok]
    total = len(results)

    lines = [f"共 {total} 个账号: 成功 {len(success)}, 失败 {len(failed)}"]
    lines.extend(
        f"{'[OK]' if ok else '[FAIL]'} {label} (凭据来源: {source})"
        for label, source, ok in results
    )
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