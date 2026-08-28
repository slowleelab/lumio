# Higress + Nacos 接入指南（MCP 单工具平面 · 统一治理）

Higress 是阿里云开源的云原生 AI 网关（Istio + Envoy）。在 Lumio 中，Higress 承担
**「单工具平面 · 单治理」**：把后端各 MCP Server（如 `mcp-server/` 的 22 个信用卡工具）统一收敛，
对上游 Python 编排大脑暴露**一个** streamable-http MCP 入口，并在网关层完成鉴权、限流、审计；
**Nacos** 作为服务发现与 MCP Registry，供 Higress 发现后端 MCP Server。

```
Python 大脑(streamable-http)
        │  MCP_ENDPOINT=http://localhost:10000/mcp/credit-card
        ▼
   ┌─────────────┐   服务发现     ┌──────────┐
   │   Higress   │ ─────────────▶ │  Nacos   │
   │  AI 网关     │                └──────────┘
   │  (统一治理)  │   SSE 代理           ▲ 注册
   └─────┬───────┘                      │
         ▼                              │
   Java MCP Server(:8090, SSE) ─────────┘（profile=nacos 时注册）
   （22 个信用卡工具，mock 数据）
```

> 生产环境 Higress 以 K8s 原生（Helm）部署；本目录提供**开发环境 all-in-one（Docker）**接入，
> 与旧有 Nginx 开发网关并存、互不影响。

## 开发环境快速接入

```bash
# 1) 拉起 Nacos + Higress（仅 gateway profile，不影响 make up 主流程）
make gateway-up

# 2) 启动 Java MCP Server（22 个信用卡工具）
make mcp-server-run
#   如需注册到 Nacos（方式 A），改用：
#   cd mcp-server && mvn spring-boot:run -Dspring-boot.run.profiles=nacos

# 3) 打开 Higress 控制台，导入 MCP Server 路由（见 mcp-credit-card.yaml）
open http://localhost:18080

# 4) 让 Python 大脑经 Higress 调用工具
#    在 .env 中：
#      MCP_ENABLED=true
#      MCP_ENDPOINT=http://localhost:10000/mcp/credit-card

# 停止网关
make gateway-down
```

## 端口

| 组件 | 宿主机端口 | 说明 |
|------|-----------|------|
| Nacos 控制台 / OpenAPI | 8848 | `http://localhost:8848/nacos`（默认账号 nacos/nacos） |
| Nacos gRPC | 9848 | 客户端长连接 |
| Higress 数据面（HTTP） | 10000 | **MCP 入口**：`/mcp/credit-card` |
| Higress 数据面（HTTPS） | 8443 | |
| Higress 控制台 | 18080 | 路由 / MCP / 治理配置 |

## 传输桥接（关键）

- 上游 Python `MCPToolClient` 使用 **streamable-http**（`mcp.client.streamable_http`）。
- 后端 Java MCP Server（Spring AI 1.0.x WebMVC）使用 **SSE**（`/sse` + `/mcp/message`）。
- **Higress 在网关层完成两种传输的桥接**：前端 streamable-http ↔ 后端 SSE，
  因此上游只需连 Higress，无需关心后端传输形态。配置见 `mcp-credit-card.yaml` 的
  `frontendProtocol` / `backendProtocol` 字段。

## 与 Python 侧治理的关系（纵深防御）

Higress 负责**网关层**治理（鉴权、限流、路由、粗粒度审计）；Python 编排层的
确认状态机（敏感工具需用户「确认」）与 `ToolGuard`（按角色授权、金额上限、决策审计）
负责**业务层**治理。两者互补，缺一不可——即便网关放行，敏感写操作仍必须经用户显式确认。

## 生产环境（K8s / Helm）参考

```bash
helm repo add higress https://higress.io/helm-charts
helm install higress higress/higress -n higress-system --create-namespace \
  --set global.mcpRegistry.enabled=true \
  --set global.mcpRegistry.nacos.serverUrl=http://nacos.lumio:8848
```

MCP Server 与网关路由通过 Higress CRD（`McpBridge` / MCP 管理）或控制台声明式下发；
后端服务经 Nacos MCP Registry 发现。业务 Ingress（bot :8000 / assist :8001）示例见下。

### 业务 Ingress 示例

```yaml
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: lumio-ingress
  namespace: lumio
  annotations:
    higress.io/websocket: "true"       # assist WebSocket
    higress.io/rate-limit: "100/min"
spec:
  ingressClassName: higress
  rules:
    - http:
        paths:
          - path: /api/bot
            pathType: Prefix
            backend: { service: { name: bot-service, port: { number: 8000 } } }
          - path: /api/assist
            pathType: Prefix
            backend: { service: { name: assist-service, port: { number: 8001 } } }
```

---

## 联调实录（2026-08-26，已完成的配置修复 + 剩余一步）

### 已修复并入库的 compose 配置（原配置三处错误）

