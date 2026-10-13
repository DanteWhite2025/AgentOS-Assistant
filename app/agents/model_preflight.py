"""LLM 网关的模型 ID 预检。

存在的理由：`AGENT_LLM_MODEL` 配成中转站不存在的 ID 时，`Settings()` 只校验
非空所以会通过，容器正常启动，`/healthz` 照样返回 ok —— 但每一次真实对话
都会拿到 503 `model_not_found`。这种「配置错了但一切看起来正常」的失败
必须在 readiness 上暴露出来。

区分两类失败，因为处置方式完全不同：
- `model_not_found` / `gateway_rejected`：配置错误，人必须去改配置
- `gateway_unavailable`：站点抖动，等它自己恢复

只有配置错误会让 /readyz 变 not_ready（部署时被卡住，不会静默上线坏配置）；
站点抖动不拦 readiness，理由见 `blocks_readiness`。两者都不在启动路径上抛
异常 —— 中转站临时不可用不该让整个服务起不来。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Literal

import httpx

logger = logging.getLogger(__name__)

PreflightStatus = Literal[
    "not_checked",
    "valid",
    "model_not_found",
    "gateway_rejected",
    "gateway_unavailable",
]


@dataclass(frozen=True)
class ModelPreflightSnapshot:
    """预检结果。可安全下发给 /readyz —— 不含 key。"""

    status: PreflightStatus
    ready: bool
    model: str
    available_models: tuple[str, ...] = field(default_factory=tuple)
    detail: str = ""

    @property
    def blocks_readiness(self) -> bool:
        """这个结果是否应该让 /readyz 变 not_ready。

        只有**配置错误**才拦：模型不存在、key 被拒 —— 这两种不改配置永远不会
        自己好，必须让部署卡住、让人去改。

        站点抖动（gateway_unavailable）刻意**不拦**：预检只在启动时跑一次，
        若把它算进 readiness，启动瞬间的一次超时会让服务永久 not_ready，
        直到有人手动重启。可用性问题由真实请求的失败去反映，不归预检管。
        """

        return self.status in ("model_not_found", "gateway_rejected")

    def public_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "valid": self.ready,
            "model": self.model,
            # 只回前 20 个：站内模型可能很多，readyz 不该变成一份清单转发。
            "available_models": list(self.available_models[:20]),
            "detail": self.detail,
        }


def _parse_model_ids(payload: Any) -> tuple[str, ...] | None:
    """从 OpenAI 兼容的 /models 响应里取模型 ID。

    结构不符时返回 None（而不是空元组）—— 空元组会被误读成「站里一个模型都
    没有」，那是另一回事。
    """

    if not isinstance(payload, dict):
        return None
    data = payload.get("data")
    if not isinstance(data, list):
        return None
    return tuple(
        item["id"] for item in data if isinstance(item, dict) and isinstance(item.get("id"), str)
    )


class LLMModelPreflight:
    """打一次 GET {base_url}/models，确认配置的模型真的在站里。"""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        client: httpx.AsyncClient,
    ) -> None:
        self._url = f"{base_url.rstrip('/')}/models"
        self._api_key = api_key
        self._model = model
        self._client = client
        self._snapshot = ModelPreflightSnapshot(status="not_checked", ready=False, model=model)

    @property
    def snapshot(self) -> ModelPreflightSnapshot:
        return self._snapshot

    async def check(self) -> ModelPreflightSnapshot:
        self._snapshot = await self._check()
        self._log(self._snapshot)
        return self._snapshot

    async def _check(self) -> ModelPreflightSnapshot:
        try:
            response = await self._client.get(
                self._url, headers={"Authorization": f"Bearer {self._api_key}"}
            )
        except httpx.HTTPError as exc:
            return self._fail("gateway_unavailable", type(exc).__name__)

        if response.status_code in (401, 403):
            # key 的问题，不是模型的问题。混为一谈会把排查引向错误方向。
            return self._fail("gateway_rejected", f"HTTP {response.status_code}")
        if response.status_code >= 400:
            return self._fail("gateway_unavailable", f"HTTP {response.status_code}")

        try:
            ids = _parse_model_ids(response.json())
        except ValueError:
            return self._fail("gateway_unavailable", "响应不是合法 JSON")
        if ids is None:
            return self._fail("gateway_unavailable", "响应缺少 data[] 结构")

        if self._model not in ids:
            return ModelPreflightSnapshot(
                status="model_not_found",
                ready=False,
                model=self._model,
                available_models=ids,
                detail="配置的模型不在中转站清单内，对话会全部失败",
            )
        return ModelPreflightSnapshot(
            status="valid", ready=True, model=self._model, available_models=ids
        )

    def _fail(self, status: PreflightStatus, detail: str) -> ModelPreflightSnapshot:
        return ModelPreflightSnapshot(status=status, ready=False, model=self._model, detail=detail)

    def _log(self, snapshot: ModelPreflightSnapshot) -> None:
        if snapshot.status == "valid":
            logger.info("llm_model_preflight ok model=%s", snapshot.model)
            return
        if snapshot.status == "model_not_found":
            # 明确写出站内有什么，否则运维只能靠猜。永不打印 key。
            logger.error(
                "llm_model_preflight 配置的模型不存在 model=%s available=%s —— "
                "对话会全部失败，请按 GET {base_url}/models 的清单修正 AGENT_LLM_MODEL",
                snapshot.model,
                ",".join(snapshot.available_models[:20]) or "(空)",
            )
            return
        logger.warning(
            "llm_model_preflight 未能完成 status=%s detail=%s model=%s",
            snapshot.status,
            snapshot.detail,
            snapshot.model,
        )
