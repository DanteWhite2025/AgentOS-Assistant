"""控制面 REST 请求 / 响应模型（Pydantic v2）。

与契约 docs/api/agent-mobile/openapi.agent.yaml 对齐。FastAPI 从这些模型
自动生成 /docs（Swagger UI）、/redoc、/openapi.json。

约定：时间 UTC ISO 8601；金额一律字符串 Decimal（禁 float）。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

Modality = Literal["text", "voice", "both"]
AgentName = Literal["avatar", "monitor", "strategy", "fund_manager"]
ErrorCode = Literal[
    "INVALID_REQUEST",
    "UNAUTHENTICATED",
    "FORBIDDEN",
    "AUTH_UPSTREAM_UNAVAILABLE",
    "UNAUTHORIZED",
    "TOOL_NOT_IN_SCOPE",
    "SESSION_NOT_FOUND",
    "IDEMPOTENCY_CONFLICT",
    "SESSION_EXPIRED",
    "RATE_LIMITED",
    "INTERNAL",
    "TOOL_UPSTREAM_TIMEOUT",
    "POLICY_VIOLATION",
]


class ErrorEnvelope(BaseModel):
    """统一错误信封。"""

    code: ErrorCode
    message: str
    details: dict[str, Any] | None = None
    trace_id: str


class ClientInfo(BaseModel):
    platform: Literal["ios", "android"] | None = None
    app_version: str | None = None


class SessionCreateRequest(BaseModel):
    modality: Modality = Field(description="期望的初始交互形态")
    locale: str | None = Field(default=None, examples=["zh-CN"])
    client_info: ClientInfo | None = None


class AgentScope(BaseModel):
    """会话的 AgentMandate —— 工具白名单是唯一权威来源。"""

    agents: list[AgentName]
    tools: list[str] = Field(description="该会话允许调用的工具白名单（RT 二次校验依据）")


class VoiceCredentials(BaseModel):
    """短期语音凭证；敏感 AppKey/AK/SK 永不下发。"""

    provider: Literal["fake", "openai", "volc_rtc", "gemini_live"]
    endpoint_url: str | None = Field(
        default=None,
        description=(
            "客户端建立媒体直连的入口；火山 RTC 由原生 SDK 用 app_id/room_id/token "
            "进房，无客户端可见 HTTP 入口，此处为 null"
        ),
    )
    expires_at: datetime | None = Field(
        default=None, description="统一有效期；与 client_secret/token 的到期时间一致"
    )
    allowed_tools: list[str] = Field(
        default_factory=list,
        description="本次通话允许触发的工具白名单；服务端仍会按会话 scope 二次校验",
    )
    client_secret: str | None = Field(default=None, description="OpenAI 兼容路径的短期凭证")
    client_secret_expires_at: datetime | None = None
    new_session_expires_at: datetime | None = None
    model: str | None = None
    call_url: str | None = None
    app_id: str | None = Field(default=None, description="火山 RTC 公共 AppId")
    room_id: str | None = None
    user_id: str | None = None
    token: str | None = Field(default=None, description="火山 RTC 短期 Token")
    token_expires_at: datetime | None = None
    task_id: str | None = None
    bot_user_id: str | None = None


class SessionCredentials(BaseModel):
    session_id: str
    rt_token: str = Field(description="短期 JWT（~5min），用于文本 WS 与工具旁挂")
    rt_token_expires_at: datetime
    ws_url: str
    voice: VoiceCredentials | None = None
    scope: AgentScope
    trace_id: str


class RefreshTokenRequest(BaseModel):
    provider: Literal["fake", "openai", "volc_rtc", "gemini_live"] | None = Field(
        default=None,
        description=(
            "可选的语音渠道；缺省走部署配置的主力 provider。客户端直连握手超时降级时"
            "带上备用渠道名领备用票（例如 volc_rtc）。会话已有活跃 provider 任务时"
            "不允许切换，需先结束会话"
        ),
    )
    want: list[Literal["rt_token", "voice"]] | None = Field(
        default=None, description="需要刷新的凭证类型，默认两者都刷"
    )


class RefreshedCredentials(BaseModel):
    rt_token: str | None = None
    rt_token_expires_at: datetime | None = None
    voice: VoiceCredentials | None = None
    trace_id: str


class AudioStreamStartData(BaseModel):
    """``audio.start`` 事件的配置体：开一段新的流式识别。"""

    format: Literal["pcm", "wav", "opus", "aac", "ogg"] = "pcm"
    sample_rate: int = Field(default=16000, ge=8000, le=48000)
    channels: int = Field(default=1, ge=1, le=2)


class AudioStreamTranscriptData(BaseModel):
    """``transcript.delta`` / ``transcript.done`` 的公共载荷。"""

    text: str
    is_final: bool


class TranscriptDeltaEvent(BaseModel):
    """打字机中间态——还会被后续增量覆盖，前端应整体替换而非追加。"""

    type: Literal["transcript.delta"] = "transcript.delta"
    data: AudioStreamTranscriptData


class TranscriptDoneEvent(BaseModel):
    """整句定稿；一段录音（一次 audio.start ~ audio.end）只有一条。"""

    type: Literal["transcript.done"] = "transcript.done"
    data: AudioStreamTranscriptData


class AudioStreamErrorData(BaseModel):
    code: str
    message: str


class AudioStreamErrorEvent(BaseModel):
    type: Literal["error"] = "error"
    data: AudioStreamErrorData


class AudioTranscribeResponse(BaseModel):
    """短语音上传识别结果（火山豆包 Seed ASR Flash）。"""

    transcript: str
    duration_ms: int = Field(description="识别音频的时长（毫秒），上游未回传时为 0")
    trace_id: str


class VoiceStartResponse(BaseModel):
    session_id: str
    state: Literal["task_accepted", "bot_joining", "voice_ready"]
    trace_id: str


class VoiceDiagnostics(BaseModel):
    """脱敏 Provider 诊断；UI 只展示 SessionState.voice_error_message。"""

    original_failure_stage: str | None = None
    provider_error_code: str | None = None
    provider_error_message: str | None = None
    first_failed_at: datetime | None = None
    start_request_id: str | None = None
    presence_request_id: str | None = None
    callback_trace_id: str | None = None
    room_hash: str | None = None
    mobile_user_hash: str | None = None
    bot_user_hash: str | None = None
    task_hash: str | None = None
    start_requested_at: datetime | None = None
    start_accepted_at: datetime | None = None
    mobile_joined_at: datetime | None = None
    bot_joined_at: datetime | None = None
    task_started_at: datetime | None = None
    first_provider_callback_at: datetime | None = None
    user_audio_status: Literal["UNKNOWN", "OBSERVED"] = "UNKNOWN"
    asr_started_at: datetime | None = None
    user_speech_started_at: datetime | None = None
    user_speech_ended_at: datetime | None = None
    first_user_transcript_at: datetime | None = None
    llm_started_at: datetime | None = None
    llm_completed_at: datetime | None = None
    assistant_first_token_at: datetime | None = None
    function_call_started_at: datetime | None = None
    function_call_completed_at: datetime | None = None
    function_call_failed_at: datetime | None = None
    tts_started_at: datetime | None = None
    tts_first_packet_at: datetime | None = None
    first_assistant_transcript_at: datetime | None = None
    provider_audio_started_at: datetime | None = None
    interrupted_at: datetime | None = None
    stopped_at: datetime | None = None
    cleanup_status: str | None = None
    callback_schema_hash: str | None = None
    callback_received_total: int = 0
    callback_accepted_total: int = 0
    callback_duplicate_total: int = 0
    callback_stale_total: int = 0


class SessionState(BaseModel):
    session_id: str
    state: Literal[
        "created",
        "text_active",
        "voice_starting",
        "task_accepted",
        "bot_joining",
        "voice_ready",
        "bot_join_timeout",
        "provider_failed",
        "voice_stopped",
        "closed",
    ]
    modality: Modality
    created_at: datetime
    last_activity_at: datetime
    scope: AgentScope
    voice_error_code: str | None = None
    voice_error_message: str | None = None
    voice_run_stage: str | None = None
    voice_provider_request_id: str | None = None
    voice_task_submitted_at: datetime | None = None
    voice_bot_joined_at: datetime | None = None
    voice_task_creation_result: str | None = None
    voice_callback_received_at: datetime | None = None
    voice_stopped_at: datetime | None = None
    voice_cleanup_status: str | None = None
    voice_user_transcript_at: datetime | None = None
    voice_assistant_transcript_at: datetime | None = None
    voice_provider_audio_started_at: datetime | None = None
    voice_interrupted_at: datetime | None = None
    voice_diagnostics: VoiceDiagnostics | None = None


class ToolSpec(BaseModel):
    """受控 Function Calling 工具的 Schema 定义。"""

    name: str = Field(description="工具名称")
    description: str = Field(description="工具功能描述（供 LLM / Function Call 识别）")
    parameters: dict[str, Any] = Field(
        default_factory=dict, description="JSON Schema 格式的参数列表及限制"
    )
    write: bool = Field(default=False, description="是否为受控写工具（创建草案）")
    external: bool = Field(default=False, description="是否由外部 Staging 节点执行")


class ToolCallRequest(BaseModel):
    call_id: str = Field(description="OpenAI function_call 的 call_id，用于回灌配对")
    tool_name: str = Field(description="客户端上报的工具名（RT 必按 scope 二次校验，不可信任）")
    arguments: dict[str, Any] = Field(
        default_factory=dict, description="工具参数（RT 做 JSON Schema 校验）"
    )


class ToolCallResult(BaseModel):
    call_id: str
    status: Literal["ok", "error"]
    output: dict[str, Any] | None = Field(
        default=None, description="成功时的结构化结果（回灌 function_call_output）"
    )
    error: ErrorEnvelope | None = None
    trade_draft_id: str | None = Field(default=None, description="下单类工具返回的 TradeDraft id")


class SingleTurnRequest(BaseModel):
    session_id: str
    text: str
    web_search: bool = Field(
        default=False,
        description="仅对本条消息启用并强制执行联网搜索",
    )


class SingleTurnResponse(BaseModel):
    text: str
    tool_calls: list[ToolCallResult] = Field(default_factory=list)
    blockers: list[str] = Field(
        default_factory=list,
        description="结构化 Core readiness blocker；普通用户文案不直接显示内部错误码",
    )
    trace_id: str


class HistoryMessage(BaseModel):
    id: str
    role: Literal["user", "assistant", "tool"]
    source: Literal["text", "voice"] = Field(description="消息来源通道")
    content: str
    created_at: datetime


class HistoryPage(BaseModel):
    items: list[HistoryMessage]
    next_cursor: str | None = None


class ConversationSummary(BaseModel):
    """历史会话列表项。

    对外只给 conversation_id，不给 session_id：取值虽然相同，但露出
    session_id 会让客户端以为可以直接拿去连 WebSocket，而那个运行时会话
    大概率早已过期（Redis TTL 1 小时）。要续聊必须先走 /resume。
    """

    conversation_id: str
    title: str = Field(description="恒非空。初始为用户首句截断值，LLM 生成后升级为语义标题")
    modality: Modality
    created_at: datetime
    last_message_at: datetime
    message_count: int = Field(description="消息条数，一问一答计 2 条")


class ConversationPage(BaseModel):
    items: list[ConversationSummary]
    next_cursor: str | None = Field(default=None, description="null 表示已到底")


class ConversationMessage(BaseModel):
    """回放里的一条消息。没有 tool 角色——工具调用不进对话历史。"""

    id: str
    role: Literal["user", "assistant"]
    content: str
    created_at: datetime


class ConversationMessagePage(BaseModel):
    items: list[ConversationMessage]
    next_cursor: str | None = None


class ConversationRenameRequest(BaseModel):
    """重命名历史会话。

    上限 200 与 agent_conversations.title 的列宽一致 —— 放过去就是写库失败。
    """

    title: str = Field(min_length=1, max_length=200)

    @field_validator("title")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        """去掉两侧空白并拒绝纯空白。

        min_length 挡不住 "   "：空标题会把列表项的主文案清掉，用户看到的是
        一行没有文字的条目，比留着原标题差得多。
        """
        trimmed = value.strip()
        if not trimmed:
            raise ValueError("标题不能为空")
        return trimmed


class TranscriptTurn(BaseModel):
    role: Literal["user", "assistant"]
    text: str
    started_at: datetime | None = None
    ended_at: datetime | None = None


class TranscriptArchiveRequest(BaseModel):
    source: Literal["voice"]
    turns: list[TranscriptTurn] = Field(
        description="App 收到 transcript done 后异步批量上报的整轮字幕"
    )


class VoiceToolExecuteRequest(BaseModel):
    """WebRTC 直连通话里的 Tool Call 回传。

    资金安全红线：客户端严禁在前端直接调交易所，必须把大模型给出的工具指令
    原样 POST 回服务端，由服务端核验 scope / mandate / policies 后执行。
    """

    session_id: str = Field(description="与文字聊天共享的全局唯一 session_id")
    tool_call_id: str = Field(
        min_length=1, max_length=128, description="Provider 的 tool_call_id，用于回灌配对"
    )
    name: str = Field(
        min_length=1, max_length=128, description="客户端上报的工具名（服务端必按 scope 二次校验）"
    )
    arguments: dict[str, Any] = Field(default_factory=dict, description="工具入参")


class VoiceToolExecuteResult(BaseModel):
    """标准 tool result，客户端原样喂回大模型即可。"""

    tool_call_id: str
    name: str
    status: Literal["ok", "error"]
    output: dict[str, Any] | None = None
    trade_draft_id: str | None = Field(
        default=None, description="下单类工具返回的 TradeDraft id；仍需用户确认，不会自动执行"
    )
    trace_id: str


class VoiceUsageTokens(BaseModel):
    """通话用量。数据面直连，Runtime 看不到音频流，只能由客户端上报。"""

    audio_tokens: int = Field(default=0, ge=0, le=100_000_000)
    text_tokens: int = Field(default=0, ge=0, le=100_000_000)


class VoiceSessionEndRequest(BaseModel):
    session_id: str
    provider: Literal["fake", "openai", "volc_rtc", "gemini_live"]
    duration_seconds: float = Field(ge=0, le=86_400, description="本次通话时长（秒）")
    usage_tokens: VoiceUsageTokens = Field(default_factory=VoiceUsageTokens)
    transcript_summary: str | None = Field(
        default=None,
        max_length=4000,
        description="通话摘要；写入同一条记忆流，供挂断后继续打字时无缝衔接",
    )


class VoiceSessionEndResult(BaseModel):
    session_id: str
    provider: str
    state: str = Field(description="会话当前状态；结账不负责停止 Provider 任务")
    duplicate: bool = Field(description="true 表示重复上报，本次未产生新的扣减")
    billed_units: str = Field(description="本次通话计费单元（Decimal 字符串）")
    total_billed_units: str = Field(description="该用户累计计费单元")
    remaining_units: str | None = Field(default=None, description="剩余额度；未配额度时为 null")
    memory_episode_id: str | None = Field(default=None, description="通话摘要写入的记忆条目 id")
    trace_id: str


class TradeDraftCardRequest(BaseModel):
    """自然语言下单意图 → 构造 AgentOS 交易草稿。

    客户端只选择 strategy_id；账户、Venue 与 Mandate **不由客户端传入**，
    Runtime 使用 Core AGENT Token 读取权威 StrategyBinding 后精确解析。
    """

    model_config = ConfigDict(extra="forbid")

    session_id: str
    strategy_id: str = Field(min_length=3, max_length=128)
    text: str = Field(description="自然语言下单意图，如「用 3000 限价买 1.5 个 ETH」")


class StrategyTradeDraftRequest(BaseModel):
    """策略上下文内的自然语言下单意图。

    与 `TradeDraftCardRequest` 的区别：策略场景下账户/权限上下文由
    `strategy_id`（路径）+ `X-User-Id`/`X-Agent-Id`（请求头）派生，不需要
    也不接受客户端传入 `session_id`。
    """

    model_config = ConfigDict(extra="forbid")

    text: str = Field(description="自然语言下单意图，如「买 0.001 BTC 限价 3000」")


class TradeDraftCard(BaseModel):
    """交易草稿的用户确认卡片。confirm 仍由用户端持 step-up 发起，本响应不代为确认。"""

    card: str = Field(description="给用户看的中文确认卡片文本")
    draft_id: str | None = None
    core_draft_id: str | None = Field(
        default=None, description="Core 返回的真实 Trade Draft id；与 draft_id 相同"
    )
    draft_version: int | None = None
    status: str | None = None
    draft: dict[str, Any] = Field(default_factory=dict, description="原始 TradeDraftResponse")
    trace_id: str


_TP_DESC = "止盈比例，0~1（0.15 表示 +15% 止盈）。留空表示不设"
_SL_DESC = "止损比例，0~1（0.05 表示 -5% 止损）。留空表示不设"


class StrategyCreateRequest(BaseModel):
    name: str
    template_id: str
    runtime_env: Literal["live", "paper", "backtest"]
    allocation: str
    capital_capacity: str
    strategy_capacity: str
    max_leverage: str
    allowed_assets: list[str] | None = None
    expires_at: datetime | None = None
    take_profit_pct: str | None = Field(default=None, description=_TP_DESC)
    stop_loss_pct: str | None = Field(default=None, description=_SL_DESC)


class StrategyTemplateView(BaseModel):
    """官方策略模板。params_schema 是 JSON Schema，前端据此生成参数表单。"""

    template_id: str
    name: str
    description: str
    recommended_leverage: str
    default_allocation: str
    risk_level: Literal["LOW", "MEDIUM", "HIGH"]
    params_schema: dict[str, Any]


class StrategyTemplateList(BaseModel):
    items: list[StrategyTemplateView]


class StrategyAllocateRequest(BaseModel):
    allocation: str


class StrategyMetricsView(BaseModel):
    """收益统计。注意两种量纲：`_pct` 是百分数，`win_rate`/`max_drawdown` 是 0~1 比例。"""

    total_pnl: float = Field(default=0.0, description="累计收益金额，如 805.50")
    total_pnl_pct: float = Field(default=0.0, description="累计收益率百分比，如 3.66 表示 3.66%")
    win_rate: float = Field(default=0.0, description="胜率，0.0 ~ 1.0，如 0.666")
    max_drawdown: float = Field(default=0.0, description="最大回撤比例，0.0 ~ 1.0，如 0.125")


class StrategyView(StrategyMetricsView):
    strategy_id: str
    user_id: str
    agent_id: str
    name: str
    template_id: str
    runtime_env: str
    allocation: str
    capital_capacity: str
    strategy_capacity: str
    max_leverage: str
    allowed_assets: list[str] | None
    expires_at: datetime | None
    take_profit_pct: str | None = Field(default=None, description=_TP_DESC)
    stop_loss_pct: str | None = Field(default=None, description=_SL_DESC)
    state: str
    version: int


class StrategyList(BaseModel):
    items: list[StrategyView]
    summary: StrategyMetricsView = Field(
        default_factory=StrategyMetricsView,
        description="该 Agent 全部策略合并后的整体收益统计",
    )


class EquityPointView(BaseModel):
    timestamp: int = Field(description="采样时刻的 Unix 秒")
    equity: float = Field(description="该时刻净值")
    pnl: float = Field(description="相对窗口首点的收益金额")
    drawdown_pct: float = Field(description="相对历史峰值的回撤百分比，如 1.2 表示 1.2%")


class EquityHistory(BaseModel):
    points: list[EquityPointView] = Field(default_factory=list)
