# aether-search 增强方案（v3 · 最终形态）

> 基座：`C:\Users\a\Desktop\项目开发\aether-search`（v0.1.0，5 个轻依赖）
> 移植来源：`C:\Users\a\Desktop\项目开发\GrokSearch`（v1.3.3，MIT）
> v3 定稿要点：第二答案通道定为 **80 行 OpenAI 工具调用循环**（零框架、零新增依赖）；保留 plan×6 与 get_sources；砍掉 switch_model；全文档按"简单直接"瘦身。

---

## 一、目标（三句话）

1. **Grok 不再是单点**：Grok 通道多端点主备容灾；同时 Aux 升级为第二答案通道（LLM 自主工具调用循环：搜 → 看结果 → 追问/抓全文 → 写带引用答案），Grok 失败时自动顶替。
2. **补齐工具面、替代 Claude 内置搜索**：移植 web_fetch / web_map / get_sources / plan×6 / toggle_builtin_tools。
3. **去品牌、零新增付费服务**：剥离 guda 署名；第二通道全部复用已配置的 Aux LLM 与 9 个搜索后端。

---

## 二、架构

```
search(query, mode="full", platform="", sources_mode="inline")

  阶段1 · 双答案通道并发（各带独立超时）
    ├─ grok 通道 ── 端点列表 [xAI, OpenRouter, 自建中转…]
    │               sequential 主备切换（默认）｜ race 全端点竞速
    │               ─► 带内联引用答案
    └─ agent 通道（缺后端或缺 LLM 时静默禁用）
         LLM 工具调用循环（max_steps 封顶，默认 6）：
           可用工具 = search_multi（现有 9 后端扇出+RRF）
                    + fetch_page（仅当 Tavily/Firecrawl 已配置才注册）
           LLM 自主决定：搜什么 / 搜几轮 / 抓哪页全文 / 何时收手写答案
           ─► 带 [N] 引用答案（引用全部来自真实检索结果）

  阶段2 · 核验并发（跨通道互证）
    verify(grok 引用,  证据索引 ∪ agent 引用)
    verify(agent 引用, 证据索引 ∪ grok 引用)
    probe(展示链接)                          # 死链探测

  阶段3 · 装配
    主答案(grok) + ✓⚠? 核验标记
    → grok 失败：agent 顶替为主；两者皆失败：证据链接列表兜底
    → grok 成功：agent 仅出摘要行（N 引用 / M 条互证 ✓），省 token
    → 所有信源（双通道引用+证据）统一去重入 SourcesCache，返回 session_id
    → 状态行：各通道成败/耗时/端点/agent 步数
```

**mode**：`full`（默认，双通道并发）/ `grok` / `agent` / `aux`（仅链接列表，**现版行为逐字不变**）。

**降级矩阵**：

| Grok key | Agent 材料（≥1 后端 + Aux LLM 三件套） | full 行为 |
|---|---|---|
| ✓ | ✓ | 双答案并发 + 引用互证 |
| ✓ | ✗ | 等同现版 grok 单通道 |
| ✗ | ✓ | agent 顶替为主（目标兜底场景） |
| ✗ | ✗ | 报错提示配置 |

---

## 三、模块设计

### 3.1 `channels.py`（新增）

```python
@dataclass
class Endpoint:
    url: str; key: str; model: str = ""; label: str = ""

@dataclass
class AnswerResult:
    channel: str                 # "grok" | "agent"
    text: str                    # 带引用答案
    citations: list[str]
    detail: str                  # 端点标签 / agent 步数，进状态行
    elapsed: float
    error: str | None
```

**GrokChannel**（现 `grok_provider.py` 重构并入，现有实现原样保留：SSE 解析 + 非流式兜底、`_WaitRetryAfter`、xAI Live Search 参数、OpenRouter `:online`、时间注入）+ 新增多端点调度：
- `_sequential`（默认）：按序逐端点，单端点内保留 tenacity 重试，全灭判失败
- `_race`：全端点并发，首个成功 cancel 其余（显式选择，README 注明多耗备用端点额度）

**AgentChannel**（核心 ~80 行，零框架）：

```python
async def answer(self, query, platform="") -> AnswerResult:
    msgs = [{"role": "system", "content": RESEARCH_SYSTEM},
            {"role": "user", "content": query}]
    tools = [SEARCH_MULTI_SPEC] + ([FETCH_PAGE_SPEC] if fetcher.configured() else [])
    for step in range(settings.agent_max_steps):                 # 默认 6
        resp = await self.llm.complete_with_tools(msgs, tools)   # llm.py 新增方法，openai SDK 原生
        if not resp.tool_calls:                                  # LLM 收手 → 答案
            return AnswerResult("agent", resp.content, extract_urls(resp.content),
                                f"{step+1} steps", ...)
        msgs.append(resp)
        for call in resp.tool_calls:                             # 工具并发执行
            msgs.append(tool_result(call))                       # search_multi: 扇出+RRF 证据块
    return 强制收尾(现有证据)                                      # 超步数兜底
```

