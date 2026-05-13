# OpenAI Log Proxy

轻量级 OpenAI-compatible 请求/响应日志代理，适合放在：

```text
new-api → log-proxy → CLIProxyAPI / CPA → OAuth Provider
```

也可以作为 LiteLLM 的 raw 归档补充：

```text
new-api → LiteLLM → log-proxy → CLIProxyAPI
```

它只做一件事：**透明转发 OpenAI API 请求，并确定性保存原始 request / response / stream chunks**。

## 已实现能力

- 透明代理 `/v1/*` 所有路径，例如：
  - `GET /v1/models`
  - `POST /v1/chat/completions`
  - `POST /v1/responses`
- 支持非流式请求日志：request body、response body、headers、status、latency、usage。
- 支持 `stream=true`：边转发 SSE chunk，边采集 raw chunks，并尽量拼接 assistant 文本。
- 支持 SQLite 持久化。
- 支持 ingress key：new-api 调 log-proxy 用一个 key，log-proxy 调 CPA 用另一个 key。
- 自动脱敏：`Authorization`、`Cookie`、`api_key`、`token`、`password` 等字段会被 mask。
- 提供最小日志查询 API：
  - `GET /logs`
  - `GET /logs/{request_id}`
- 日志写入不阻塞代理路径；stream 场景不会等待完整响应后再返回给客户端。

## 适合你的部署链路

推荐先用：

```text
new-api → log-proxy → CLIProxyAPI
```

如果你还想保留 LiteLLM 的 UI 和统计：

```text
new-api → LiteLLM → log-proxy → CLIProxyAPI
```

## 环境变量

| 变量 | 说明 | 默认值 |
|---|---|---|
| `UPSTREAM_BASE_URL` | 上游 OpenAI-compatible base URL | `http://relay-cli-proxy:8317/v1` |
| `UPSTREAM_API_KEY` | 转发到 CPA 使用的 key | 空，表示透传 inbound Authorization |
| `PROXY_API_KEY` | new-api 调用 log-proxy 的 key；为空则不校验 | 空 |
| `ADMIN_API_KEY` | 查询 `/logs` 的 key；为空则禁用 `/logs` | 空 |
| `SQLITE_PATH` | SQLite 数据库路径 | `/data/log-proxy.sqlite3` |
| `REQUEST_TIMEOUT_SECONDS` | 上游请求超时 | `600` |
| `MAX_LOG_BODY_BYTES` | 单字段最大日志字节数 | `2000000` |
| `LOG_REQUEST_BODY` | 是否保存请求 body | `true` |
| `LOG_RESPONSE_BODY` | 是否保存响应 body / stream chunks | `true` |
| `LOG_STREAM_CHUNKS` | 是否保存 SSE raw chunks | `true` |

## Coolify 部署建议

在同一个 Docker 网络里部署即可。假设 CPA 内网别名是：

```text
relay-cli-proxy
```

log-proxy 配：

```env
UPSTREAM_BASE_URL=http://relay-cli-proxy:8317/v1
UPSTREAM_API_KEY=sk-your-cpa-key
PROXY_API_KEY=sk-your-log-proxy-key
ADMIN_API_KEY=sk-your-admin-key
```

new-api 渠道改成：

```text
类型：OpenAI
Base URL：http://log-proxy:8080/v1
API Key：sk-your-log-proxy-key
```

如果 new-api 和 log-proxy 不在同一 Docker 网络，用公网域名：

```text
https://log-proxy.yourdomain.com/v1
```

## Docker Compose

```bash
docker compose -f docker-compose.example.yml up -d --build
```

## 本地运行

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env
uvicorn app.main:app --host 0.0.0.0 --port 8080
```

## 测试

```bash
pytest -q
```

## 调用示例

```bash
curl http://localhost:8080/v1/chat/completions \
  -H "Authorization: Bearer sk-your-log-proxy-key" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gpt-5.4",
    "messages": [{"role": "user", "content": "hello"}]
  }'
```

查询日志：

```bash
curl http://localhost:8080/logs \
  -H "Authorization: Bearer sk-your-admin-key"
```

查看单条详情：

```bash
curl http://localhost:8080/logs/lp_xxxxx \
  -H "Authorization: Bearer sk-your-admin-key"
```

## 性能设计

同 Docker 网络下，多一层代理的网络延时通常是毫秒级。这个项目避免两个最容易拖慢体验的问题：

1. stream 请求不会等完整响应收完再返回；它会边读 upstream chunk，边 yield 给客户端。
2. 日志写入通过后台 task 完成，不阻塞主响应路径。

真正需要注意的是日志体积。你如果经常有 20 万 token 级请求，建议降低：

```env
MAX_LOG_BODY_BYTES=500000
```

或者只在调试阶段开启完整 body 日志。

## 安全注意事项

日志里会保存 prompt、response、tools schema、部分 metadata。即使有脱敏，也应该把 `/logs` 限制在内网或加鉴权。

建议：

- 设置 `ADMIN_API_KEY`。
- 不要暴露 `/logs` 到公网，或者在 Coolify/Traefik 前面再加 Basic Auth / IP allowlist。
- 定期清理 SQLite 数据库，避免无限增长。

## 当前边界

这是轻量代理，不是 LiteLLM / Helicone 的完整替代品。它不做：

- 用户额度管理
- team / budget
- 模型路由
- fallback
- 成本计算
- 可视化 UI

它的目标是：**把每条 OpenAI-compatible 请求和响应可靠留档**。
