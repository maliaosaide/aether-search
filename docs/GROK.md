# Grok 通道

新实现已接入现有 MCP 入口。双通道并行、Agent 通道、公共信源处理和页面工具保持原有方案；本次只替换 Grok 内部实现及必要的配置、导入和测试。

## 实现方式

- 使用官方 `openai` SDK 的 `AsyncOpenAI`，不自行实现 SSE 分帧或通用网关框架。
- 只有两条请求路径：Chat Completions 和 Responses。CPA 复用这两种兼容协议，不另外维护一套适配器。
- xAI 官方通过 Responses 的 `web_search`、`x_search` 工具联网，并设置 `store:false`。
- SDK 自带重试设为 `0`，由通道统一控制重试次数和总时限。
- 两通道使用同一个 `channel_types.AnswerResult`；Grok 失败时返回空正文，让现有编排接替。

新 Grok 实现依据下方的官方协议独立编写，不以旧 Grok 代码和提示词为实现模板。公共模块及许可证没有随本次重写删除，也不宣称整个项目的历史来源已经消失。

## 接入配置

所有字段使用 `AETHER_` 环境变量前缀，也可写入项目根目录 `.env`。该文件被 Git 忽略；不要把真实密钥放进示例文件或提交。

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `AETHER_GROK_API_URL` | 空 | 包含版本前缀的 base URL，不是完整生成路径 |
| `AETHER_GROK_API_KEY` | 空 | Bearer 密钥；只有 URL、没有 key 时不启用通道 |
| `AETHER_GROK_MODEL` | `grok-4` | 模型名或 CPA 模型别名，原样透传；须选择上游实际可用的模型 |
| `AETHER_GROK_PROVIDER` | `auto` | `auto`、`openai`、`xai`、`cpa` |
| `AETHER_GROK_API_FORMAT` | `auto` | `auto`、`chat_completions`、`responses` |
| `AETHER_GROK_SEARCH_MODE` | 空 | 空值、`auto`、`on`、`off`、`none`，见下方说明 |
| `AETHER_GROK_ENDPOINTS` | 空 | 多端点 JSON，设置后覆盖单端点 |
| `AETHER_GROK_FAILOVER` | `sequential` | 顺序主备，或显式启用 `race` 竞速 |
| `AETHER_GROK_MAX_CONCURRENCY` | `2` | 竞速并发上限 |
| `AETHER_GROK_TIMEOUT` | `60` | 整个 Grok 调用的总时限，单位秒 |
| `AETHER_GROK_REQUEST_TIMEOUT` | `45` | 单次尝试时限，不超过总预算剩余时间 |
| `AETHER_RETRY_MAX_ATTEMPTS` | `3` | 所有端点共享的额外重试次数，不按端点数量倍增 |

`auto` 只通过精确主机名 `api.x.ai` 识别 xAI；其他地址默认视为 OpenAI 兼容网关。API 格式自动选择时，xAI 使用 Responses，其他使用 Chat Completions。

### 三种接入示例

以下密钥和通用模型名均为占位符。三组配置选择一组，不是同时写入。

```dotenv
# OpenAI 兼容网关
AETHER_GROK_API_URL=https://gateway.example/v1
AETHER_GROK_API_KEY=YOUR_KEY
AETHER_GROK_MODEL=YOUR_GROK_MODEL
AETHER_GROK_PROVIDER=openai
AETHER_GROK_API_FORMAT=chat_completions
```

```dotenv
# xAI 官方原生搜索
AETHER_GROK_API_URL=https://api.x.ai/v1
AETHER_GROK_API_KEY=YOUR_KEY
AETHER_GROK_MODEL=YOUR_GROK_MODEL
AETHER_GROK_PROVIDER=xai
AETHER_GROK_API_FORMAT=responses
```

```dotenv
# 自建 CPA；模型使用其配置的别名
AETHER_GROK_API_URL=http://localhost:8317/v1
AETHER_GROK_API_KEY=YOUR_KEY
AETHER_GROK_MODEL=YOUR_MODEL_ALIAS
AETHER_GROK_PROVIDER=cpa
AETHER_GROK_API_FORMAT=chat_completions
```

