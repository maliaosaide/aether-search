# aether-search

[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/) [![FastMCP](https://img.shields.io/badge/FastMCP-2.0.0+-green.svg)](https://github.com/jlowin/fastmcp)

**双通道搜索 MCP：Grok 主检索 + LLM 编排的多后端辅助检索。**

---

## 一、概述

aether-search 是基于 [FastMCP](https://github.com/jlowin/fastmcp) 的 MCP 服务器，采用双通道并行架构：

- **Grok 通道（主）**：调用任意 OpenAI 兼容端点（xAI 官方 / OpenRouter / 自建代理）做 LLM 原生网络搜索，输出带内联引用的 Markdown 答案。
- **Aux 通道（辅）**：用一个 OpenAI 兼容 LLM 改写查询，并发扇出到 9 个搜索后端，结果经 RRF 融合，可选 LLM 摘要。

两通道并行执行。Grok 答案中的引用 URL 与 aux 索引交叉印证，未印证的做存活探测，标记死链与潜在幻觉。

```
Client ──MCP──► aether-search
                 ├─ grok ──► OpenAI 兼容端点 ─► 带引用答案
                 │              │
                 │              └─► 引用核验（aux 印证 / HEAD 探测）
                 │
                 └─ aux
                     ├─ LLM 改写 → 查询变体
                     ├─ 并发 fan-out → Parallel · Brave · Exa · Serper · Linkup · You.com · Sonar · Tavily · Firecrawl
                     └─ RRF 融合 →（可选）LLM 摘要
```

### 功能特性

- **双通道并行**：`full` / `grok` / `aux` 三种模式，单通道失败自动降级。
- **9 个高质量搜索后端**（Parallel / Brave / Exa 为第一梯队）：配哪个启用哪个，未配置静默跳过；Aux 未配置任何后端时自动退化为仅 Grok 通道，不强行兜底。
- **查询改写**：LLM 生成多角度子查询，`economy` / `full` 两种扇出策略。
- **RRF 融合**：跨后端按 Reciprocal Rank Fusion 去重排序。
- **引用核验**：aux 索引印证（免费）+ HEAD/Range-GET 存活探测，识别 404/410 幻觉链。
- **鲁棒性**：SSE 流式 + 非流式兜底解析、`Retry-After` 指数退避、整通道超时。

## 二、安装

前置：Python 3.11+，以及任意 MCP 客户端（Claude Code / Cherry Studio 等）。

```bash
cd aether-search
pip install -e .
```

或使用 [uv](https://docs.astral.sh/uv/)：

```bash
uv pip install -e .
```

安装后获得入口命令 `aether-search`。

## 三、配置

复制 `.env.example` 为 `.env`，填入要启用的 key。**原则：配哪个启用哪个，未配的静默跳过，绝不影响运行。**

### Grok 主通道（OpenAI 兼容）

| 变量 | 说明 |
|---|---|
| `AETHER_GROK_API_URL` | 端点，如 `https://api.x.ai/v1`、`https://openrouter.ai/api/v1` |
| `AETHER_GROK_API_KEY` | API key |
| `AETHER_GROK_MODEL` | 模型，默认 `grok-4`（OpenRouter 自动追加 `:online`） |
| `AETHER_GROK_SEARCH_MODE` | xAI Live Search：`auto` / `on` / `off` / `none`，留空在 api.x.ai 上为 `auto`，其他端点省略该字段 |

### Aux 查询 agent LLM（OpenAI 兼容）

| 变量 | 说明 |
|---|---|
| `AETHER_LLM_API_BASE` | 端点 base url |
| `AETHER_LLM_API_KEY` | API key |
| `AETHER_LLM_MODEL` | 模型名 |
| `AETHER_LLM_REWRITE` | 查询改写，默认 `true` |
| `AETHER_LLM_DIGEST` | 融合结果摘要，默认 `false`（额外消耗 token） |

### 搜索后端

`AETHER_<NAME>_API_KEY` + 可选 `AETHER_<NAME>_API_URL`，NAME 取值：`PARALLEL` / `BRAVE` / `EXA` / `SERPER` / `LINKUP` / `YOUCOM` / `SONAR` / `TAVILY` / `FIRECRAWL`。URL 均有官方默认值，走代理时才需覆写。未配置任何后端 key 时 Aux 通道不启用，仅走 Grok。

> Tavily / Firecrawl：若同环境的 `grok-search` 已在消耗其额度，此处留空即可。

### 并发 / 融合 / 核验

| 变量 | 默认 | 说明 |
|---|---|---|
| `AETHER_FANOUT` | `economy` | `economy`：原查询×全部后端 + 变体轮询；`full`：变体×后端全交叉。multi_query 后端（Parallel）总是一次吃全部变体 |
| `AETHER_MAX_VARIANTS` | `3` | 含原查询的子查询数 |
| `AETHER_TOP_K` | `10` | 单后端单次返回条数 |
| `AETHER_MAX_CONCURRENCY` | `8` | 并发扇出上限 |
| `AETHER_VERIFY` | `true` | 是否核验 Grok 引用 URL |

## 四、接入 MCP 客户端

MCP stdio 配置通用 JSON 格式如下（所有 stdio 客户端一致，按需删减 `env` 字段）：

```json
{
  "mcpServers": {
    "aether-search": {
      "command": "aether-search",
      "args": [],
      "env": {
        "AETHER_GROK_API_URL": "https://api.x.ai/v1",
        "AETHER_GROK_API_KEY": "sk-...",
        "AETHER_GROK_MODEL": "grok-4",
        "AETHER_LLM_API_BASE": "https://api.openai.com/v1",
        "AETHER_LLM_API_KEY": "sk-...",
        "AETHER_LLM_MODEL": "gpt-4o-mini",
        "AETHER_LLM_REWRITE": "true",
        "AETHER_BRAVE_API_KEY": "...",
        "AETHER_EXA_API_KEY": "...",
        "AETHER_PARALLEL_API_KEY": "...",
        "AETHER_FANOUT": "economy",
        "AETHER_MAX_VARIANTS": "3",
        "AETHER_TOP_K": "10",
        "AETHER_VERIFY": "true"
      }
    }
  }
}
```

要点：

- `command` 用安装后的入口 `aether-search`；若未加入 PATH，改为 Python 直接启动：`"command": "python"`，`"args": ["-m", "aether_search.server"]`。
- `env` 只填要启用的项，未列出等价于未配置（静默跳过）。**布尔与数值必须写字符串**（`"true"`、`"10"`），JSON env 只接受字符串。
- 最低可用配置：仅 `AETHER_GROK_API_URL` + `AETHER_GROK_API_KEY` 即可跑（自动等同 `grok` 模式）。启用 Aux 需至少一个搜索后端 key + Aux LLM 三件套（`API_BASE`/`API_KEY`/`MODEL`）才会改写，否则 Aux 不执行、仅 Grok 出结果。

### 各客户端放置位置

| 客户端 | 方式 |
|---|---|
| Claude Code（项目级，团队共享） | 项目根新建 `.mcp.json`，写入上方 JSON，提交到仓库 |
| Claude Code（用户级，仅自己） | `claude mcp add aether-search -- aether-search`，或编辑 `~/.claude.json` |
| Cherry Studio | 设置 → MCP 服务 → 新建，粘贴 JSON（或分别填 command / args / env） |
| 其他 stdio 客户端 | 同格式，写入其对应的 mcp 配置文件 |

## 五、支持哪些查询 / MCP 工具

### `search(query, mode="full", platform="")`

任意自然语言网络搜索。底层 Grok 会推断真实意图、多角度并行检索、优先权威源（官方文档 / 学术 / 主流媒体），每条结论内联来源 URL；Aux 通道则用 LLM 拆解查询并扇出到多个搜索 API 取原始链接。

| 参数 | 类型 | 说明 |
|---|---|---|
| `query` | str | 任意自然语言，中英文均可。Grok 默认英文检索以获取更优覆盖，主题需要时自动切中文 |
| `mode` | str | `full`（默认）/ `grok` / `aux`，见下表 |
| `platform` | str | 让 Grok 聚焦的平台，如 `"GitHub"`、`"GitHub, Reddit"`、`"arxiv"`、`"Stack Overflow"`；留空不限定 |

**三种模式**

| mode | 行为 | 适用场景 |
|---|---|---|
| `full` | Grok + Aux 并行：Grok 给综合答案，Aux 融合补充原始链接，并对 Grok 引用做核验 | 默认，最大覆盖与可信度 |
| `grok` | 仅 Grok 通道，返回带引用的 AI 综合答案 | 只需结论、省 token / 省 API 调用 |
| `aux` | 仅 Aux 通道：LLM 改写 → 多后端扇出 → RRF 融合，返回原始链接列表 | 需要可点击的来源列表、Grok 不可用或额度吃紧 |

**查询改写**：Aux 通道中 LLM 把原查询拆为最多 `AETHER_MAX_VARIANTS` 个不同角度的子查询（同义词 / 子主题 / 不同范围），保留原查询语言，再并发投递到各后端。

**返回内容**：Grok 带引用答案 + 引用核验标记（✓ 印证或存活 / ⚠ 死链，可能幻觉 / ? 不可达或被反爬）+ Aux 融合结果（可选 LLM 摘要）+ 通道状态行（耗时、命中数、各通道成败）。Grok 失败时 Aux 自动提升为主体。

### `get_config()`

返回脱敏后的当前配置状态（key 仅显示首尾 4 位），用于排查哪些后端实际启用。

## 六、工作原理

1. **并行执行**：grok / aux 作为两个 `asyncio` 任务并行，各有整通道超时（默认 grok 60s，aux 25s）。
2. **Aux 流水线**：LLM 改写 → `_assigned` 按后端类型分配变体 → 各后端 `search_all` 并发扇出（Parallel 一次吃全部变体）→ RRF 融合。
3. **降级链**：Grok 失败 → aux 提升为主体；aux 无任何后端 → 仅 grok；两者皆不可用 → 报错。
4. **核验分层**：Tier 0 aux 索引印证（跳过探测）；Tier 1 HEAD 探测，405/501 回退 Range-GET，404/410 判死链，其余不可达判为 unverified（反爬不等于不存在）。

## 七、验证

```bash
python test_smoke.py
```

无 key 环境下验证：连接器注册、改写降级、economy 规划、RRF 融合、Aux 无后端时不执行。

## 八、状态

v0.1.0。依赖：`fastmcp`、`httpx`、`openai`、`pydantic-settings`、`tenacity`。
