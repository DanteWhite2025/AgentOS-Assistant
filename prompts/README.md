# prompts

各 agent 的 system prompt 文案集中管理。

**当前状态**：MVP 默认 prompt 在 `app/container.py` 与 `app/agents/profiles/`。

**待补**（需产品/团队提供）：确定 avatar / strategy / monitor / fund_manager 的人设、语气、约束文案后迁入此处。

**建议格式**：每个 agent 一个 `.md`（或 `.txt`），按版本管理；加载逻辑在 `app/agents/profiles` 读取。