1. **`MODE=standalone` → `MODE=full`**：standalone 模式 `start-gateway.sh` 直接退出，网关数据面（envoy :80/443）根本不启动。
2. **端口映射修正**：all-in-one envoy 实际监听 **8080(HTTP)/8443(HTTPS)**，控制台 jar 监听 **8001**（原映射 10000:80 / 8443:443 / 18080:8080 全部错位）→ 改为 `10000:8080`、`8443:8443`、`18080:8001`。
3. **Nacos 实例 IP 自动探测**：`LUMIO_NACOS_INSTANCE_IP=mcp-server`（主机名）无法被 envoy 解析 → 留空自动探测容器 IP（172.20.0.x，网关直连可达）。

### 控制台 API 全流程（免 UI，curl 可用）

```bash
# 1) 初始化管理员（system.initialized=false 时）
curl -X POST -H 'Content-Type: application/json' \
  -d '{"adminUser":{"name":"admin","displayName":"管理员","password":"<密码>"}}' \
  http://localhost:18080/system/init
# 2) 登录（cookie 会话）
curl -c /tmp/hc.txt -X POST -H 'Content-Type: application/json' \
  -d '{"username":"admin","password":"<密码>"}' http://localhost:18080/session/login
# 3) 下发 DIRECT_ROUTE MCP 服务（⚠️ 2.1.5 SDK 字段是 upstreamPathPrefix，
#    不是主线分支的 directRouteConfig — 用错字段会被静默丢弃）
curl -b /tmp/hc.txt -X PUT -H 'Content-Type: application/json' \
  -d '{"name":"credit-card","description":"Lumio 信用卡工具平面","type":"DIRECT_ROUTE",
       "upstreamPathPrefix":"/",
       "services":[{"name":"lumio-mcp-server","port":8090,"weight":100}]}' \
  http://localhost:18080/v1/mcpServer
```

### 已验证状态与剩余阻塞

| 环节 | 状态 |
|---|---|
| Java MCP Server（容器版）注册 Nacos（22 工具元数据、容器 IP、healthy） | ✅ |
| 网关数据面 envoy 启动、:10000 HTTP 可达 | ✅ |
| 控制台初始化 + MCP DIRECT_ROUTE 资源创建 | ✅ |
| 网关 MCP 过滤器路由（匹配 `/mcp-servers/credit-card`） | ✅ |
| **envoy 上游 cluster 生成（pilot ServiceEntry push → CDS）** | ❌ **503：controller 已 push ServiceEntry 但 envoy 未生成对应 cluster**（all-in-one 单容器 pilot/gateway 同体，节点身份与端点推送异常）。公开路径 `/mcp/credit-card` 亦未生成（2.1.5 控制台实际以 `/mcp-servers/{name}` 为入口）。 |

剩余一步是 Higress all-in-one 镜像内部（pilot↔gateway 同容器）的端点推送问题，建议：生产用 Helm 部署（`global.mcpRegistry.enabled=true` + Nacos），或向 higress-group 提 issue；开发联调继续用直连方案（见上：参考 Server :8080/mcp 或 Java :8090 SSE）。

### 根因补充（2026-08-26 深挖结论）

排查后确定是两个 **all-in-one standalone 镜像内部缺陷**（非本项目配置问题），均有完整证据：

1. **key-auth WASM 插件 schema 不兼容**：创建 MCP 路由时，控制端自动生成内部插件实例 `key-auth.internal`（`global_auth:false` 旧字段），被官方 registry 的 key-auth:1.0.0 插件严格模式拒绝 → envoy NACK 整次 xDS 推送 → 数据面无监听器/cluster。
   - **workaround（已验证有效）**：删除该实例（`DELETE /apis/extensions.higress.io/v1alpha1/namespaces/higress-system/wasmplugins/key-auth.internal`）→ envoy 立即恢复收配置、clusters 生成。
2. **standalone 控制器 ServiceEntry 节点身份错误**：控制器把 ServiceEntry push 到不存在的节点 `higress-pilot`（二进制内定，`POD_NAME` env 无法覆盖，已实测），网关代理（`higress-gateway`）永远收不到 → 上游 cluster 缺失 → `503 cluster_not_found`。K8s/Helm 部署下控制器写 CRD、pilot informer fan-out，无此问题。

**结论**：本地开发联调用直连方案（参考 Server :8080/mcp 或 Java :8090 SSE）；生产走 Helm；该两缺陷建议向 higress-group 提 issue（证据链完整可复现）。

### 开发直连方案补充（2026-08-26，SSE 直连 Java 已实现并验证）

Python 侧 MCP 客户端（`lumio/services/common/mcp_client.py`）现已支持双传输：
- `MCP_TRANSPORT=streamable-http`（默认）：经 Higress 网关（生产/治理链路）；
- `MCP_TRANSPORT=sse`：**直连 SSE 后端，无需 Higress**——开发联调可直接用容器版 Java MCP Server 的 22 个工具：
  `.env` 配 `MCP_ENDPOINT=http://127.0.0.1:8090/sse`（SDK 把 url 视为 SSE 端点本身，须含 /sse）+ `MCP_TRANSPORT=sse`。
  已验证：握手/工具目录/工具编排对话/敏感卡号豁免重路由/防死循环兜底全链正常（工具循环受本地 Ollama 容量约束）。