- `SEARCH_MULTI_SPEC`：包装现有 `AuxAgent` 扇出 + RRF，返回"标题/URL/摘要/来源后端"证据块——LLM 看到的就是 9 后端的链接 + 结果
- `FETCH_PAGE_SPEC`：包装 `fetcher.fetch`，仅 Tavily/Firecrawl 已配置时注册（未配则 LLM 根本看不到此工具）
- RESEARCH_SYSTEM 提示词要点：多角度检索、每条论断标 [N]、证据不足明说不编造、输出 Markdown + Sources 列表
- 复用 `AETHER_LLM_*` 三件套（`llm.py` 的 OpenAI 兼容客户端），不引入新服务

### 3.2 `config.py`（扩展，新增 6 项）

| 字段 | 默认 | 说明 |
|---|---|---|
| `grok_endpoints` | `""` | JSON `[{"url","key","model?","label?"}]`；设置即覆盖 `grok_api_url/key`，留空走旧字段 |
| `grok_failover` | `"sequential"` | `sequential` / `race` |
| `agent_max_steps` | `6` | agent 循环步数上限 |
| `agent_timeout` | `90.0` | agent 通道整通道超时（超时用已有证据强制收尾） |
| `fetch_timeout` | `60.0` | web_fetch 单服务超时 |
| `scrape_retry` | `3` | Firecrawl 空内容重试 |

现有字段零改动；`sources_cache_size` 复用现有规模（256）。模型解析：`AETHER_GROK_MODEL` env > 默认（switch_model 已删，无持久化层）。

### 3.3 `verify.py`（签名泛化）

```python
async def verify_urls(urls: list[str], corroboration: set[str]) -> dict[str, tuple[str, str]]
```

印证集装配：grok 引用 ↔ `norm(证据索引) ∪ agent 引用`；agent 引用 ↔ `norm(证据索引) ∪ grok 引用`。两个独立来源引同一 URL → `CORROBORATED`。探测/标记体系不变。

### 3.4 `sources.py`（移植 GrokSearch，零依赖）

- `split_answer_and_sources`：四格式兜底解析信源块（`sources([...])` 函数式 / `## Sources` 标题式 / `<details>` 块 / 尾部链接块）
- `SourcesCache`（LRU + asyncio.Lock）、`merge_sources`（URL 去重）、`new_session_id`
- **两通道遵从**：search 结束统一把 grok 引用 + agent 引用 + 证据索引合并入缓存，**每条附核验结论**（✓/⚠/?，比 GrokSearch 多这一维）；响应恒含 `session_id`
- `sources_mode="inline"`（默认）：内联输出照旧 + 尾部带 session_id；`"session"`：正文只留答案 + 核验块，信源全走 `get_sources(session_id)` 拉取

### 3.5 `planning.py`（移植 GrokSearch，零外部依赖）

6 pydantic 模型 + `PlanningEngine`（会话状态机、累积/合并/覆盖三策略、复杂度 1–3 级决定必需阶段）原样移植；`tool_hint` 枚举改为 `search | web_fetch | web_map`。**全通道遵从**：客户端规划后按 `executable_plan` 调度三个执行工具。

### 3.6 `fetcher.py`（新增，请求格式来自 GrokSearch 已验证代码）

- `tavily_extract`：`POST {url}/extract` body `{"urls":[url],"format":"markdown"}` → `results[0].raw_content`
- `firecrawl_scrape`：`POST {url}/scrape` body `{"url","formats":["markdown"],"timeout":60000,"waitFor":递增}`，空内容重试
- `tavily_map`：`POST {url}/map` → `{base_url, results, response_time}`
- 复用现有 `tavily_api_key/url`、`firecrawl_api_key/url`，不新增 key 字段

### 3.7 `server.py`（重构）— 12 工具 + 进程增强