CPA 或通用网关确实支持 Responses 时，可显式设置 `AETHER_GROK_API_FORMAT=responses`。

搜索模式规则：
- 空值或 `auto`：xAI 请求 `web_search` 和 `x_search`；其他网关不附加未经确认的工具参数。
- `on`：要求 Responses。xAI 使用上述两个工具；通用／CPA Responses 仅请求标准 `web_search`，需要其上游支持。
- `off`／`none`：我方不携带原生工具；不能据此关闭网关未公开的联网行为。

多端点 JSON 的每项支持 `url,key,model?,label?,provider?,api_format?,search_mode?`；未提供的可选字段继承全局设置。端点的 `search_mode:""` 可以覆盖全局 `on`。竞速可能同时产生多笔上游费用，取消请求也不保证服务商停止计费。

## 回答、引用与失败处理

- Chat 只采用 choice 0，文本非空且 `finish_reason=stop` 才算成功；截断、过滤、未执行的客户端工具调用和缺少结束信号均按失败处理。
- Responses 采用 `response.completed` 的完整输出，只提取 assistant 消息的 `output_text`，不把 reasoning 或工具内容当成回答。
- 收集结构化注解、可用的顶层 `citations` 和最终正文链接，去重后生成 `Sources:` 列表，供现有信源缓存读取。
- `search=observed` 只表示观测到了搜索调用完成；其余为 `requested`／`unconfirmed`／`not_requested`。模型自称搜索过不算执行证据，链接存在也不等于内容已经核实。
- 网络错误和 408／429／500／502／503／504 可以重试；认证、付费、参数、解析错误及空回答不在同一端点自动重试。`Retry-After` 和退避等待都受总时限约束。
- 成功、失败、超时和取消时关闭流；损坏的响应返回通道错误，不击穿另一个通道。

## 离线验证与兼容边界

当前验证环境使用 `openai 3.13.0`，项目以此作为已验证的 SDK 最低版本，未声称更早版本均已测试。

在项目根目录执行（Windows／Git Bash）：

```bash
# 单元与回归测试
PYTHONPATH=src .venv/Scripts/python.exe -m unittest discover -s tests -p 'test_grok.py' -v
PYTHONPATH=src .venv/Scripts/python.exe -m unittest discover -s tests -p 'test_grok_protocol.py' -v
# 进程内 MCP 集成：并行、信源合并、失败接替
PYTHONPATH=src .venv/Scripts/python.exe -m unittest discover -s tests -p 'test_grok_integration.py' -v
```

测试使用标准库 `unittest`、SDK 的模拟 HTTP 传输及进程内 MCP 客户端，不读取用户 `.env`，不调用真实 Grok 或搜索后端。集成测试执行真实的 `server.search` 和 `AgentChannel`，只模拟外部服务。

为保持实现简单，不兼容流式请求返回整包 JSON、严重错标 Content-Type、吞掉 Responses 完成事件等非标准网关行为；这些情况会失败并交给现有回退流程。没有自建 SSE 解析器、字节限额配置或持久化的端点健康管理。

离线测试不证明真实 xAI／CPA 当前可用。模型出现在 `/v1/models` 中，也不等于生成有额度或具备联网能力。真实生成与检索效果需要可用接口另行验证。

## 官方文档依据

- [OpenAI Chat Completions](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create)
- [OpenAI Responses 流式事件](https://developers.openai.com/api/reference/resources/responses/streaming-events)
- [xAI Web Search](https://docs.x.ai/developers/tools/web-search)
- [xAI Citations](https://docs.x.ai/developers/tools/citations)
- [xAI 两种文本接口对比](https://docs.x.ai/developers/model-capabilities/text/comparison)
- [xAI Generate Text](https://docs.x.ai/developers/model-capabilities/text/generate-text)
- [CPA OpenAI Compatibility](https://help.router-for.me/configuration/provider/openai-compatibility)
- [CLIProxyAPI 项目说明](https://github.com/router-for-me/CLIProxyAPI)
