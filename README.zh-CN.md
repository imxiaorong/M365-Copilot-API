# M365 Copilot API — 免费平替 OpenAI API

> 把公司白给的 M365 Copilot 额度，变成标准的 OpenAI API。
> 零成本、零注册、零管理员审批，开箱即用。

## 这是什么？

你公司给你买了 Microsoft 365 E3/E5，但你还在自掏腰包充 OpenAI、充 Claude？

**M365 Copilot API** 是一个本地 API 服务，把 M365 Copilot Chat（`m365.cloud.microsoft/chat`）背后的 WebSocket 协议转成标准的 OpenAI 格式。你的 Codex CLI、OpenAI SDK、任何 OpenAI 兼容客户端，可以直接连它，**零成本**调用 GPT-5.6 Thinker。

```
你的代码 → OpenAI SDK → localhost:8000 → M365 Copilot 云端（公司买单）
```

**不需要**：
- ❌ 注册 OpenAI 账号
- ❌ 申请 Azure 应用注册
- ❌ 管理员同意 Graph API 权限
- ❌ 信用卡

**只需要**一个公司 M365 工作账号，登录一次，就能一直用。

## 核心卖点

### 🚀 GPT-5.6 Thinker，公司买单
M365 Copilot 背后用的是 **GPT-5.6 Thinker**（微软内部代号 `Gpt_5_6_Reasoning`），推理能力极强。你公司已经为它付了钱——只是默认只给了 Copilot Chat 网页版。这个项目把它变成 API 让你随便调。

### 🔌 原生兼容 OpenAI SDK
```python
from openai import OpenAI
client = OpenAI(base_url="http://localhost:8000/v1", api_key="unused")

resp = client.chat.completions.create(
    model="m365-copilot",
    messages=[{"role": "user", "content": "用 Python 写一个快排"}],
)
print(resp.choices[0].message.content)
```

### 🧩 原生支持 Responses API
Codex CLI v0.144+ 强制走 `wire_api = "responses"`？没问题。这个项目原生支持 Responses API 的全部 SSE 事件流，适配 Codex CLI 无需任何额外代理。

### 🔄 自动续期，无需操心
悉尼 token 有效期 60-75 分钟。项目内置了：
- 后台静默刷新（HTTP 层，不弹浏览器）
- 可见但隐藏的 Playwright 窗口策略（绕过 Entra 条件访问策略）
- JWT 过期检测，避免不必要的刷新

### 🎯 适合谁用
- **M365 打工人** — 公司有 E3/E5，想免费调用大模型
- **Codex CLI 用户** — 想用公司 M365 额度跑 Codex，不花自己钱
- **AI 工具开发者** — 需要稳定的 API 做测试、批量处理、自动化
- **大模型重度用户** — 月调用量大的，用自家公司额度不心疼

## 快速开始

```bash
git clone https://github.com/imxiaorong/M365-Copilot-API.git
cd M365-Copilot-API
./install.sh

# 首次登录（会弹浏览器，用公司账号登录）
copilot login

# 启动服务
python app.py
```

服务跑起来后，任何 OpenAI 兼容客户端直接连 `http://localhost:8000/v1` 就能用。

## 和竞品对比

| 方案 | 成本 | 注册 | 管理员审批 | 可用模型 |
|------|------|------|-----------|---------|
| OpenAI API | 💰💰💰 | 要 | 不 | GPT-4o / o1 |
| Claude API | 💰💰💰 | 要 | 不 | Sonnet / Opus |
| Azure OpenAI | 💰💰 | 要 | 要 | 同上 |
| **M365 Copilot API** | **免费** | **不** | **不** | **GPT-5.6 Thinker** |
| Windows Copilot API | 免费 | 不 | 不 | 个人版 Copilot |
| GitHub Copilot API | 免费 | 要 | 要 | GPT-4o |

> M365 Copilot 和 Windows Copilot 的区别：M365 版用的是企业额度，对话更聪明、配额更高。Windows Copilot 是个人版，体验差一大截。

## 项目结构

```
m365copilot/       ← 核心库
├── auth.py         token 缓存 / 过期检查
├── browser.py      Playwright 登录 + 抓 token
├── protocol.py     SignalR 协议帧 + 调用构造
├── driver.py       WebSocket Chathub 驱动
├── silent_refresh.py  Entra 刷新令牌交换（无浏览器）
└── client.py       高层 API（chat/stream/对话续接）

server/            ← FastAPI 服务
├── api.py           OpenAI 兼容接口
├── prompt.py        消息转 Copilot 提示词
├── responses_format.py  Responses API 适配
├── schemas.py       Pydantic 请求模型
├── keepalive.py     后台 token 刷新器
└── config.py        环境变量配置

tools/             ← 调试工具
├── capture_m365.py    协议抓包
├── diag_headless.py   诊断 token 捕获问题
└── inspect_tokens.py  查看 MSAL token 缓存
```

## 模型名对照

| 请求用的 model 名 | Copilot 后端 tone | 说明 |
|---|---|---|
| `m365-copilot` | `Gpt_5_6_Reasoning` | 默认，Thinker |
| `m365-copilot-thinker` | `Gpt_5_6_Reasoning` | 显式 Thinker |
| `m365-copilot-fast` | `Magic` | 快速模式，非推理 |
| `m365-copilot-creative` | `Creative` | 创意模式 |
| `m365-copilot-precise` | `Precise` | 严谨模式 |
| `m365-copilot-balanced` | `Balanced` | 平衡模式 |

## 已知限制

- **M365 账号不能并行** — 一个账号同时只能处理一个对话，请求会串行排队
- **单对话上限 600 条消息** — 从 Copilot 的限流帧获取
- **日配额看许可证** — 查看 completion 帧里的 `throttling.metering` 字段
- **条件访问策略** — 如果你的公司限制了非托管设备登录，Playwright 登陆可能失败

## 安装方式

### 一键安装（推荐）

```bash
./install.sh
```

自动完成：创建 venv → 安装依赖 → 下载 Playwright Chromium → 注册全局 `copilot` 命令 → 自动配置 cc-switch。

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

`install.sh` 会自动注册一个 "M365 Copilot" 的 Codex provider。打开 cc-switch → Codex → 选择 "M365 Copilot" 即可。

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

```bash
export M365_KEY=unused
alias codex-m365='codex --profile m365'
```

## 常见问题

**Q: 公司没有 M365 Copilot 许可证能用吗？**
A: 不能。需要你的 M365 账号有 Copilot 的访问权限。

**Q: 会封号吗？**
A: 这个项目模拟的是浏览器正常行为，不是暴力破解。只要不过度并发（默认限流 20 RPM），不会触发风控。

**Q: 和 Windows Copilot API 有什么区别？**
A: Windows Copilot API 走的是个人版 copilot.microsoft.com，额度低、模型弱。本项目的 M365 Copilot 走的是企业版 m365.cloud.microsoft，模型更聪明，配额更高。

**Q: 支持流式输出吗？**
A: 支持。Chat Completions 和 Responses API 都支持 SSE 流式。

**Q: 需要管理员权限吗？**
A: 不需要。只要你能在浏览器里登录 M365 Copilot，就能用。

## 法律声明

本项目是 **非官方工具**，与微软无关。通过反向工程实现的协议兼容，使用的是 M365 Copilot Chat 的消费者网页端点。请遵守你所在公司的可接受使用政策（Acceptable Use Policy）。

详见 [DISCLAIMER](DISCLAIMER.md) 和 [MIT 许可证](LICENSE)。

---

**如果觉得有用，点个 ⭐ 吧！** 你的 Star 是对开发者最大的鼓励。