# OpenTelemetry 全链路追踪接入与部署指南 (Local & Staging)

本文档用于指导开发者（或 Claude）完成 **OpenTelemetry (OTel)** 全链路追踪的**本地验证**与 **Staging 环境落地部署**。

---

## 一、背景与可观测性目标

Runtime 内部已预置 OpenTelemetry SDK 及自动埋点能力（见 [`app/telemetry/otel.py`](../../app/telemetry/otel.py) 与 [`docs/research/tech-stack-assessment.md`](../research/tech-stack-assessment.md) §7）。

启用后将覆盖以下全链路追踪能力：
1. **入站 Web 链路**：FastAPI 所有 Controller/API 路由请求自动打上分布式 `Trace ID`。
2. **出站下游链路**：HTTPX 自动捕获向下游 **yesup-core** 与 **LLM 大模型 (Gemini / OpenAI / Claude / New API)** 的调用耗时与状态。
3. **日志关联**：日志自动注入 `otelTraceID` / `otelSpanID`，实现“日志 $\leftrightarrow$ 链路”秒级穿透。
4. **可视化大盘**：通过 Jaeger UI 呈现树状链路瀑布图（Waterfall），精确定位各阶段耗时与错误节点。

---

## 二、第一阶段：本地验证流程 (Local Validation)

> **原则：在推送到远程/Staging 之前，必须先在本地完成端到端验证。**

### 1. 运行本地自动化单测
验证 OTel 内存导出与基础埋点逻辑：
```bash
.venv/bin/pytest tests/test_otel.py -q
```
*期望结果：测试全部通过。*

### 2. 启动本地 Jaeger 容器（收集端 + Web UI）
在本地终端执行：
```bash
docker run -d --name jaeger-local \
  -p 16686:16686 \
  -p 4318:4318 \
  jaegertracing/all-in-one:latest
```
* 端口说明：
  * `16686`：Jaeger 可视化 Web UI 界面
  * `4318`：OTLP HTTP 接收端口（Runtime 上报地址）

### 3. 配置本地 `.env`
在本地 `.env` 文件中开启 OTel 配置：
```env
# 开启 OpenTelemetry
AGENT_OTEL_ENABLED=true
# 指向本地 Jaeger OTLP 端点
AGENT_OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:4318/v1/traces
```

### 4. 启动本地 Runtime 并触发流量

> **前置**：`.env` 里 `AGENT_PROVIDER_MODE=real` 时，`Settings()` 会做一批必填项校验，
> 缺任何一项都直接启动失败（不是警告）。本地 `.env` 通常缺 `AGENT_RT_WS_BASE`；
> 若 `AGENT_REALTIME_VOICE_PROVIDER` 落到默认的 `volc_rtc`，还会额外要求 12 项火山凭证
> （且 `AGENT_RTC_APP_ID` 必须恰好 24 字符、`AGENT_VOLC_CALLBACK_SECRET` 至少 32 字符）。
>
> 最省事的做法是跟 staging 对齐走 `gemini_live`，只需要 `AGENT_GEMINI_API_KEY`：
>
> ```bash
> export AGENT_REALTIME_VOICE_PROVIDER=gemini_live
> export AGENT_RT_WS_BASE=wss://agent-staging.agentos-app.app
> ```
>
> 注意 `AGENT_PROVIDER_MODE=fake` 需要同时 `AGENT_ENV=local`，
> 否则会被 `Fake providers are forbidden in staging/production` 挡下；
> 但 fake 模式不会真发下游请求，看不到出站 span。

```bash
# 启动本地服务
.venv/bin/uvicorn app.main:app --port 8080
```
在另一个终端发起一次接口调用产生链路数据：
```bash
curl -X GET http://localhost:8080/healthz
curl -X GET "http://localhost:8080/v1/agent/news?limit=3"
```

> ⚠️ 本仓库**没有** `/v1/agent/market/tickers` 路由。可用路径以
> `curl -s http://localhost:8080/openapi.json` 为准；业务端点多数需要 USER 鉴权，
> 无鉴权可直接打的只有 `/healthz`、`/readyz`、`/v1/agent/news`。

