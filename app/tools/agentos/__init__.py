"""AgentOS Staging Agent API 对接层（真测试网链路）。

隔离于旧的 core_client：本包只对接 https://api-staging.agentos-app.app 的
`Agent` tag 接口，身份从 Bearer JWT 派生，不复用 /internal/v1/{resource} 猜测式契约。
"""
