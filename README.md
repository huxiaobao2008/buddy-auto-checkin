# buddy-auto-checkin

![Python](https://img.shields.io/badge/Python-3.9%2B-3776AB?logo=python&logoColor=white)
![Platform](https://img.shields.io/badge/Platform-Windows%2010%2F11-0078D6?logo=windows&logoColor=white)
![License](https://img.shields.io/badge/License-MIT-green)
![Zero deps](https://img.shields.io/badge/dependencies-none-brightgreen)
![Lines](https://img.shields.io/badge/code-16.8k%20lines-blueviolet)

WorkBuddy「Buddy 加油站」自动签到与成长任务工具。

> English: Multi-account auto check-in and growth-task automation for **WorkBuddy / CodeBuddy**
> desktop. Reads the local login session (no password ever stored), drives tasks via official
> API **and** CDP browser automation, with a Windows Task Scheduler trigger and a built-in web
> dashboard. Zero third-party dependencies.

读取本机客户端的登录态，为多个账号自动完成每日签到与成长任务，
支持 Windows 计划任务定时触发、桌面提醒与网页工作台。

> **只用本机已登录的客户端做自动化，不存储明文密码，不向任何第三方上传数据。**

---

## 一眼看懂：它能做什么

| | |
|---|---|
| **多账号统一调度** | 一个账号池（实测 20 个），串行切号、自动续期、断点续传（今天跑过的自动跳过） |
| **签到 + 成长任务全套** | 每日签到、成长任务接取/领取、成长热度点亮、开盲盒、派 Buddy 出行、领出行奖励 |
| **没有 API 的任务也做** | 通过 CDP 真实驱动客户端界面，补齐纯接口方案做不到的任务 |
| **网页工作台** | 实时进度、一键全自动、随时停止、切号、续期、发现新账号，浏览器里点着用 |
| **积分台账** | 不止告诉你"领了多少"，还告诉你**还剩多少、哪一批哪天几分过期、该先用哪个号** |
| **工程化防御** | 付费/捐款类任务一票否决只记不刷；401 抖动重试后才判死，不误杀正常账号 |

```bash
python auto_buddy.py --discover     # 扫描本机登录态
python auto_buddy.py --check        # 只查询不领取，先体检
python auto_buddy.py --ui           # 打开网页工作台
```

---

## 与同类项目的区别

社区已有若干单账号签到脚本（如 `88lin/workbuddy-auto-signin` 等）。差异不在功能多少，而在 **规模与工程化程度**：

| 维度 | 常见单账号脚本 | 本项目 |
|---|---|---|
| 账号规模 | 单账号 | **多账号池** + 切号调度 |
| 无 API 的任务 | 做不了 | CDP 驱动界面完成 |
| 工作台 | 命令行 + 日志 | 网页工作台 |
| 积分视角 | 领了多少 | 剩余 / 过期倒计时 / 优先消耗 |
| 付费任务拦截 | 无 | 一票否决 |
| 平台 | Win / macOS / Linux | 仅 Windows |

**我们不如别人的地方也写清楚**（详见 [`docs/开源同类项目对比.md`](docs/开源同类项目对比.md)）：
不能云端运行（依赖本机开机）、仅 Windows、暂无断登补签与连登兑换、1.7 万行多模块比单文件脚本上手重。

---

## 安全须知（请先读）

本仓库**不包含**任何账号凭据，也不应该包含。以下路径已被 `.gitignore` 永久忽略：

| 路径 | 说明 |
|---|---|
| `accounts/` | 账号凭证文件（`*.cred.json`）——**绝不入库** |
| `config.json` | 本地配置（含路径、提醒 webhook 等）——**绝不入库** |
| `logs/` `backup/` `data/` | 运行日志与本地状态 |
| `.workbuddy/` `.dumate/` `.tmp/` | 工具数据目录 |
| `index.js` `package.json` | 来自 WorkBuddy Desktop（腾讯产品），版权不属于本项目 |

首次使用请复制配置模板：

```bash
cp config.example.json config.json
```

---

## 快速开始

```bash
# 1) 扫描本机可用的登录态
python auto_buddy.py --discover

# 2) 把当前客户端登录的账号存为凭证
python auto_buddy.py --capture account_a

# 3) 只查询不领取（先体检，确认读到的东西是对的）
python auto_buddy.py --check

# 4) 执行到点的账号
python auto_buddy.py --run

# 5) 注册 Windows 计划任务（之后全自动）
python auto_buddy.py --install-scheduler
```

## 命令一览

| 命令 | 作用 |
|---|---|
| `--discover` | 扫描本机所有可用的登录态 |
| `--capture <KEY>` | 把当前客户端登录的账号存成该 KEY 的凭证 |
| `--capture-all` | 一键续期所有账号凭证 |
| `--list` | 列出配置账号与最近执行状态 |
| `--run` | 执行到点的账号（计划任务调用） |
| `--run --force` | 忽略时间窗，执行所有启用账号 |
| `--run --account <KEY>` | 只跑指定账号 |
| `--check` | 只查询不领取（体检用） |
| `--sync` | 扫登录态，新账号自动入库 |
| `--ui [--port 8765]` | 启动网页工作台 |
| `--install-scheduler` | 注册 Windows 计划任务 |
| `--remove-scheduler` | 删除计划任务 |
| `--add <UID>` | 把本机某个登录态账号加入列表 |
| `--remove <KEY>` | 从列表移除（凭证文件保留） |
| `--disable <KEY>` / `--enable <KEY>` | 停用 / 启用 |
| `--notify-test` | 测试提醒通道 |

**退出码**：`0` 全部成功或无需执行；`1` 存在失败；`2` 凭证失效或缺失。

## 目录结构

```
buddy-auto-checkin/
├── auto_buddy.py            主 CLI 入口
├── client_cdp.py            通过 CDP 与本机客户端通信
├── launcher.py              启动器
├── run_entry.py             计划任务入口
├── core/                    核心能力（登录态、签名、请求）
├── engine/                  任务执行引擎
├── orchestrator/            多账号编排
├── missions/                成长任务实现
├── tools/                   辅助工具
├── web/                     网页工作台
├── tests/                   测试
├── docs/                    架构与流程文档
└── log-protocol-guardian/   日志协议守护（独立子模块）
```

## 日志协议守护（log-protocol-guardian）

一个独立的小工具：统一「日志怎么写」与「日志怎么读」，用可执行测试锁死前后端一致性。

适用于：日志挤在一个区域、API 与 UI 日志串区、已完成却显示已停止、前端正则匹配不到日志。

详见 [`log-protocol-guardian/README.md`](log-protocol-guardian/README.md)。

## 积分台账（可用积分 + 过期倒计时）

积分是**按资源包各自到期**的，不是一个总数统一失效。本项目把这件事做成台账：

```bash
python credit_report.py          # 刷新全部账号的积分与过期数据
```

产出 `logs/credits_report.html`：当前可用积分、每一批的到期时刻与倒计时（精确到分钟）、
30 天过期时间轴、按账号的优先消耗建议。签到收尾时也会自动刷新（旁路，不影响签到结果）。

数据来源为官方 billing 接口（`get-user-resource-summary` / `get-user-resource-free-packages`），
非页面抓取；过期时间取资源包 `ExpiredTime` 与 `CycleEndTime` 中更早者。

## 环境要求

- Windows 10/11
- Python 3.9+
- 已安装并登录 WorkBuddy 桌面客户端

## 常见问题

**Q：会不会泄露我的账号？**
不会。仓库不含任何凭据，`accounts/`、`config.json`、`logs/`、`data/` 已被 `.gitignore` 永久忽略；
工具只在本地读取客户端已有的登录态，不存储明文密码，不向第三方上传任何数据。
发布前可用 `python tools/build_opensource.py` 生成脱敏包（手机号 / JWT 替换 + 出包校验）。

**Q：为什么需要客户端在线？**
部分成长任务没有开放接口，只能通过 CDP 驱动客户端界面完成，因此需要本机开机且客户端已登录。
纯接口任务不受此限制。

**Q：登录态失效怎么办？**
凭证有效期约 28 天。执行 `--capture-all` 或在工作台点「一键续期」即可批量刷新；
接口返回 401 时会重试确认后才判定失效，避免把临时抖动误判为作废。

**Q：多账号会不会有风险？**
本项目定位为「多账号统一管理」，用于管理本人名下账号。请遵守平台用户协议，
勿用于批量注册或任何违反服务条款的用途。

## 许可

MIT License —— 详见 [LICENSE](LICENSE)。

## 免责声明

本项目为个人自动化工具，仅用于在本人设备上操作本人账号。
请遵守相关平台的用户协议，勿用于任何违反服务条款的用途。
使用者需自行承担使用风险。