### 5. 本地 UI 验证
1. 打开浏览器访问：`http://localhost:16686`
2. 在左侧 **Service** 下拉列表中选择 `yesup-agent-runtime`（或对应 `AGENT_APP_NAME`）。
3. 点击 **Find Traces**，点击任意一条 Trace 查看详情：
   * 确认能看到根请求 Span；
   * 确认能看到下游出站调用的子 Span 及其毫秒耗时。

也可以不开浏览器，直接用 Jaeger 的 HTTP API 断言链路树结构：

```bash
curl -s http://127.0.0.1:16686/api/services          # 应含 yesup-agent-runtime
curl -s "http://127.0.0.1:16686/api/traces?service=yesup-agent-runtime&limit=20"
```

> **正常现象，不要误判为缺陷**：Jaeger 里会有一批**没有父 span 的独立 client trace**，
> 指向 `mcp.followin.io`、`api-staging.agentos-app.app`、`generativelanguage.googleapis.com`。
> 它们来自 `app/news/collector.py` 的后台采集循环与启动预检，本来就不在任何请求上下文里，
> 独立成 trace 是对的。
>
> 判断「入站/出站是否真的串起来了」要看**请求内**发出的下游调用。可以注入
> `traceparent` 头做受控验证：
>
> ```bash
> TP="00-$(openssl rand -hex 16)-$(openssl rand -hex 8)-01"
> curl -s -H "traceparent: $TP" http://localhost:8080/healthz
> curl -s "http://127.0.0.1:16686/api/traces/$(echo $TP | cut -d- -f2)"
> ```

---

## 三、第二阶段：Staging 环境部署流程 (Staging Deployment)

> **重要：staging 不能手改服务器上的文件。** 这套部署是「仓库即事实来源」：
>
> * `.github/workflows/deploy-staging.yml` 把仓库里的 `docker-compose.yml` scp 到
>   `/home/sxy/releases/<commit-sha>/`，再从那个 release 目录 `docker compose -p app up -d`；
> * 同一个 job 用 `set_env` / `set_env_optional` **按仓库 vars/secrets 重新生成该目录下的 `.env`**，
>   工作流里写得很直白：「以仓库 vars 为唯一事实来源 —— 未设置时从 .env 里删掉」。
>
> 所以：
>
> * `/home/sxy/app/` 是 2026-08-07 遗留的旧目录，**当前根本没在用**（活的工作目录可用
>   `sudo docker inspect agent-runtime --format '{{index .Config.Labels "com.docker.compose.project.working_dir"}}'` 确认）；
> * 手改服务器 `docker-compose.yml` 不会生效，手改服务器 `.env` 会在下次发布被覆盖回来。
>
> **正确做法是改仓库，合并到 `main` 触发部署。**

### 1. Staging 基础信息
* **主机 IP**：`174.138.21.66`
* **登录用户**：`sxy`
* **发布目录**：`/home/sxy/releases/<commit-sha>/`（每次部署新建，compose 从这里拉起）
* **compose 项目名**：`app`（所有命令都要带 `-p app`）

### 2. 在仓库 `docker-compose.yml` 追加 `jaeger` 服务

```yaml
  jaeger:
    image: jaegertracing/all-in-one:1.76.0
    container_name: agent-runtime-jaeger
    restart: unless-stopped
    ports:
      - "127.0.0.1:16686:16686"    # Web UI（SSH 隧道访问）
      - "127.0.0.1:4318:4318"      # OTLP HTTP，仅供宿主机排查；容器间走服务名
    environment:
      - COLLECTOR_OTLP_ENABLED=true
      - MEMORY_MAX_TRACES=20000
      - SPAN_STORAGE_TYPE=memory
```

并给 `agent-runtime` 的 `depends_on` 加上 `jaeger`。

三个关键点，别照抄成默认写法：

1. **端口必须绑 `127.0.0.1`。** Jaeger UI **没有任何鉴权**，而链路里带着
   `unified_account_id`、`request_id`、下游 URL 与查询参数。绑 `0.0.0.0`
   等于把这些直接摊在公网上。看板走 SSH 隧道（见第四节）。
