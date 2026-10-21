# 百度搭子 DuMate 每日自动签到

通过 GitHub Actions 每天定时自动签到**百度搭子(DuMate)**，领取每日免费积分，全程免费、无需本地运行。

**支持多账号**：一次运行可为多个账号依次签到，并汇总每个账号的结果；任一账号失败时可通过 Server 酱推送微信提醒。

## 工作原理

```
GitHub Actions (cron 定时)
        │  每天 UTC 01:05 触发(北京时间 09:05)
        ▼
运行 Python 脚本 (src/checkin.py)
        │  认证优先级: ① 多账号 Cookie  ② 单账号 Cookie  ③ 账号密码自动登录
        ▼
逐个账号调用百度搭子签到接口
        │
        ▼
汇总每个账号结果(并可推送 Server 酱/Webhook 通知)
```

## 目录结构

```
baidu-dumate-checkin/
├── .github/
│   └── workflows/
│       └── daily-checkin.yml   # GitHub Actions 定时工作流
├── src/
│   ├── checkin.py              # 签到主脚本(多账号 + 认证 + 接口探测 + 签到)
│   ├── baidu_login.py          # 百度账号密码自动登录模块
│   └── capture_helper.py       # 本地抓包辅助脚本(替代手工 Fiddler/Charles)
├── requirements.txt            # Python 依赖(requests + rsa)
├── .env.example                # 环境变量示例(勿提交真实凭据)
├── .gitignore
└── README.md
```

## 快速开始(推荐: 多账号 Cookie)

### 1. 配置 GitHub Secrets

在 GitHub 仓库打开 **Settings → Secrets and variables → Actions → New repository secret**，添加：

