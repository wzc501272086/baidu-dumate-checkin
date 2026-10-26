#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
百度账号密码登录模块(passport.baidu.com 通行证协议)
====================================================

作用:
  使用百度账号+密码自动完成登录, 获取 BDUSS / STOKEN 等会话 Cookie,
  供百度搭子(DuMate)签到请求使用, 免去手动抓取 Cookie 的步骤。

实现说明:
  - 通过已实测验证的 https://passport.baidu.com/v2/getpublickey 获取 RSA 公钥;
  - 密码以 RSA 公钥加密后提交至 /v2/api/?login 登录接口;
  - 登录成功后读取 BDUSS, 再调用 getsession 换取 STOKEN 等附加 Cookie;
  - token 参数按百度通行证多版本接口尝试获取(新版登录已不再强制要求)。

限制与注意事项(重要):
  - 百度通行证协议未公开, 且风控策略(图形/滑块验证码、频控)随时调整;
    部分账号在无头 CI 环境可能无法自动通过验证码, 此时代码会抛出清晰异常,
    建议回退"手动 Cookie"模式(见 README);
  - 若登录接口返回"验证码"类错误, 说明该账号触发了风控, 无法绕过;
  - 本模块仅面向自己账号的自动化签到场景, 请遵守百度服务协议。
