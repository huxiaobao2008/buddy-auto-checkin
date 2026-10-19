# 一句话安装（把这段整段复制给你的 AI 助手）

> 本项目的安装方式为「交给 AI 执行」：你不需要读完整 README，也不需要手动敲命令。
> 把下面**提示词区**里的内容整段复制，发给 WorkBuddy / CodeBuddy / 任何能执行命令的 AI 助手即可。

---

## 提示词区（从这里开始复制 ↓）

```
请帮我安装并配置这个开源项目：https://github.com/huxiaobao2008/buddy-auto-checkin

它是一个 WorkBuddy 桌面端的自动签到与成长任务工具（Windows，Python 3.9+，零第三方依赖）。
请按下面的要求完成安装，并在每一步之后简要告诉我结果。

【前置检查】
1. 确认系统是 Windows 10/11，确认有 Python 3.9+（没有就告诉我，不要擅自装）。
2. 确认 WorkBuddy 桌面客户端已安装且当前已登录（没登录就提示我先登录，然后停下等我）。

【安装步骤】
1. 把仓库克隆到用户目录下，例如 %USERPROFILE%\buddy-auto-checkin。
2. 复制配置模板：把 config.example.json 复制成 config.json。
3. 执行 python auto_buddy.py --discover  扫描本机可用登录态，把结果告诉我。
4. 执行 python auto_buddy.py --capture account_a  把当前登录的账号存为凭证。
5. 执行 python auto_buddy.py --check  只查询不领取，把体检结果告诉我，等我确认无误。
6. 我确认后再执行 python auto_buddy.py --install-scheduler  注册 Windows 计划任务。

【定时时间】
默认按 config.json 里 accounts[].times 的时间点执行（模板为 08:00）。
如果我要改时间，修改 config.json 里对应的 times 数组后重新执行 --install-scheduler。

【安全红线】（必须遵守）
- 不要在任何对话、命令输出或日志里显示、复制、上传我的 Token、Cookie 或登录态文件内容。
- 不要把 accounts/ 目录、config.json、logs/ 目录里的内容粘贴到对话里。
- 不要执行任何"领取付费任务 / 捐款 / 充值"相关的操作。

【验收标准】
- --check 能正确列出我的账号与当前状态。
- 计划任务已注册（可用 --list 或任务计划程序确认）。
- 明确告诉我：每天几点跑、跑哪些账号、日志在哪里看。
```

## 提示词区（到这里结束 ↑）

---

## 安装之后

| 想做的事 | 命令 |
|---|---|
| 看账号列表与状态 | `python auto_buddy.py --list` |
| 只查询不领取（体检） | `python auto_buddy.py --check` |
| 手动跑一次 | `python auto_buddy.py --run --force` |
| 打开网页工作台 | `python auto_buddy.py --ui` |
| 看积分与过期台账 | `python credit_report.py` |
| 续期所有账号凭证 | `python auto_buddy.py --capture-all` |
| 删除计划任务 | `python auto_buddy.py --remove-scheduler` |

凭证有效期约 28 天，到期前跑一次 `--capture-all` 即可。

## 为什么要用这种方式安装

本项目的安装涉及登录态扫描、账号凭证抓取、计划任务注册等多个步骤，手动执行容易漏序。
交给 AI 执行时，它会逐步确认前置条件并在每步后汇报，出错可以当场追问——比照着 README 敲命令更稳。

如果你更愿意手动安装，完整的命令说明见 [README.md](README.md) 的「快速开始」。