| # | 工具 | 说明 |
|---|---|---|
| 1 | `search` | `query, mode="full", platform="", sources_mode="inline"` |
| 2 | `get_sources` | `session_id` → 带核验标记的信源列表 |
| 3 | `web_fetch` | Tavily Extract → Firecrawl Scrape 降级 |
| 4 | `web_map` | Tavily Map |
| 5 | `toggle_builtin_tools` | 改项目级 `.claude/settings.json` 的 `permissions.deny` 增删 WebFetch/WebSearch（移植，无品牌） |
| 6 | `get_config` | 现有配置查看 + 并入连接诊断（逐端点测 `/models`：耗时/模型数/可用性，key 脱敏） |
| 7–12 | `plan_intent` / `plan_complexity` / `plan_sub_query` / `plan_search_term` / `plan_tool_mapping` / `plan_execution` | 规划脚手架 |

- 所有工具**不带** `meta.author`；配置报错纯提示（"请配置 AETHER_GROK_API_URL/KEY"），无外链
- `main()` 增强（移植）：SIGINT/SIGTERM 处理 + Windows 父进程监控（防僵尸 MCP 进程）
- `agent.py`：`run()` 抽出 `run_queries()` 入口供 agent 循环复用，`mode=aux` 行为不变

### 3.8 去品牌清单

工具 meta 署名全删；报错引导改纯提示；README 重写无外链；代码注释保留移植出处（MIT 合规要求，非推广）；建议新增 MIT LICENSE 并注明移植来源（由你定）。

---

## 四、成本与依赖

- **零新增付费服务**：agent 通道复用已有后端配额与 Aux LLM；增量仅 full 模式下的 LLM 工具循环 token（步数封顶 6）
- **零新增依赖**：工具调用走 aether 已有的 openai SDK；sources/planning 零依赖（pyproject 显式补 `pydantic>=2` 防悬空）
- 延迟：full 模式 = max(grok 10–30s, agent 15–50s)；赶时间用 `mode=grok`（现版速度）

## 五、文件变更

**新增 4**：`channels.py`、`fetcher.py`、`sources.py`、`planning.py`
**修改 7**：`server.py`（12 工具 + 编排 + main 增强）、`config.py`（6 新字段）、`verify.py`（互证签名）、`agent.py`（抽 `run_queries`）、`llm.py`（+`complete_with_tools`）、`.env.example`、`README.md`
**删除 1**：`grok_provider.py`（并入 channels.py）
**不动**：`fusion.py`、9 连接器、`docs/BACKENDS.md`
版本 → `0.2.0`；`test_smoke.py` 扩展。

## 六、风险

| 风险 | 对策 |
|---|---|
| agent 答案质量取决于 LLM 端点 | README 建议 GPT-4o-mini 级及以上；提示词强制"证据不足明说" |
| LLM 端点不支持 tool calling | `get_config` 诊断探测；不支持时 agent 通道禁用并提示（grok/aux 不受影响） |
| full 模式变慢（30–50s 档） | mode 分层：grok 模式保持现速；agent_timeout 硬封顶 + 超时强制收尾 |
| race 多耗端点额度 | 默认 sequential，README 注明 |
| plan 工具膨胀工具列表 | 描述标注"复杂调研前可选"，非强制 |
| 移植许可 | MIT 允许；保留出处注释 + 建议加 LICENSE |

## 七、实施步骤

| 步 | 内容 | 验收 |
|---|---|---|
| 1 | config.py 6 新字段 + 端点 JSON 解析 | 旧 .env 行为不变 |
| 2 | channels.py（GrokChannel 多端点 + AgentChannel 循环）+ llm.py `complete_with_tools` + verify.py 互证 + agent.py 抽入口 | 无 key 实例化不报错；mode=aux 冒烟不变 |
| 3 | server.py 双通道编排 + 装配降级 | test_smoke 过 |
| 4 | fetcher.py + web_fetch/web_map | 双 key 未配返回明确错误 |
| 5 | sources.py + get_sources + sources_mode | 四格式解析断言过 |
| 6 | planning.py + plan×6 | 三复杂度流转断言过 |
| 7 | toggle_builtin_tools + get_config 诊断 + main() 增强 | deny 增删正确 |
| 8 | .env.example + README + 冒烟扩展 + v0.2.0 | 全量冒烟通过 |

## 八、验证

1. 无 key 冒烟：双通道禁用、mode=aux 回归、工具循环超步数收尾、sources 四格式、planning 流转、端点解析兼容、RRF、fetcher 错误路径
2. `pip install -e .`（环境执行时定：base/yw/新建）
3. 真实 key 联调：full 双答案 + 互证标记；主端点坏 key → sequential 切备；grok 全挂 → agent 顶替；web_fetch 降级链；web_map；session 拉信源带核验标记；toggle 三态；plan 全流程
4. 共存：同环境 grok-search MCP 保留，aether 侧 Tavily/Firecrawl 留空 → fetch 工具不注册、扇出跳过，无额度双花
