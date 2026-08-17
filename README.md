# M365-Copilot-API

> 我有 M365 账号，可以用 Copilot Chat 的 GPT-5.6-deepthink 模型。
> 但我更希望用 Codex CLI 这类 Agent 产品来充分发挥它的能力——所以有了这个项目。

[English](README.en.md)

> **⚠️ 重要声明**
>
> 本项目是 **非官方工具**，与微软无关。通过协议兼容的方式对接 M365 Copilot Chat 的消费者网页端点。
>
> 使用前请务必结合你的实际使用场景，仔细阅读[已知限制](#已知限制)和 [DISCLAIMER](DISCLAIMER.md)，评估对你所在组织的信息安全影响。确保你的使用方式符合公司政策，且不涉及敏感数据泄露风险。
>
> 详见 [DISCLAIMER](DISCLAIMER.md) 和 [MIT 许可证](LICENSE)。

## 故事

我是 M365 用户，日常用 Copilot Chat 写代码、做分析。GPT-5.6-deepthink 的推理能力很强，但网页版 Copilot 的交互方式限制了它的发挥：

- 写代码写到一半，想请教 Copilot，得从编辑器切到浏览器、复制粘贴
- 跑 CI/CD 的时候，想让它帮忙分析一下构建日志，没法直接接入
- 重复性的数据分析任务，每次都要手动操作，没法自动化
- 更不用说 Agent 那种"帮我规划、执行、迭代"的体验了

我真正想要的是：**用 Codex CLI 这类 Agent 产品来调用 M365 的模型能力**。但官方没有给 API，怎么办？

于是有了这个项目——它把 M365 Copilot Chat 背后的 WebSocket 协议转成了标准的 OpenAI API。你在浏览器里能做的事情，现在通过 API 也能做，而且可以用你喜欢的 Agent 工具。

```
你的 Agent 工具 → OpenAI SDK → localhost:8000 → M365 Copilot 云端
```

## 开箱即用

```bash
git clone https://github.com/imxiaorong/M365-Copilot-API.git
cd M365-Copilot-API
./install.sh
copilot login
python app.py
```

四步完成。`install.sh` 会自动配置 [cc-switch](https://github.com/farion1231/cc-switch) 的 provider，打开 cc-switch → Codex → 选择 "M365 Copilot" 即可使用，无需任何额外配置。

## 场景

**如果你和我一样：**
- 有 M365 账号，能用 Copilot Chat
- 觉得网页版 Copilot 交互不够灵活
- 想用 Codex CLI、OpenAI SDK 或其他 Agent 工具来调用模型
- 不想为了一个 API 去申请 Azure 资源、走审批流程

那这个项目就是为你准备的。

## 能力

### 🔌 OpenAI 兼容接口
标准的 Chat Completions + Responses API，支持流式输出。任何 OpenAI 兼容客户端直接连。

```python
from openai import OpenAI
client = OpenAI(base_url="http://localhost:8000/v1", api_key="unused")

resp = client.chat.completions.create(
    model="m365-copilot",
    messages=[{"role": "user", "content": "用 Python 写一个快排"}],
)
print(resp.choices[0].message.content)
```

### 🧩 Codex CLI 原生支持
原生支持 Responses API 的全部 SSE 事件流，Codex CLI 切换 `wire_api = "responses"` 后无需任何额外代理。

### 🔄 自动续期
M365 的 token 有效期 60-75 分钟，项目内置了自动刷新机制，登录一次持续使用，无需反复操作。

### 🎯 多 tone 选择
支持 Thinker、Fast、Creative、Precise、Balanced 等多种 Copilot 模式，按需切换。

## 模型名对照

| 请求用的 model 名 | Copilot 后端 tone | 说明 |
|---|---|---|
| `m365-copilot` | `Gpt_5_6_Reasoning` | 默认，Thinker |
| `m365-copilot-thinker` | `Gpt_5_6_Reasoning` | 显式 Thinker |
| `m365-copilot-fast` | `Magic` | 快速模式，非推理 |
| `m365-copilot-creative` | `Creative` | 创意模式 |
| `m365-copilot-precise` | `Precise` | 严谨模式 |
| `m365-copilot-balanced` | `Balanced` | 平衡模式 |

## 项目结构

```
m365copilot/        ← 核心库
├── auth.py          token 缓存 / 过期检查
├── browser.py       Playwright 登录 + 抓 token
├── protocol.py      SignalR 协议帧构造
├── driver.py        WebSocket Chathub 驱动
├── silent_refresh.py  Entra 刷新令牌交换（无浏览器）
└── client.py        高层 API（chat/stream/对话续接）

server/             ← FastAPI 服务
├── api.py            OpenAI 兼容接口（Chat Completions + Responses）
├── prompt.py         消息转 Copilot 提示词
├── responses_format.py  Responses API 适配
├── schemas.py        Pydantic 请求模型
├── keepalive.py      后台 token 刷新器
└── config.py         环境变量配置

tools/              ← 调试工具
├── capture_m365.py     协议抓包
├── diag_headless.py    诊断 token 捕获问题
└── inspect_tokens.py   查看 MSAL token 缓存
```

## 和类似方案的关系

这个项目最初基于 [Windows-Copilot-API](https://github.com/sums001/Windows-Copilot-API.git) 改造，但做了几个关键升级：

| 对比项 | 原始项目 | 本项目的改进 |
|--------|---------|------------|
| 目标平台 | 个人版 Copilot | M365 企业版 Copilot |
| 模型能力 | 个人版，较弱 | GPT-5.6-deepthink，更强 |
| 配额 | 受个人账号限制 | 随 M365 许可证，配额更高 |
| API 协议 | Chat Completions | Chat Completions + Responses API |
| 后台刷新 | 无 | 自动 token 刷新 |
| cc-switch 集成 | 无 | 一键配置 |

## 安装

### 一键安装

```bash
./install.sh
```

自动完成：venv → 依赖 → Playwright Chromium → 全局 `copilot` 命令 → cc-switch 配置。

### 手动安装

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium
python -m m365copilot login
```

## Codex CLI 集成

### 通过 cc-switch（推荐）

`install.sh` 会自动注册 "M365 Copilot" 的 Codex provider。打开 cc-switch → Codex → 选择即可。

### 手动配置

添加到 `~/.codex/config.toml`：

```toml
[profiles.m365]
model = "m365-copilot"
model_provider = "m365"

[model_providers.m365]
name = "M365 Copilot"
base_url = "http://localhost:8000/v1"
wire_api = "responses"
env_key = "M365_KEY"
```

## 已知限制

- **M365 账号不能并行** — 一个账号同时只能处理一个对话，请求会串行排队
- **单对话上限 600 条消息** — 从 Copilot 的限流帧获取
- **日配额看许可证** — 查看 completion 帧里的 `throttling.metering` 字段
- **条件访问策略** — 如果公司限制了非托管设备登录，Playwright 登录可能失败

## 常见问题

**Q: 需要有 M365 Copilot 许可证吗？**
A: 是的。需要你的 M365 账号有 Copilot 的访问权限。

**Q: 会触发风控吗？**
A: 项目模拟的是浏览器正常行为，默认限流 20 RPM，不会过度请求。

**Q: 和 Windows Copilot API 有什么区别？**
A: 本项目走的是企业版 M365 Copilot（m365.cloud.microsoft），模型更强、配额更高。

**Q: 支持流式输出吗？**
A: 支持。Chat Completions 和 Responses API 都支持 SSE 流式。

**Q: 需要管理员权限吗？**
A: 不需要。只要你能在浏览器里登录 M365 Copilot，就能用。

