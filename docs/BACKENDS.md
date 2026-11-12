# 搜索后端申请与配置指南

> **数据查询日期：2026-07-27**。本指南中每一项额度与价格均来自各服务官方 pricing / docs 页面（一手），并在文末标注来源。**原则：配哪个用哪个，未配置的静默跳过，绝不影响运行。**
> aether-search 采用双通道架构：**Grok 主通道**（AI 综合答案）+ **Agent 通道**（10 个搜索后端 RRF 融合）。Agent 通道至少配一个主层后端 key 才会启用；不配置 Grok 时可独立运行。
> **DeepSeek Search / Fetch 于 2026-11-01 补充**，其接口与计费以对应小节的官方文档为准，其余条目的查询日期不变。

---

## 一、总览

| # | 服务 | 角色 | 免费额度 | 起步价（付费） | 申请入口 | 环境变量 |
|:-:|---|---|---|---|---|---|
| 0 | **xAI Grok** | 主通道（AI 答案） | 无（按量） | grok-4.5：$2/1M 入 + $6/1M 出；Live Search $0.025/次起 | `console.x.ai` | `AETHER_GROK_API_KEY` 等 |
| 1 | **You.com** | Aux | **$100 credit（≈20,000 次，无需信用卡）** | $5/1k calls | `you.com/platform` | `AETHER_YOUCOM_API_KEY` |
| 2 | **Parallel** | Aux（multi_query） | 5,000 次/月 + $5 月度额度 | $1/1k requests | `platform.parallel.ai` | `AETHER_PARALLEL_API_KEY` |
| 3 | **Linkup** | Aux | 4,000 次（**一次性**） | $0.005–0.006/次 | `linkup.so` | `AETHER_LINKUP_API_KEY` |
| 4 | **Serper** | Aux | 2,500 次（无需信用卡，6 个月有效） | $1/1k（Starter $50/50k） | `serper.dev` | `AETHER_SERPER_API_KEY` |
| 5 | **Tavily** | Aux | 1,000 credits/月（无需信用卡） | $0.008/credit | `app.tavily.com` | `AETHER_TAVILY_API_KEY` |
| 6 | **Firecrawl** | Aux | 1,000 credits/月（无需信用卡） | ~$0.00083/credit（Standard $83/月） | `firecrawl.dev` | `AETHER_FIRECRAWL_API_KEY` |
| 7 | **Brave** | Aux | $5 月度额度（≈1,000 次，**需信用卡反欺诈**） | $5/1k requests | `api.search.brave.com` | `AETHER_BRAVE_API_KEY` |
| 8 | **Exa** | Aux | **$10 credit/月**（可再生）+ 注册 $20 + Startup/Education $1,000 grant | $7/1k Search | `dashboard.exa.ai` | `AETHER_EXA_API_KEY` |
| 9 | **Perplexity Sonar** | Aux | **无免费层（需信用卡）** | Sonar：$1/1M 入 + $1/1M 出 + search $5/1k | `perplexity.ai`（Settings → API） | `AETHER_SONAR_API_KEY` |
| 10 | **DeepSeek** | Agent + 抓取 | 指南未列免费额度 | Search / Fetch 均为 ¥5/1k 次有结果的成功请求 | `platform.deepseek.com` | `AETHER_DEEPSEEK_API_KEY` |

> **免费额度排名**（建议优先配置顺序）：You.com $100 ≫ Parallel 5,000/月 > Linkup 4,000（一次性）> Serper 2,500 > Exa $10/月（≈1.4k Search）> Tavily 1,000/月 ≈ Firecrawl 1,000/月 ≈ Brave $5/月 > Sonar（无）。

### 图片检索支持

`search_images` 只向支持图片的后端发请求，未配置 key 的自动跳过：