| Secret 名称 | 是否必需 | 说明 |
| --- | --- | --- |
| `DUMATE_COOKIES` | 推荐 | 多账号 Cookie，一次签多个账号（填写格式见下） |
| `DUMATE_COOKIE` | 兼容 | 单账号 Cookie，未配置 `DUMATE_COOKIES` 时生效 |
| `DUMATE_USERNAME` | 回退 | 百度账号（仅未配置任何 Cookie 时生效，CI 可能触发风控） |
| `DUMATE_PASSWORD` | 回退 | 百度密码（与 `DUMATE_USERNAME` 成对） |
| `DUMATE_CHECKIN_URL` | 可选 | 签到接口地址(未填时脚本自动探测) |
| `DUMATE_CHECKIN_METHOD` | 可选 | 请求方法 `GET` 或 `POST`，默认 `POST` |
| `DUMATE_NOTIFY_URL` | 可选 | 通用通知 Webhook(Server酱旧版/PushPlus/企业微信机器人等) |
| `SCT_SEND_KEY` | 可选 | [Server 酱](https://sct.ftqq.com/) SendKey，失败时推送微信通知 |

### 2. `DUMATE_COOKIES` 填写格式

只需一个 Secret 即可放入多个账号，脚本会依次为每个账号签到。

**方式 A：多行文本（最直观）** —— 每行一个账号，推荐写成 `账号标签|Cookie`：

```
主号|BDUSS=xxxxx; STOKEN=yyyyy; ...
小号|BDUSS=aaaaa; STOKEN=bbbbb; ...
```

不写标签也可以，脚本会自动编号为「账号1、账号2…」：

```
BDUSS=xxxxx; STOKEN=yyyyy; ...
BDUSS=aaaaa; STOKEN=bbbbb; ...
```

**方式 B：JSON 数组**：

```
["BDUSS=xxxxx; STOKEN=yyyyy", "BDUSS=aaaaa; STOKEN=bbbbb"]
```

带自定义名称：

```
[
  {"name": "主号", "cookie": "BDUSS=xxxxx; STOKEN=yyyyy"},
  {"name": "小号", "cookie": "BDUSS=aaaaa; STOKEN=bbbbb"}
]
```

> 同一个 Cookie 重复填写会被自动去重；只配 `DUMATE_COOKIE` 时等价于一个账号，脚本向后兼容。

#### 如何获取某个账号的 Cookie

1. 用该账号在浏览器登录 [https://dumate.baidu.com](https://dumate.baidu.com)；
2. 按 `F12` 打开开发者工具 → **Network（网络）** 标签；
3. 刷新页面，随便点开一个请求 → **Headers → Request Headers → Cookie**；
4. 复制整段 Cookie（通常包含 `BDUSS`、`STOKEN` 等），作为该账号的 Cookie 填入。

对每个账号重复上述步骤，一行一个写入 `DUMATE_COOKIES`。

### 3. 配置 Server 酱微信提醒（可选）

若希望**任一账号签到失败时**收到微信通知，可启用 [Server 酱](https://sct.ftqq.com/)：

1. 打开 [https://sct.ftqq.com/](https://sct.ftqq.com/)，微信扫码登录；
2. 复制页面上的 **SendKey**（格式如 `SCTxxxxT`）；
3. 在 GitHub Secrets 新增 `SCT_SEND_KEY`，填入该 SendKey。

> 提醒内容会列出本次每个账号的成功/失败情况，例如 `共 3 个账号: 成功 2, 失败 1`。

### 4. 推送代码并验证

把工程推送到 GitHub 仓库后，两种方式立即验证：

- 在 **Actions** 页面找到 `Baidu Duzi Daily Check-in` 工作流 → **Run workflow** 手动触发一次；
- 或等待第二天 cron 自动触发。

查看运行日志：Actions → 对应工作流 → 最新一次运行 → `执行百度搭子每日签到` 步骤。

## 自抓包能力说明(重要)

GitHub Actions 是无界面的 CI 环境，**无法真正运行抓包工具**(Fiddler/Charles 需要在本地图形界面操作)。因此工程把"自抓包"实现为等价的自动探测方案：

1. **接口自动探测**：未配置 `DUMATE_CHECKIN_URL` 时，脚本会按顺序尝试内置的候选签到接口，找到返回成功特征的地址即认定有效并完成签到；
2. **本地辅助抓包**：若候选接口都失效，可在**自己电脑上**运行 `src/capture_helper.py` 完成"探测接口"，把结果填入 Secrets：

```bash
pip install -r requirements.txt
# 已有 Cookie: 直接探测接口
python src/capture_helper.py --cookie "手动Cookie字符串"
# 脚本会把探测结果写入 dummy_capture_result.json, 并提示应填入哪些 Secret
```

> 说明：`capture_helper.py` 输出文件只包含接口地址，不包含 Cookie 内容；探测结果用于填写 `DUMATE_CHECKIN_URL`。

## 认证模式与回退

| 场景 | 行为 |
| --- | --- |
| 配置了 `DUMATE_COOKIES` | 依次为列出的每个账号签到，并汇总每个账号的结果 |
| 只配置 `DUMATE_COOKIE` | 签到单个账号 |
| 只配置账号密码 | 自动登录后签到单个账号；被验证码/风控拦截时报错退出 |
| 无任何凭据 | 报错退出(退出码 1)，可通过 Server 酱/Webhook 获知 |

百度账号触发**图形/滑块验证码或风控**时无法在 CI 环境自动通过(这是百度服务端的安全策略，无法绕过)，因此**推荐使用 Cookie 模式**：在本机已登录的浏览器中抓取请求头里的 `Cookie`(通常含 `BDUSS`、`STOKEN`)。

## 退出码

| 退出码 | 含义 |
| --- | --- |
| `0` | 全部账号签到成功或已签过 |
| `1` | 存在账号签到失败 / 认证失败 / 缺少必需凭据 |
| `2` | 参数错误(如 `DUMATE_CHECKIN_METHOD` 取值非法) |

## 本地运行(调试)

```bash
pip install -r requirements.txt

# 方式一: 使用 .env(需先复制 .env.example 并填值)
set -a && source .env && set +a
python src/checkin.py

# 方式二: 直接传环境变量(单账号示例)
DUMATE_COOKIE="BDUSS=xxx; STOKEN=yyy" python src/checkin.py

# 多账号(多行文本)
DUMATE_COOKIES="主号|BDUSS=xxx
小号|BDUSS=aaa" python src/checkin.py
```

## 配置 GitHub Actions 定时时间(可选)

编辑 `.github/workflows/daily-checkin.yml` 中的 cron 表达式(默认 `5 1 * * *`，即 UTC 01:05 / 北京时间 09:05)。GitHub Actions 的 cron 使用 **UTC 时区**，例如要北京时间 08:00 触发，应填写 `0 0 * * *`。

## 常见问题

| 现象 | 原因与处理 |
| --- | --- |
| 日志提示 `缺少凭据` | 未配置 Secrets，或 Secret 名写错(注意大小写) |
| 只有部分账号成功 | 失败账号多为 Cookie 过期；日志与 Server 酱提醒会标出失败账号，更新对应的 Cookie 即可 |
| HTTP 401 或提示 `登录已经过期` | 该账号 Cookie 已过期，重新抓取并更新 `DUMATE_COOKIES` 中对应行 |
| 提示 `所有候选接口均未确认成功` | 未配置真实接口；在本机运行 `capture_helper.py` 抓取后填 `DUMATE_CHECKIN_URL` |
| 工作流未按时触发 | 检查 cron 是否为 UTC 时区、仓库是否处于活跃状态 |

## 安全说明

- Cookie 与账号密码都属于敏感凭据，**只**应存放在 GitHub Secret(`Settings → Secrets → Actions`)中，写入后 GitHub 不会明文展示；
- 多账号 Cookie 同样放在 `DUMATE_COOKIES` 这一个 Secret 中，请勿写入代码或提交到仓库；
- 本仓库的 `.gitignore` 已忽略 `.env*` 和 `dummy_capture_result.json`，提交前请确认未误传；
- 建议定期修改密码并留意账号登录设备记录，降低凭据泄露风险。

## 免责声明

本项目仅用于个人学习与自动化研究。百度搭子积分规则、登录与签到接口可能随时调整，脚本不保证长期有效；请遵守百度服务协议，使用风险由使用者自行承担。