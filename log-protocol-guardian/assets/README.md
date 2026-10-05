# assets

产出物模板，直接复制到目标项目后按项目实际 area 集合调整。

| 文件 | 用途 |
|---|---|
| `logproto.template.py` | 协议模块模板，含受控area 枚举、`normalize_stage()`、`format_line()`、`side_of()` |
| `test_logproto.template.py` | 回归测试模板，镜像解析侧正则断言一致性 |

## 使用方式

```bash
# 1. 协议模块放入项目核心层
cp assets/logproto.template.py <项目>/core/logproto.py

# 2. 回归测试放入测试目录并按实际 area 集合调整
cp assets/test_logproto.template.py <项目>/tests/test_logproto.py
```

## 必须调整的两处

1. **area 枚举**：按项目实际语义增删，同步修改前端声明的区域集合，
   两侧集合必须相等（由测试断言）。
2. **账号标识形态**：默认接受 `account_` 前缀。若项目用其他命名，
   修改 `logproto.py` 中的 `_ACCOUNT_RE` 与测试中的对应断言。

## 不要改动的部分

- `format_line()` 的三段式输出格式 —— 解析侧正则依赖其结构
- `normalize_stage()` 的降级行为 —— 未知输入必须降级而非抛异常