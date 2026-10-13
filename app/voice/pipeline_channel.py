"""pipeline 渠道在签票链路上的守卫。

02 方案的音频由 Runtime 自己合成再转发，没有票要签、没有第三方 Bot 要进房，
``/api/voice`` 那一整套端点在这个渠道下没有对应语义。

这里选择显式抛错而不是返回一张空票 —— 一张能拿到但连不上的票会让客户端在
「凭证问题」方向排查很久，而真正的原因是这条链路根本不该被调用。真正干活的
是 ``app.voice.pipeline_provider.VoicePipeline``，它不实现本 Protocol。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Final, NoReturn

from app.voice.provider import VoiceCredential, VoicePresence, VoiceTaskAcceptance

_UNSUPPORTED: Final = (
    "pipeline 渠道不签发实时语音凭证：音频由 Runtime 合成后经 WebSocket 下发，"
    "客户端不直连第三方。"
)


class PipelineChannelVoiceProvider:
    """签票端点的显式拒绝器。``requires_bot_presence`` 恒为 False。

    没有 Bot 就不能跑进房监控 —— 对客户端不直连的渠道跑它，只会在超时后把一通
    正常通话误判成连接失败（线上实测过）。
    """

    requires_bot_presence: bool = False
    # 这条链路没有票这回事。调用方应当读它来决定「跳过签票」，而不是把
    # NotImplementedError 当故障捕获 —— 后者会变成 500，App 的语音入口整个进不去。
    issues_credentials: bool = False

    @staticmethod
    def _refuse() -> NoReturn:
        raise NotImplementedError(_UNSUPPORTED)

    async def issue_credentials(
        self,
        session_id: str,
        user_id: str,
        tools: tuple[str, ...],
        model: str,
        now: datetime,
        attempt: int = 0,
        context_summary: str = "",
    ) -> VoiceCredential:
        self._refuse()

    async def start(
        self,
        session_id: str,
        user_id: str,
        attempt: int = 0,
        context_summary: str = "",
    ) -> VoiceTaskAcceptance:
        self._refuse()

    async def stop(self, session_id: str, user_id: str, attempt: int = 0) -> None:
        """无操作。

        会话清理链路会无条件调 stop，它必须安全 —— 这个渠道本来就没有远端任务
        需要停止，抛错只会把正常的清理流程搞成告警。
        """

        return

    async def presence(
        self, session_id: str, user_id: str, attempt: int = 0
    ) -> VoicePresence:
        self._refuse()

    async def send_function_result(
        self,
        session_id: str,
        user_id: str,
        tool_call_id: str,
        content: dict[str, Any],
        attempt: int = 0,
    ) -> VoiceTaskAcceptance:
        self._refuse()

    async def send_progress_message(
        self,
        session_id: str,
        user_id: str,
        message: str,
        attempt: int = 0,
    ) -> VoiceTaskAcceptance:
        self._refuse()