| 后端 | 调用方式 | 返回字段 |
|---|---|---|
| Serper | `POST /images` | `imageUrl` / `thumbnailUrl` / `link` / `imageWidth` / `imageHeight` |
| Brave | `GET /images/search` | `properties.url`（原图）/ `thumbnail.src` / 来源页 `url` |
| Parallel | `POST /v1/images/search` | `image_url` / `source_page_url` / `width` / `height` |
| Tavily | `POST /search` + `include_images` | `images[].url` / `description` |
| Firecrawl | `POST /search` + `sources:["images"]` | `imageUrl` / `url` / `imageWidth` / `imageHeight` |
| Linkup | `POST /search` + `includeImages` | `type=image` 的 `name` / `url` |

Exa 的 `contents.extras.imageLinks` 属于网页配图抽取，不属于图片搜索，本项目未接入；You.com / DeepSeek / Sonar / Grok 无图片检索。

`search_images` 会扇出**所有已配置的图片后端**，包括被 `AETHER_FALLBACK_BACKENDS` 指定为补充层的后端——它是显式调用的独立工具，不走主层/补充层划分。若不希望某后端在图片检索时消耗额度，请不要配置其 key。

---

## 二、各后端详情

### 0. xAI Grok（主通道）

aether-search 的**主检索通道**，调用 Grok 做 LLM 原生网络搜索，输出带内联引用的 Markdown 答案。

- **定位**：任意 OpenAI 兼容端点（xAI 官方 / OpenRouter / 自建代理）
- **免费额度**：无，按量计费
- **价格（2026-07 最新）**：
  - `grok-4.5`（推荐，最新 flagship）：**$2.00/1M 输入 + $6.00/1M 输出**，context 500k，知识截止 2026-02-01
  - Live Search（按引用 source 计费）：1 source **$0.025/次**，4 sources 约 $0.05/次，随 source 数递增
  - 旧 `grok-4`：$3/1M 入 + $15/1M 出（更贵，建议改用 grok-4.5）
- **申请**：访问 `console.x.ai` 注册 → 充值 → 生成 API Key
- **配置**（`.env`）：
  ```bash
  AETHER_GROK_API_URL=https://api.x.ai/v1
  AETHER_GROK_API_KEY=xai-xxxxxxxx
  AETHER_GROK_MODEL=grok-4.5          # 推荐，比默认 grok-4 便宜得多
  AETHER_GROK_SEARCH_MODE=auto        # auto|on|off|none；api.x.ai 上 auto 即开 Live Search
  ```
- **注意**：OpenRouter 用户填 `https://openrouter.ai/api/v1`，model 自动追加 `:online`，`SEARCH_MODE` 留空。Grok 失败时 Aux 自动提升为主体。

---

### 1. You.com（免费额度最慷慨）

- **定位**：AI 原生 Web Search API，深度可调
- **免费额度**：**$100 免费额度（无需信用卡）**，按 $5/1k 计 ≈ **20,000 次免费搜索** —— 全榜最慷慨
- **价格**：Web Search **$5.00/1k calls**（每次 1-100 results 同价）；Contents $1/1k pages；Research API $12/1k；Finance Research $110/1k
- **申请**：访问 `you.com/platform` 注册 → 自动获 $100 credit → 在 dashboard 取 `YDC_API_KEY`
- **配置**：
  ```bash
  AETHER_YOUCOM_API_KEY=your_ydc_key
  # AETHER_YOUCOM_API_URL=https://api.you.com/v1   # 默认值，无需改
  ```