"""

from __future__ import annotations

import base64
import json
import time
from typing import Dict, List, Optional

import requests

# 说明: rsa 仅在账号密码登录(encrypt_password)时按需导入。
# 手动 Cookie 签到不需要该依赖, 因此不在此处做模块级导入,
# 便于在未安装 rsa 的环境里直接运行/测试签到主流程。

PASSPORT_BASE = "https://passport.baidu.com"
PUBLIC_KEY_URL = PASSPORT_BASE + "/v2/getpublickey"
LOGIN_URL = PASSPORT_BASE + "/v2/api/?login"
GETSESSION_URL = PASSPORT_BASE + "/v2/api/?getsession"
LOGIN_TEMPLATE = "mn"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
DEFAULT_TIMEOUT = 30


class BaiduLoginError(Exception):
    """百度登录失败."""


def _headers(extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Referer": "https://www.baidu.com/",
    }
    if extra:
        headers.update(extra)
    return headers


def _now_ms() -> str:
    return str(int(time.time() * 1000))


def get_public_key(session: requests.Session) -> str:
    """获取登录所用的 RSA 公钥(已实测接口有效)."""
    resp = session.get(PUBLIC_KEY_URL, headers=_headers(), timeout=DEFAULT_TIMEOUT)
    resp.raise_for_status()
    try:
        body = resp.json()
    except ValueError:
        # 百度 getpublickey 实际返回单引号 JSON(JSONP 风格), 兼容解析
        text = resp.text
        try:
            cleaned = text.replace("'", '"')
            body = json.loads(cleaned)
        except ValueError as exc:
            raise BaiduLoginError(
                f"获取 RSA 公钥失败(响应无法解析): {text[:300]}"
            ) from exc

    if body.get("errno") not in ("0", 0):
        raise BaiduLoginError(
            f"获取 RSA 公钥失败: {json.dumps(body, ensure_ascii=False)[:500]}"
        )
    pubkey = body.get("pubkey", "")
    if not pubkey:
        raise BaiduLoginError("获取 RSA 公钥失败: 响应缺少 pubkey 字段")
    return pubkey


def get_login_token(session: requests.Session) -> str:
    """
    尝试获取登录 token(百度新版接口已不再强制, 失败返回空串即可).

    说明: getapi 接口响应格式随前端版本变化, 此处采样并兼容多版本;
    若拿不到 token, 登录请求仍会发送(tok 参数留空)。
    """
    try:
        url = PASSPORT_BASE + "/v2/api/"
        params = {
            "getapi": "",
            "tpl": LOGIN_TEMPLATE,
            "apiver": "v3",
            "tt": _now_ms(),
            "class": "login",
        }
        resp = session.get(url, params=params, headers=_headers(), timeout=DEFAULT_TIMEOUT)
        resp.raise_for_status()
        body = resp.json()
        data = body.get("data") or {}
        token = data.get("token") or ""
        # token 为占位错误串时视为不可用
        if token and "string type" not in token and "the" not in token:
            return token
    except (requests.RequestException, ValueError):
        pass
    return ""


def encrypt_password(pubkey_pem: str, password: str) -> str:
    """使用服务端 RSA 公钥加密密码(百度通行证流程)."""
    try:
        import rsa
    except ImportError as exc:
        raise BaiduLoginError(
            "缺少依赖 rsa: 账号密码自动登录需要它; 如只用手动 Cookie 签到可以忽略。"
            "安装方式: pip install rsa"
        ) from exc

    try:
        pub = rsa.PublicKey.load_pkcs1_openssl_pem(pubkey_pem.encode("utf-8"))
    except Exception as exc:
        raise BaiduLoginError("无法解析百度登录 RSA 公钥") from exc

    cipher = rsa.encrypt(password.encode("utf-8"), pub)
    return base64.b64encode(cipher).decode("ascii")


def login_with_password(
    username: str,
    password: str,
    session: Optional[requests.Session] = None,
) -> Dict[str, str]:
    """
    使用账号密码登录百度, 返回完整 Cookie 字典(BDicts/BDUSS/STOKEN 等).

    成功后返回的 cookies 可直接用于签到请求; 失败抛 BaiduLoginError。
    """
    if not username or not password:
        raise BaiduLoginError("账号或密码为空")

    session = session or requests.Session()
    pubkey = get_public_key(session)
    token = get_login_token(session)
    password_enc = encrypt_password(pubkey, password)

    data = {
        "username": username,
        "password": password_enc,
        "token": token,
        "tpl": LOGIN_TEMPLATE,
        "apiver": "v3",
        "isPhone": "false",
        "detect": "1",
        "quick_user": "0",
        "logtype": "dialogLogin",
        "staticpage": "https://www.baidu.com/cache/user/html/v3Jump.html",
        "u": "https://www.baidu.com/",
        "bdash": "1",
    }

    resp = session.post(
        LOGIN_URL,
        data=data,
        headers=_headers({"Referer": "https://www.baidu.com/"}),
        timeout=DEFAULT_TIMEOUT,
    )
    resp.raise_for_status()
    try:
        body = resp.json()
    except ValueError:
        body = {"raw": resp.text[:500]}

    # 需要验证码/风控时 errno 为相应错误码, 无法自动通过
    if body.get("errno") not in ("0", 0):
        msg = json.dumps(body, ensure_ascii=False)[:800]
        lower = msg.lower()
        if "verify" in lower or "captcha" in lower or "风控" in msg:
            raise BaiduLoginError(
                "百度账号触发了图形/滑块验证码或风控, 无法在 CI 环境自动通过; "
                "请改用手动 Cookie 模式(见 README 第 2 节)。"
            )
        raise BaiduLoginError(f"百度登录失败: {msg}")

    cookies: Dict[str, str] = {
        k: v for k, v in session.cookies.get_dict().items() if v is not None
    }
    if "BDUSS" not in cookies:
        detail = json.dumps(body, ensure_ascii=False)[:800]
        raise BaiduLoginError(f"登录响应未带 BDUSS Cookie: {detail}")

    # 换取 STOKEN(用于后续保持会话)
    try:
        r2 = session.get(
            GETSESSION_URL,
            params={"tpl": LOGIN_TEMPLATE, "apiver": "v3", "tt": _now_ms()},
            headers=_headers(),
            timeout=DEFAULT_TIMEOUT,
        )
        r2.raise_for_status()
        extra = {
            k: v for k, v in session.cookies.get_dict().items() if v is not None
        }
        cookies.update(extra)
    except requests.RequestException:
        # STOKEN 获取失败不致命, BDUSS 已足够完成签到
        pass

    return cookies


def cookies_to_header(cookies: Dict[str, str]) -> str:
    """把 Cookie 字典序列化为请求头 Cookie 字符串."""
    return "; ".join(f"{k}={v}" for k, v in cookies.items())


def cookie_names(header_text: str) -> List[str]:
    """解析 Cookie 字符串里的键名(调试用)."""
    names = []
    for part in header_text.split(";"):
        if "=" in part:
            names.append(part.strip().split("=", 1)[0])
    return names