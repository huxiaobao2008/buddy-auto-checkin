# buddy-auto-checkin

WorkBuddy「Buddy 加油站」自动签到与成长任务工具。

读取本机客户端的登录态，为多个账号自动完成每日签到与成长任务，
支持 Windows 计划任务定时触发、桌面提醒与网页工作台。

> **只用本机已登录的客户端做自动化，不存储明文密码，不向任何第三方上传数据。**

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

## 环境要求

- Windows 10/11
- Python 3.9+
- 已安装并登录 WorkBuddy 桌面客户端

## 许可

MIT License —— 详见 [LICENSE](LICENSE)。

## 免责声明

本项目为个人自动化工具，仅用于在本人设备上操作本人账号。
请遵守相关平台的用户协议，勿用于任何违反服务条款的用途。
使用者需自行承担使用风险。
