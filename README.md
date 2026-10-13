# 百度搭子 DuMate 每日自动签到

通过 GitHub Actions 每天定时自动签到**百度搭子(DuMate)**，领取每日免费积分，全程免费、无需本地运行。

支持两种认证方式，**推荐只填账号密码**，脚本自动登录、自动探测签到接口，无需手动抓包。

## 工作原理

```
GitHub Actions (cron 定时)
        │  每天 UTC 01:05 触发(北京时间 09:05)
        ▼
运行 Python 脚本 (src/checkin.py)
        │  优先级: ① 账号密码自动登录  ② 手动 Cookie
        ▼
自动探测/调用百度搭子签到接口
        │
        ▼
输出结果日志(并可推送 Webhook 通知)
```

## 目录结构

```
baidu-dumate-checkin/
├── .github/
│   └── workflows/
│       └── daily-checkin.yml   # GitHub Actions 定时工作流
├── src/
│   ├── checkin.py              # 签到主脚本(认证 + 接口探测 + 签到)
│   ├── baidu_login.py          # 百度账号密码自动登录模块
│   └── capture_helper.py       # 本地抓包辅助脚本(替代手工 Fiddler/Charles)
├── requirements.txt            # Python 依赖(requests + rsa)
├── .env.example                # 环境变量示例(勿提交真实凭据)
├── .gitignore
└── README.md
```

## 快速开始(推荐: 账号密码自动登录)

### 1. 配置 GitHub Secrets

在 GitHub 仓库打开 **Settings → Secrets and variables → Actions → New repository secret**，添加：

| Secret 名称 | 是否必需 | 说明 |
| --- | --- | --- |
| `DUMATE_USERNAME` | 推荐 | 百度账号(手机号/用户名) |
| `DUMATE_PASSWORD` | 推荐 | 百度密码(与 USERNAME 成对) |
| `DUMATE_COOKIE` | 备用 | 手动 Cookie(仅在账号登录不可用时需要) |
| `DUMATE_CHECKIN_URL` | 可选 | 签到接口地址(未填时脚本自动探测) |
| `DUMATE_CHECKIN_METHOD` | 可选 | 请求方法 `GET` 或 `POST`，默认 `POST` |
| `DUMATE_NOTIFY_URL` | 可选 | 通知 Webhook(Server酱/PushPlus/企业微信机器人) |

> 填 `DUMATE_USERNAME` + `DUMATE_PASSWORD` 即完成最小配置。脚本运行时会通过百度通行证接口自动登录、获取会话凭据(Bduss/Stoken)，然后签到，**全程无需手动抓包**。

### 2. 推送代码并验证

把工程推送到 GitHub 仓库后，两种方式立即验证：

- 在 **Actions** 页面找到 `Baidu Duzi Daily Check-in` 工作流 → **Run workflow** 手动触发一次；
- 或等待第二天 cron 自动触发。

查看运行日志：Actions → 对应工作流 → 最新一次运行 → `执行百度搭子每日签到` 步骤。

## 自抓包能力说明(重要)

GitHub Actions 是无界面的 CI 环境，**无法真正运行抓包工具**(Fiddler/Charles 需要在本地图形界面操作)。因此工程把"自抓包"实现为等价的自动探测方案：

1. **自动登录**：脚本自己调用百度通行证接口完成登录，拿到会话凭据(替代"手动抓 Cookie")；
2. **接口自动探测**：未配置 `DUMATE_CHECKIN_URL` 时，脚本会按顺序尝试内置的候选签到接口，找到返回成功特征的地址即认定有效并完成签到；
3. **本地辅助抓包**：若候选接口都失效，可在**自己电脑上**运行 `src/capture_helper.py` 一键完成"登录 + 探测接口"，把结果填入 Secrets：

```bash
pip install -r requirements.txt
# 用账号密码: 自动登录并探测接口
python src/capture_helper.py --username 你的百度账号 --password 你的密码
# 或已有 Cookie: 直接探测接口
python src/capture_helper.py --cookie "手动Cookie字符串"
# 脚本会把探测结果写入 dummy_capture_result.json, 并提示应填入哪些 Secret
```

> 说明：`capture_helper.py --username/--password` 不会保存或上传你的密码，只在本地内存中完成一次登录探测；输出文件只包含接口地址，不包含 Cookie 内容。

## 认证模式与回退

| 场景 | 行为 |
| --- | --- |
| 配置了账号密码 | 优先自动登录；登录被验证码/风控拦截时，自动回退手动 Cookie |
| 只有 Cookie | 直接用手动 Cookie 签到 |
| 登录失败且无 Cookie | 报错退出(退出码 1)，可通过通知 Webhook 获知 |

百度账号触发**图形/滑块验证码或风控**时无法在 CI 环境自动通过(这是百度服务端的安全策略，无法绕过)，此时建议改用手动 Cookie 模式：在本机已登录的浏览器/客户端中抓取请求头里的 `Cookie`(通常含 `BDUSS`、`STOKEN`)，填入 `DUMATE_COOKIE`。

## 本地运行(调试)

```bash
pip install -r requirements.txt

# 方式一: 使用 .env(需先复制 .env.example 并填值)
set -a && source .env && set +a
python src/checkin.py

# 方式二: 直接传环境变量
DUMATE_USERNAME="你的账号" DUMATE_PASSWORD="你的密码" python src/checkin.py
```

## 配置 GitHub Actions 定时时间(可选)

编辑 `.github/workflows/daily-checkin.yml` 中的 cron 表达式(默认 `5 1 * * *`，即 UTC 01:05 / 北京时间 09:05)。GitHub Actions 的 cron 使用 **UTC 时区**，例如要北京时间 08:00 触发，应填写 `0 0 * * *`。

## 常见问题

| 现象 | 原因与处理 |
| --- | --- |
| 日志提示 `缺少凭据` | 未配置 Secrets，或 Secret 名写错(注意大小写) |
| 提示 `触发了图形/滑块验证码或风控` | 百度账号安全策略，无法自动绕过；改用手动 Cookie 模式 |
| HTTP 401/403 或提示认证失败 | Cookie 已过期，更新 `DUMATE_COOKIE` 或重新登录 |
| 提示 `所有候选接口均未确认成功` | 未配置真实接口；在本机运行 `capture_helper.py` 抓取后填 `DUMATE_CHECKIN_URL` |
| 工作流未按时触发 | 检查 cron 是否为 UTC 时区、仓库是否处于活跃状态 |

## 安全说明

- 账号密码和 Cookie 都属于敏感凭据，**只**应存放在 GitHub Secret(`Settings → Secrets → Actions`)中，写入后 GitHub 不会明文展示；
- 本仓库的 `.gitignore` 已忽略 `.env*` 和 `dummy_capture_result.json`，提交前请确认未误传；
- 建议定期修改密码并留意账号登录设备记录，降低凭据泄露风险。

## 免责声明

本项目仅用于个人学习与自动化研究。百度搭子积分规则、登录与签到接口可能随时调整，脚本不保证长期有效；请遵守百度服务协议，使用风险由使用者自行承担。