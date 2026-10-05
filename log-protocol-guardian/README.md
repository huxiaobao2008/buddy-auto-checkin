# 日志协议守护（log-protocol-guardian）

> 统一「日志怎么写」与「日志怎么读」，用可执行测试锁死前后端一致性。

## 定位

面向「后端写日志 + 前端或下游解析日志」的两端架构，把分散在多处的
日志格式定义收敛为唯一协议模块，消除静默失效的日志分流与状态误判。

**触发场景**：日志都挤在一个区域、API 与 UI 日志串区、已完成却显示已停止、
反复执行后日志文件打不开、前端正则匹配不到日志。

**不适用**：无解析侧的纯写入型日志（如独立告警日志、启动器日志），
这类日志无area 语义，不属协议管辖范围。

## 目录结构

```
log-protocol-guardian/
├── SKILL.md                    技能主文件（七步执行流程 + 故障速查）
├── references/
│   └── domain-notes.md         设计原则、五类故障模式、迁移 checklist、反模式
├── scripts/
│   └── check_log_protocol.py   协议一致性校验器（零依赖，退出码可入 CI）
└── assets/
    ├── logproto.template.py协议模块模板
    └── test_logproto.template.py  回归测试模板
```

## 核心能力

1. **协议收敛** —— 格式定义唯一化，未知输入降级不抛异常
2. **双向迁移** —— 写入点与解析点同时迁移，含向后兼容的归一处理
3. **回归锁定** —— 镜像解析侧正则的测试，必须包含旧逻辑会失败的用例

## 校验器用法

```bash
python scripts/check_log_protocol.py <项目根目录>
```

退出码：`0` 干净，`1` 发现问题，`2` 参数错误。可直接接入 CI 阻断回归。

## 使用方式

将 `log-protocol-guardian/` 目录放入 `~/.workbuddy/skills/`，重启客户端或执行重载。

## 上传开放平台

打包为 root 直铺 zip（SKILL.md 位于 zip 根目录）后上传：

```bash
cd log-protocol-guardian && zip -r ../log-protocol-guardian.zip . -x '.*'
```

## 来源

本技能的全部故障模式、判据与修复方案，均来自真实项目治理过程
（WorkBuddy 多账号自动化工程，`log-protocol-guardian` v1.0.0），
每条均可复现与验证，非虚构示例。