2. **镜像钉死完整版本号。** `latest` 目前指向 v1.76.0；Jaeger **v2 换成了配置文件驱动**，
   与上面这些环境变量不兼容，跟着 `latest` 漂会在某次拉取后直接起不来。
   注意 tag 必须写完整三段 `1.76.0` —— 两段式的 `1.76` **在 registry 里不存在**
   （`1.60` 之后就不再发布两段式 tag 了），写错会在 `up -d` 拉镜像时失败。
3. **`MEMORY_MAX_TRACES` 必须设。** all-in-one 是纯内存存储，staging 总共 4G 内存，
   不封顶会被吃满。

### 3. 在部署工作流里加 OTel 变量（不是改服务器 `.env`）

`.github/workflows/deploy-staging.yml` 的 env 段：

```yaml
          OTEL_ENABLED: ${{ vars.AGENT_OTEL_ENABLED || 'true' }}
          OTEL_ENDPOINT: ${{ vars.AGENT_OTEL_EXPORTER_OTLP_ENDPOINT || 'http://jaeger:4318/v1/traces' }}
```

`set_env` 段：

```bash
            set_env_optional AGENT_OTEL_ENABLED "$OTEL_ENABLED"
            set_env_optional AGENT_OTEL_EXPORTER_OTLP_ENDPOINT "$OTEL_ENDPOINT"
```

用 `set_env_optional` 而不是 `set_env`：Jaeger 挂掉不该拖垮业务 ——
`BatchSpanProcessor` 只会丢 span 并打日志，不会让请求失败，所以这两项不做硬性 require。

要临时关闭追踪，把仓库 var `AGENT_OTEL_ENABLED` 设成 `false` 重新发布即可。

### 4. 合并触发部署并验证

合并到 `main` 后部署自动执行。验证：

```bash
ssh sxy@174.138.21.66
release_dir=$(sudo docker inspect agent-runtime \
  --format '{{index .Config.Labels "com.docker.compose.project.working_dir"}}')
cd "$release_dir"
sudo docker compose -p app ps
```

*确认 `agent-runtime` 与 `agent-runtime-jaeger` 均为 `Up`。*

确认 Runtime 真的把 OTel 打开了（而不是只看容器起来了）：

```bash
sudo docker compose -p app exec -T agent-runtime printenv \
  AGENT_OTEL_ENABLED AGENT_OTEL_EXPORTER_OTLP_ENDPOINT
```

## 四、验收与线上排查方式

1. **访问 Staging Jaeger Web 大盘**（只能走 SSH 隧道，端口没有对公网开放，见第三节第 1 点）：

   > ⚠️ **别用 16686 做本地转发端口。** 第二节的本地 `jaeger-local` 容器已经占了
   > 16686，隧道再往这个端口转发会把本地 UI 顶掉 —— 而且**不报错**：浏览器照常
   > 打开，但你看到的是 staging 的数据。现象极具误导性：服务列表里本地的
   > `yesup-agent-runtime` 凭空消失，只剩 staging 的 `agent-runtime`，还有一串每
   > 10 秒一次的 `GET /healthz`（那是 staging 容器的 healthcheck）。很容易误判成
   > 「本地链路丢了」，实际只是在读另一个 Jaeger。
   >
   > 用一个不冲突的本地端口：

   ```bash
   ssh -N -f -L 16687:127.0.0.1:16686 sxy@174.138.21.66
   ```

   然后：

   * `http://localhost:16686` → 本地 Jaeger（服务名 `yesup-agent-runtime`）
   * `http://localhost:16687` → Staging Jaeger（服务名 `agent-runtime`）

   分不清当前在读哪个，看服务名即可；也可以绕开端口直接查本地容器：

   ```bash
   docker exec jaeger-local wget -qO- http://127.0.0.1:16686/api/services
   ```
2. **实际业务流量验证**：
   * 在 App / 测试端触发一次语音对话或文字请求（例如查询余额或查行情）；
   * 在 Jaeger UI 中即可检索到该会话的完整耗时分解与下游 API 调用详情；
   * 若遇到报错（如 404 / 500），链路树上的对应节点将**直接标红**，点开即可看到完整的 HTTP 响应状态码与错误信息。