- **来源**：[you.com/pricing](https://you.com/pricing)、[you.com/docs](https://docs.you.com)

---

### 2. Parallel（Aux 第一梯队，multi_query 主力）

aether-search 里 Parallel 是 `multi_query` 后端——一次调用吃掉 AuxAgent 拆分的全部变体（objective + 多个 search_queries），发挥第一梯队质量。

- **定位**：面向 AI agent 的企业级 Web 检索（自有数百亿页索引，估值 $2B）
- **免费额度**：**5,000 requests/月 + $5 月度 recurring额度**（自动到账）+ 注册赠送 credit
- **价格**：web search **$1/1k requests 起**（按请求计费，非按 token）
- **申请**：访问 `platform.parallel.ai` 注册 → 取 API Key
- **配置**：
  ```bash
  AETHER_PARALLEL_API_KEY=parallel_xxxxxxxx
  # AETHER_PARALLEL_API_URL=https://api.parallel.ai/v1   # 默认值
  ```
- **来源**：[parallel.ai](https://parallel.ai)、[docs.parallel.ai/api-reference/search/search](https://docs.parallel.ai/api-reference/search/search)

---

### 3. Linkup

- **定位**：AI 原生搜索，Verified SimpleQA 92% F-score，亚秒级
- **免费额度**：**4,000 次查询**（一次性,用完转按量付费;官方定价页未标月度,2026-07-30 确认）
- **价格**：Search $0.005–0.006/次（Fast/Standard/Deep 三档）；Fetch $0.001–0.005/次；Research（异步）$0.25–2.50/次；Startup 可申请 $5,000 credit
- **申请**：访问 `linkup.so` → 登录 → dashboard 取 key
- **配置**：
  ```bash
  AETHER_LINKUP_API_KEY=linkup_xxxxxxxx
  # AETHER_LINKUP_API_URL=https://api.linkup.so/v1   # 默认值
  ```
- **来源**：[linkup.so/pricing](https://www.linkup.so/pricing)

---

### 4. Serper

- **定位**：纯 Google SERP 透传（质量 ≈ Google），1-2s 延迟
- **免费额度**：**2,500 次（无需信用卡）**，credits **6 个月有效**（一次性）
- **价格**（top-up 模式，无月订阅）：
  - Starter $50 / 50k（$1.00/1k）
  - Standard $375 / 500k（$0.75/1k）
  - Scale $1,250 / 2.5M（$0.50/1k）
  - Ultimate $3,750 / 12.5M（$0.30/1k）
- **退款**：7 天内且未用超 20% 全退
- **申请**：访问 `serper.dev` 注册 → 自动生成 API Key
- **配置**：
  ```bash
  AETHER_SERPER_API_KEY=your_serper_key
  # AETHER_SERPER_API_URL=https://google.serper.dev   # 默认值
  ```
- **来源**：[serper.dev](https://serper.dev)

---

### 5. Tavily

- **定位**：LLM 原生搜索，Agent Score 13.67（AIMultiple #5）
- **免费额度**：**1,000 credits/月（无需信用卡）**，月度可再生；学生免费
- **价格**：Pay As You Go **$0.008/credit**；Project 档 4,000 credits/月（价格滑块可调）；Enterprise 定制
- **申请**：访问 `app.tavily.com` 注册 → 取 `TVLY-API-KEY`
- **配置**：
  ```bash
  AETHER_TAVILY_API_KEY=tvly-xxxxxxxx
  # AETHER_TAVILY_API_URL=https://api.tavily.com   # 默认值
  ```
- **注意**：若同环境的 `grok-search` 已在消耗 Tavily 额度，此处留空即可。
- **来源**：[tavily.com/pricing](https://tavily.com/pricing)

---

### 6. Firecrawl

- **定位**：Agent Score 14.58（AIMultiple #2，相关结果数全场最高），主做 scrape/crawl，附 search
- **免费额度**：**1,000 credits/月（无需信用卡）**，2 并发，月度可再生
- **价格**：Hobby $16/月（5k credits）；Standard $83/月（100k）；Growth $333/月（500k）；Scale $599/月（1M）
- **消耗规则**：Scrape/Crawl/Map 各 **1 credit/page**；**Search = 2 credits / 10 results**（即一次 `search(top_k=10)` 约耗 2 credits）；credits 不滚存
- **申请**：访问 `firecrawl.dev` 注册 → 取 API Key
- **配置**：
  ```bash
  AETHER_FIRECRAWL_API_KEY=fc-xxxxxxxx
  # AETHER_FIRECRAWL_API_URL=https://api.firecrawl.dev/v2   # 默认值
  ```
- **来源**：[firecrawl.dev/pricing](https://www.firecrawl.dev/pricing)

---

### 7. Brave

- **定位**：Agent Score 14.89（AIMultiple #1），独立索引（非 Google/Bing），最快 669ms
- **免费额度**：**$5 月度 credit（≈1,000 次）**，每月自动到账；**需绑信用卡**（仅反欺诈验证，不扣费）
- **价格**：Search 档 **$5/1k requests**；LLM Context 档 $4/1k + $5/1M tokens
- **政策变动（2026-02）**：原"2,000 次/月非商用免费"已下线，改为 $5 月度 credit 制
- **申请**：访问 `api.search.brave.com` 订阅 Free 计划 → 绑卡 → 取 `X-Subscription-Token`
- **配置**：
  ```bash
  AETHER_BRAVE_API_KEY=BSA_xxxxxxxx
  # AETHER_BRAVE_API_URL=https://api.search.brave.com/res/v1   # 默认值
  ```
- **来源**：[brave.com/search/api](https://brave.com/search/api/)、[api-dashboard.search.brave.com/documentation/pricing](https://api-dashboard.search.brave.com/documentation/pricing)

---

### 8. Exa

- **定位**：神经/语义搜索，质量分 3.82/5 全场最高（AIMultiple）
- **免费额度**：**$10 credit/月**（可再生，每月自动到账）+ 注册 **$20** 一次性 credit；Startup / Education 可申请 **$1,000 grant**。credit 池跨 endpoint 共用、按消耗扣（2026-07-30 官方定价页确认，原「1,000 次/月」已改为 credit 制）
- **价格（per 1k requests）**：Search **$7**（含 10 results，每加 1 result +$1/1k）；Deep Search $12；Deep-Reasoning $15；Contents $1/1k pages；Answer $5
- **申请**：访问 `dashboard.exa.ai` 注册 → 取 API Key
- **配置**：
  ```bash
  AETHER_EXA_API_KEY=exa-key-xxxxxxxx
  # AETHER_EXA_API_URL=https://api.exa.ai   # 默认值
  ```
- **来源**：[exa.ai/pricing](https://exa.ai/pricing)

---

### 9. Perplexity Sonar

- **定位**：端到端 LLM 搜索（Sonar API，OpenAI 兼容 chat 端点）
- **免费额度**：**无免费层**，需绑信用卡充值 credit，余额耗尽 API key 被封停
- **价格（per 1M tokens + search query）**：
  - `sonar`（默认）：**$1/1M 入 + $1/1M 出**，search $5–12/1k（按 context size）
  - `sonar-pro`：$3/1M 入 + $15/1M 出，search $6–14/1k
  - `sonar-reasoning-pro`：$2/1M 入 + $8/1M 出
  - `sonar-deep-research`：$2 入 + $8 出
- **申请**：访问 `perplexity.ai` → Settings → API Group（填地址/税务）→ 绑卡 → API Keys → Create Key
- **配置**：
  ```bash
  AETHER_SONAR_API_KEY=pplx-xxxxxxxx
  # AETHER_SONAR_API_URL=https://api.perplexity.ai   # 默认值
  # AETHER_SONAR_MODEL=sonar   # 默认值，最便宜档
  ```
- **来源**：[docs.perplexity.ai/guides/pricing](https://docs.perplexity.ai/guides/pricing)、[docs.perplexity.ai/guides/getting-started](https://docs.perplexity.ai/guides/getting-started)

---

### 10. DeepSeek Search / Fetch

- **定位**：Agent 通道的网页检索与正文抓取，无需配置 Grok；搜索 API 不要求选择模型。
- **价格**（2026-11-01 官方指南）：Search、Fetch 均为 **5 元 / 1,000 次有结果的成功请求**；空结果、失败不计费，`count` 不影响单次搜索价格。
- **申请**：在 `platform.deepseek.com` 创建 API Key。
- **最小配置**：
  ```bash
  AETHER_DEEPSEEK_API_KEY=your-deepseek-key
  # AETHER_DEEPSEEK_API_URL=https://api.deepseek.com  # 服务根地址，不带 /v1
  ```
- 同一个 key 同时启用搜索和 Fetch。抓取顺序为 DeepSeek → Tavily → Firecrawl，跳过未配置的服务；`web_map` 仍需 Tavily。
- 搜索词须为 **1～128 个 Unicode 字符**且非全空白，支持 `site:`、`lang:`、`after:` 等语法；超长查询返回后端错误，不自动截断。每次返回数量限制在 1～50。
- 检索优先使用 `summary`，为空时使用 `snippet`；需要正文时由 Agent 调用 `fetch_page`，或由客户端直接调用 `web_fetch`。
- 不配置 Aux LLM 时直接返回证据；如需 Agent 自动补搜、阅读与生成引用答案，再配置现有 LLM 字段（也可用其他 OpenAI 兼容模型）：
  ```bash
  AETHER_LLM_API_BASE=https://api.deepseek.com/v1
  AETHER_LLM_API_KEY=your-deepseek-key
  AETHER_LLM_MODEL=deepseek-flash
  ```
- **来源**：[Search](https://api-docs.deepseek.com/zh-cn/api/search)、[Fetch](https://api-docs.deepseek.com/zh-cn/api/fetch)、[接口指南与计费](https://api-docs.deepseek.com/zh-cn/guides/search)。

---

## 三、配置示例

### 最低可用（仅 Grok）

```bash
AETHER_GROK_API_URL=https://api.x.ai/v1
AETHER_GROK_API_KEY=xai-xxxxxxxx
AETHER_GROK_MODEL=grok-4.5
```

### 推荐（Grok + 高性价比 Aux 三件套）

```bash
# 主通道
AETHER_GROK_API_URL=https://api.x.ai/v1
AETHER_GROK_API_KEY=xai-xxxxxxxx
AETHER_GROK_MODEL=grok-4.5

# Aux：You.com（$100 免费最慷慨）+ Parallel（第一梯队）+ Linkup（高准确率）
AETHER_YOUCOM_API_KEY=your_ydc_key
AETHER_PARALLEL_API_KEY=parallel_xxxxxxxx
AETHER_LINKUP_API_KEY=linkup_xxxxxxxx

# Aux LLM（改写查询用，任意 OpenAI 兼容）
AETHER_LLM_API_BASE=https://api.openai.com/v1
AETHER_LLM_API_KEY=sk-xxxxxxxx
AETHER_LLM_MODEL=gpt-4o-mini
```

### 验证配置

启动后调用 `get_config` 工具，查看已启用的后端列表（key 脱敏显示）。

---

## 四、数据来源（均为 2026-07-27 抓取的一手官方页面）

| 服务 | 官方来源 URL |
|---|---|
| xAI Grok | `docs.x.ai/developers/models`、`x.ai/api` |
| You.com | `you.com/pricing`、`you.com/docs/guides/search` |
| Parallel | `parallel.ai`（FAQ）、`docs.parallel.ai/api-reference/search/search` |
| Linkup | `linkup.so/pricing` |
| Serper | `serper.dev` |
| Tavily | `tavily.com/pricing` |
| Firecrawl | `firecrawl.dev/pricing` |
| Brave | `brave.com/search/api/`、`api-dashboard.search.brave.com/documentation/pricing` |
| Exa | `exa.ai/pricing` |
| Perplexity Sonar | `docs.perplexity.ai/guides/pricing`、`docs.perplexity.ai/guides/getting-started` |

> 各服务的额度与价格可能随时调整，实际注册时以官网当前公示为准。本指南反映 2026-07-27 当日数据。
