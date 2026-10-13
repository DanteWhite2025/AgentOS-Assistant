"""应用配置。所有配置从环境变量读取（前缀 AGENT_），系统边界一次性校验。"""

from __future__ import annotations

import logging
import os
from functools import lru_cache
from typing import Literal

from pydantic import AliasChoices, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.voice.config import load_volc_voice_config, validate_volc_voice_config
from app.voice.speech_text import DEFAULT_BRAND_SPOKEN_AS

logger = logging.getLogger(__name__)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="AGENT_",
        env_file=".env",
        extra="ignore",
        populate_by_name=True,
    )

    app_name: str = "yesup-agent-runtime"
    env: str = "local"

    # Provider 模式：fake（自测，全 Fake）/ real（真实 Core/Voice/LLM）
    provider_mode: Literal["fake", "real"] = "fake"

    # 上游 yesup-core（受控工具调用目标）
    core_base_url: str = Field(
        default="",
        validation_alias=AliasChoices(
            "AGENT_CORE_BASE_URL",
            "AGENT_AGENTOS_STAGING_BASE_URL",
            "AGENT_STAGING_BASE_URL",
            "CORE_BASE_URL",
        ),
    )
    # Runtime AGENT credential has one canonical name.  Legacy token names are
    # deliberately not aliases: a deployment that forgot the formal Secret
    # must fail closed instead of silently selecting another credential.
    core_actor_token: str = Field(default="", validation_alias="AGENT_CORE_ACTOR_TOKEN")
    core_timeout_seconds: float = Field(default=10.0, gt=0, le=60)
    core_credential_agent_id: str = "agent-test-001"
    core_credential_required_scopes: str = "market:read,account:read,trade:draft"
    core_credential_forbidden_scopes: str = (
        "trade:confirm,trade:execute,withdraw:write,withdrawal:write,"
        "transfer:write,funding:write,admin,admin:*"
    )
    core_credential_validation_interval_seconds: int = Field(default=300, ge=5, le=86400)
    core_credential_expiry_warning_seconds: int = Field(default=86400, ge=60, le=2592000)

    @staticmethod
    def _scope_list(value: str) -> tuple[str, ...]:
        return tuple(dict.fromkeys(item.strip() for item in value.split(",") if item.strip()))

    @property
    def required_core_scopes(self) -> tuple[str, ...]:
        return self._scope_list(self.core_credential_required_scopes)

    @property
    def forbidden_core_scopes(self) -> tuple[str, ...]:
        return self._scope_list(self.core_credential_forbidden_scopes)

    # Deprecated base-URL property for internal callers. Token aliases were
    # removed entirely so an old environment name cannot become a credential.
    @property
    def staging_base_url(self) -> str:
        return self.core_base_url

    # 建草稿的账户上下文（会话未提供时的兜底默认）。
    agentos_default_leverage: str = "1"
    agentos_default_max_slippage: str = "0.005"
    # Strategy identity is resolved from Core Trading Readiness/StrategyBinding.
    # Kept only as a deprecated deployment compatibility input; runtime trading
    # code must never use it as an authority.
    agentos_default_strategy_id: str = ""

    # 策略净值采样间隔（秒）。上游无历史净值接口，收益曲线靠这里持续采样攒出来；
    # 设为 0 关闭采样（此时收益字段维持已落盘数据，无数据则为 0）。
    strategy_equity_sample_interval_seconds: int = Field(default=60, ge=0, le=3600)

    # 仅兼容 OpenAI Realtime provider 时使用；火山默认链路不读取。
    openai_api_key: str = Field(
        default="",
        validation_alias=AliasChoices("AGENT_OPENAI_API_KEY", "OPENAI_API_KEY"),
    )

    # OpenAI Realtime 直连入口。走中转站时改这里即可，代码不用动。
    openai_base_url: str = Field(
        default="https://api.openai.com",
        validation_alias=AliasChoices("AGENT_OPENAI_BASE_URL", "OPENAI_BASE_URL"),
    )

    # 实时语音主链路。火山 RTC 为默认；OpenAI / Gemini Live / pipeline 为显式可选渠道。
    # pipeline = 02 方案分工流水线：LLM 出文本 → 分句 → 火山 TTS 2.0 流式合成，
    # 音频经 Runtime 转发（不签票、不直连、无第三方 Bot 进房）。
    realtime_voice_provider: Literal["volc_rtc", "openai", "gemini_live", "pipeline"] = "volc_rtc"
    # 可同时启用多条语音能力；留空时保持向后兼容，只启用默认 Provider。
    # Staging 双模式使用：pipeline,gemini_live。
    enabled_voice_providers: str = ""

    @property
    def enabled_voice_provider_names(self) -> tuple[str, ...]:
        configured = self._scope_list(self.enabled_voice_providers)
        return configured or (self.realtime_voice_provider,)

    # Gemini Live 原生音频。客户端走 WSS 直连（BidiGenerateContentConstrained），
    # 不是 WebRTC；音频仍然不经过 Runtime。落地前先跑
    # scripts/check_gemini_live_connectivity.py 验签票链路。
    gemini_api_key: str = Field(
        default="",
        validation_alias=AliasChoices("AGENT_GEMINI_API_KEY", "GEMINI_API_KEY"),
    )
    gemini_base_url: str = "https://generativelanguage.googleapis.com"
    gemini_live_model: str = "gemini-3.1-flash-live-preview"
    # 短期票有效期；到期客户端向 /sessions/{id}/token 续签。
    gemini_token_ttl_seconds: int = Field(default=1800, ge=60, le=86400)
    # 「这张票还能用来开新会话」的窗口。拿到票应当立刻连，故意留得很短。
    gemini_session_start_window_seconds: int = Field(default=60, ge=10, le=600)
    rtc_app_id: str = ""
    rtc_app_key: str = ""
    rtc_token_ttl_seconds: int = Field(default=600, ge=120, le=86400)
    volc_access_key_id: str = ""
    volc_secret_access_key: str = ""
    volc_rtc_endpoint: str = "https://rtc.volcengineapi.com"
    volc_rtc_region: str = "cn-north-1"
    volc_rtc_api_version: str = "2024-12-01"
    volc_voice_welcome_message: str = "你好，我是 YesUP 智能助理。"
    # 品牌名送进 TTS 时替换成的念法（书面写法始终是 YesUP，只改朗读那一份）。
    # 火山对「YesUP」的默认读法是「yesuv」，只能靠改写朗读文本纠正；真机听感
    # 不合适时改这个变量即可，不必改代码。
    voice_brand_spoken_as: str = DEFAULT_BRAND_SPOKEN_AS
    volc_voice_chat_config_json: str = ""
    volc_asr_end_smooth_window_ms: int = Field(default=800, ge=500, le=50000)
    volc_callback_url: str = ""
    volc_callback_secret: str = ""
    volc_function_callback_url: str = ""
    volc_function_callback_secret: str = ""
    volc_ark_model: str = ""
    volc_ark_endpoint_id: str = ""
    volc_bot_join_timeout_seconds: int = Field(default=30, ge=5, le=120)
    volc_bot_presence_poll_seconds: float = Field(default=1.0, ge=0.25, le=10.0)
    volc_asr_silence_timeout_seconds: float = Field(default=12.0, ge=3.0, le=60.0)
    volc_function_progress_delay_seconds: float = Field(default=2.0, ge=0.0, le=10.0)

    # 通话计费口径：每 N 秒记一个时长单元、每 M 个 token 记一个 token 单元，
    # 均向上取整后相加。quota 为 0 表示不限额（此时不返回 remaining_units）。
    voice_billing_seconds_per_unit: int = Field(default=60, ge=1, le=3600)
    voice_billing_tokens_per_unit: int = Field(default=1000, ge=1, le=1_000_000)
    voice_quota_units_per_user: int = Field(default=0, ge=0, le=10_000_000)

    # 语音时长服务端兜底（spec §7.1 / D1）。火山回调的 EventType 取值需先在
    # staging 用真实回调确认，故默认留空 = 兜底关闭，此时时长仍只取客户端上报。
    # 确认取值后填这两项即可启用，不必改代码。
    voice_room_join_event_types: tuple[str, ...] = ()
    voice_room_leave_event_types: tuple[str, ...] = ()
    # 客户端与服务端时长的允许偏差比例，超出即标记 suspicious 并告警。
    voice_duration_drift_tolerance: float = Field(default=0.1, ge=0.0, le=1.0)

    # 用量与成本核算（spec §4.2 / D4）。每日汇率接入属二期，一期用固定兜底值：
    # 没有汇率时 CNY 供应商的成本会全落 unpriced，宁可用一个近似值。
    usage_fx_cny_to_usd: float = Field(default=0.1385, gt=0.0, le=100.0)
    # LLM 成本归到哪个 provider 名下计价。换网关或换主力模型时改这里。
    usage_llm_provider: str = "anthropic"

    # ElevenLabs 对话式语音（经中转 BaseURL）。仅用户 key 进 runtime；admin key 不在此定义（最小权限）。
    elevenlabs_base_url: str = "https://elevenlabsapi.chatshare.org"
    elevenlabs_api_key: str = ""
    elevenlabs_voice_id: str = "JBFqnCBsd6RMkjVDRZzb"  # 英文音色（George·温暖），Max 人设的嘴
    elevenlabs_model_id: str = "eleven_multilingual_v2"
    elevenlabs_stream_model: str = (
        "eleven_flash_v2_5"  # 流式说（stream-input WS）用：低时延，不支持 v3
    )
    elevenlabs_speed: float = 1.15  # 语速：1.0=正常，>1 更快（0.7~1.2），可调
    elevenlabs_agent_id: str = ""  # convai 用，已弃用（中转禁 convai）

    # ---- Demo 多语音链路（6 条 provider；团队 2026-08-02 确认默认 = 火山/豆包）----
    default_voice_provider: str = "huochan"
    # 豆包语音合成2.0（seed-tts-2.0，V3 单向流式 SSE，单 API Key 自包含，无需 AppID）。
    # API Key 获取：火山控制台 → 语音技术 → API Key 管理。
    huochan_api_key: str = ""  # X-Api-Key（只需填这个）
    huochan_s2s_app_id: str = ""  # 端到端实时语音控制台中的 App ID（不是 RTC AppId）
    huochan_s2s_token: str = ""  # 与 S2S App ID 配对的端到端语音 Token；不是 TTS API Key
    huochan_tts_url: str = "https://openspeech.bytedance.com/api/v3/tts/unidirectional/sse"
    huochan_resource_id: str = "seed-tts-2.0"
    # req_params.speaker，大模型 *_bigtts 音色。09 甜美小源 = 团队听感评审签收的
    # 二次元元气少女音，替掉了偏播音腔的 Gemini Leda。换音色只改这里，不用动代码。
    huochan_voice_type: str = "zh_female_tianmeixiaoyuan_uranus_bigtts"
    # audio_params.speed_ratio：1.0=正常，1.15 更轻快、更贴少女感（评审签收值）。
    huochan_speed_ratio: float = 1.15
    # 火山端到端实时语音（S2S 主通道，阶段 B 再接）
    huochan_base_url: str = ""
    minimax_base_url: str = ""  # MiniMax 海螺（中文情绪）
    minimax_api_key: str = ""
    qwen_base_url: str = ""  # 阿里 Qwen-Omni Realtime（原生 WebRTC）
    qwen_api_key: str = ""
    # Gemini 的 key / base_url 见上方「实时语音主链路」段，此处不再重复定义。
    deepgram_api_key: str = ""  # 自建管道 STT（Deepgram + Claude + ElevenLabs）
    anthropic_api_key: str = ""  # 自建管道 LLM（Claude）

    # ---- 文本推理统一网关（自建 New API 中转站）----
    # 全部文本推理走这一组配置，代码里不再有 provider 分支。中转站是 OpenAI
    # 兼容协议，换模型/换上游只改 AGENT_LLM_MODEL，不用发版。
    #
    # 三项均无默认值且在 real 模式必填：中转站地址和模型名一旦缺失，请求会
    # 打到错误的地方或用错模型名，与其运行时静默失败，不如启动即失败。
    llm_base_url: str = ""
    llm_api_key: str = ""
    llm_model: str = ""
    # 120s：交互式对话的可接受上限。不设的话走 openai SDK 默认的 600 秒 ——
    # 一个卡住的请求会占住 worker 十分钟。原本是 60s，但 claude-opus-4-8 实测隐藏
    # reasoning_tokens 就能吃掉 1500~1900 token，2048 的输出上限下这些推理开销
    # 之外几乎不剩可见文本预算；配合 max_tokens 一起调宽，见下面注释。
    llm_timeout_seconds: float = Field(default=120.0, gt=0, le=600)
    # 整轮对话的时间上限。每一层各有各的超时（Tavily 5s、Core 10s + 重试、
    # LLM 120s），但没有任何一层管得住"加起来多久"——最坏情况是 4 步循环各自
    # 把预算用满。
    #
    # **必须大于 llm_timeout_seconds**，否则一次合法的慢推理（opus 的隐藏
    # reasoning token 会吃掉大半预算，120s 那个熔断就是为它调的）会先被回合
    # 超时砍掉，用户拿到一句"想得有点久"而不是本该拿到的答案。180 = 120（一次
    # 慢推理）+ 60（工具与其余步骤）。要防的是多步累积拖成几分钟，不是单步慢。
    # 0 表示不限时，留给排查场景。
    turn_timeout_seconds: float = Field(default=180.0, ge=0, le=600)
    # 输出 token 熔断：实测过开放式问题（如"深度投研分析"）不设上限时，模型可能
    # 一次吐上万 output token、耗时 4~5 分钟。但上限也不能设太小——claude-opus-4-8
    # 每次调用的隐藏 reasoning_tokens 就能吃掉 1500~1900 token（且每次浮动），实测
    # 2048 时曾经推理阶段就把预算耗尽、可见文本返回空字符串。4096 留出推理开销之外
    # 至少 2000+ token 的可见内容空间，~50 token/s 吞吐下最坏耗时约 80~100s，在上面
    # 120s 超时阈值内。
    llm_max_output_tokens: int = Field(default=4096, ge=1, le=200_000)
    # Deprecated（2026-09-02 起未再读取，字段/别名保留仅为兼容旧 env 注入）：
    # ask_investment_expert 曾经独立配一个快速模型做"深度分析"，那次调用只
    # 拿到用户原话、不给任何行情数据，是"回答很水"的根因之一，也是三跳 LLM
    # 串联里最贵的一跳。现已改为纯数据组装（app/services/market_evidence.py），
    # 深度分析工作转移给主脑（AGENT_LLM_MODEL）在最终回复那一轮完成。见
    # docs/plans/2026-09-02-investment-expert-evidence/。
    investment_expert_llm_model: str = Field(
        default="",
        validation_alias=AliasChoices(
            "AGENT_INVESTMENT_EXPERT_LLM_MODEL",
            "AGENT_FAST_LLM_MODEL",
            "AGENT_MINARA_LLM_MODEL",
            "INVESTMENT_EXPERT_LLM_MODEL",
            "FAST_LLM_MODEL",
            "MINARA_LLM_MODEL",
        ),
    )
    # Deprecated（2026-09-02 起未再读取）：曾经缓存的是投研 LLM 调用的回复；
    # 那次调用已不存在，且缓存过期行情价格做点位归因比套话更糟，故随之下线。
    investment_expert_cache_ttl_seconds: int = Field(
        default=600,
        ge=0,
        le=86400,
        validation_alias=AliasChoices(
            "AGENT_INVESTMENT_EXPERT_CACHE_TTL_SECONDS",
            "INVESTMENT_EXPERT_CACHE_TTL_SECONDS",
        ),
    )

    @property
    def minara_llm_model(self) -> str:
        """向后兼容历史命名。"""
        return self.investment_expert_llm_model

    # Followin 新闻聚合（MCP 端点，x-api-key 鉴权）。实测单次调用约 2s。
    followin_api_key: str = Field(
        default="",
        validation_alias=AliasChoices("AGENT_FOLLOWIN_API_KEY", "FOLLOWIN_API_KEY"),
    )
    followin_mcp_url: str = "https://mcp.followin.io/v2/mcp"
    followin_timeout_seconds: float = Field(default=30.0, gt=0, le=120)

    # Tavily Search（联网搜索，Agent 回答用户非行情类问题时调用）
    tavily_api_key: str = Field(
        default="",
        validation_alias=AliasChoices("AGENT_TAVILY_API_KEY", "TAVILY_API_KEY"),
    )
    # 硬超时 5 秒：联网搜索是快速通道的一环，宁可搜不到也不能拖慢整轮对话。
    tavily_timeout_seconds: float = Field(default=5.0, gt=0, le=60)
    # 联网搜索结果短时缓存 TTL（秒），命中同一天内的重复热点问题时跳过 Tavily。0 表示禁用缓存。
    tavily_search_cache_ttl_seconds: int = Field(default=90, ge=0, le=300)
    # 搜索超时/限流/5xx 时的退避重试次数。上限 3 是刻意的：用户正在等回答，
    # 5 秒超时叠加多次重试会把一次搜索拖成十几秒，比"没搜到"更难接受。
    tavily_max_retries: int = Field(default=1, ge=0, le=3)
    # web_search 命中本地 Followin 新闻库时的回看窗口（小时），超出窗口的旧文章不算命中。
    news_local_search_lookback_hours: int = Field(default=24, ge=1, le=168)

    # 采集默认关：由运维显式确认后再打开（沿用原 grok_monitor 的保守风格）
    news_collect_enabled: bool = False
    # 上游无分页、limit 上限 50，media 源实测 2 条/分 → 50 条约 25 分钟耗尽。
    # 300s 留 5x 余量，足以扛新闻潮密度翻倍。
    news_collect_interval_seconds: int = Field(default=300, ge=60, le=3600)
    news_collect_limit: int = Field(default=50, ge=1, le=50)
    # 一次性冷启动预热：上游无历史回填能力，首轮只能拿到最近约 25 分钟的内容
    news_warmup_on_start: bool = False
    # 入库时把英文标题翻成中文（推荐流的 Followin trending 只给英文标题、
    # 正文却是中文摘要）。真实模式默认开；关掉只影响新入库条目的标题语言，
    # 采集本身不受影响。
    news_translate_titles: bool = True
    # /news 未传 time_range 时的护栏：2 字中文关键词无法用 pg_trgm 索引，
    # 必须钉住扫描范围
    news_default_time_range_days: int = Field(default=30, ge=1, le=365)
    news_retention_days: int = Field(default=90, ge=1, le=3650)
    news_query_rate_limit: int = Field(default=60, ge=0, le=100000)
    news_query_rate_window_seconds: int = Field(default=60, ge=1, le=86400)

    # 历史对话（/v1/agent/conversations）。三项都带默认值，一个必填项都没有：
    # 本仓库 push 到 main 即部署 real，新必填项漏配 set_env 会让整个服务起不来。
    # 关掉只停标题生成与清理循环，归档与读接口不受影响。
    history_enabled: bool = True
    session_retention_days: int = Field(default=30, ge=1, le=3650)
    # 刻意是小时级而不是 86400。RoundLock 的 lock_ttl_for 算出的锁 TTL 是
    # interval - 30s，日间隔会得到 86370s 的锁——滚动重启后当天就不再执行，
    # 保留期静默失效。按 cutoff 幂等删除，没数据可删就是 0 行，不浪费。
    session_purge_interval_seconds: int = Field(default=3600, ge=60, le=86400)

    # 每日热点早报：无鉴权公开端点，未命中缓存会触发一次真实付费 LLM 调用，
    # 限流比 /news（纯本地 DB 查询）严格得多。缓存命中不占配额；日本节点前面
    # 若是 nginx，request.client.host 往往是 127.0.0.1，全国用户挤同一个桶。
    # 刷新失败会继续返回上一份成功结果；冷启动失败则按 cooldown 停手。
    hotspots_cache_ttl_seconds: float = Field(default=1800.0, ge=1, le=21600)
    hotspots_error_cooldown_seconds: float = Field(default=60.0, ge=1, le=3600)
    hotspots_rate_limit: int = Field(default=10, ge=0, le=100000)
    hotspots_rate_window_seconds: int = Field(default=60, ge=1, le=86400)

    # ---- 语音识别 STT（「对着说话」）----
    # 每个人设用自己 provider 的耳：小雅→火山流式 STT2.0（我方后端中转），Max→ElevenLabs Scribe（浏览器直连）
    stt_provider: str = "elevenlabs"  # /stt 整段识别兜底用；实时听按人设分流
    elevenlabs_stt_model: str = "scribe_v1"  # 整段识别
    elevenlabs_stt_realtime_model: str = "scribe_v2_realtime"  # 实时流式（边说边出字，WS）
    huochan_asr_url: str = "https://openspeech.bytedance.com/api/v3/auc/bigmodel/recognize/flash"
    # 豆包流式语音识别模型2.0（sauc，二进制 WS）。bigmodel_async=双向流式优化版（时延最优）。
    # resource_id：小时版 volc.seedasr.sauc.duration / 并发版 volc.seedasr.sauc.concurrent（按开通选）。
    huochan_asr_ws_url: str = "wss://openspeech.bytedance.com/api/v3/sauc/bigmodel_async"
    huochan_asr_resource_id: str = "volc.seedasr.sauc.duration"

    # 凭证
    rt_token_secret: str = "dev-secret-change-me"
    rt_token_ttl_seconds: int = 300
    rt_ws_base: str = "ws://localhost:8100"

    # 模型
    voice_model: str = "gpt-realtime-2.1"
    text_model: str = "openai:gpt-5"

    # 基础设施（MVP 骨架可留空）
    redis_url: str = "redis://localhost:6379/0"
    database_url: str = ""

    # 可观测性
    otel_enabled: bool = False
    otel_exporter_otlp_endpoint: str = ""

    @model_validator(mode="after")
    def validate_turn_budget(self) -> Settings:
        """回合预算必须容得下一次最慢的推理。

        两个默认值是分别调出来的，很容易在后续调整时漂移到互相打架：回合预算
        小于单步 LLM 熔断时，一次合法的慢推理会先被回合超时砍掉，表现为用户
        永远拿不到那类问题的答案，而日志里只有一句 TURN_TIMEOUT。宁可启动就
        失败，也不要让它悄悄生效。
        """

        if 0 < self.turn_timeout_seconds <= self.llm_timeout_seconds:
            raise ValueError(
                "turn_timeout_seconds 必须大于 llm_timeout_seconds"
                f"（当前 {self.turn_timeout_seconds} <= {self.llm_timeout_seconds}），"
                "否则单次合法的慢推理会先被回合超时砍掉"
            )
        return self

    @model_validator(mode="after")
    def validate_runtime_mode(self) -> Settings:
        legacy_names = (
            "AGENT_AGENTOS_STAGING_BASE_URL",
            "AGENT_STAGING_BASE_URL",
            "AGENT_AGENTOS_STAGING_TOKEN",
            "AGENTOS_STAGING_TOKEN",
            "AGENT_STAGING_TOKEN",
        )
        configured_legacy = [name for name in legacy_names if os.environ.get(name)]
        if configured_legacy:
            logger.warning(
                "deprecated Core configuration names ignored or superseded names=%s; "
                "AGENT_CORE_ACTOR_TOKEN is the only accepted token name",
                ",".join(configured_legacy),
            )
        env = self.env.strip().lower()
        if self.provider_mode == "fake":
            if env in {"staging", "production", "prod"}:
                raise ValueError("Fake providers are forbidden in staging/production")
            return self

        common_missing = [
            name
            for name, value in (
                ("AGENT_CORE_ACTOR_TOKEN", self.core_actor_token),
                ("AGENT_LLM_BASE_URL", self.llm_base_url),
                ("AGENT_LLM_API_KEY", self.llm_api_key),
                ("AGENT_LLM_MODEL", self.llm_model),
                # 缺 Key 时 Followin 会静默退化成 Fake 新闻，喂给交易决策链很危险，
                # 宁可启动失败也不放模拟数据进 real 环境
                ("AGENT_FOLLOWIN_API_KEY", self.followin_api_key),
                ("CORE_BASE_URL", self.core_base_url),
                ("RT_WS_BASE", self.rt_ws_base),
            )
            if not value.strip()
        ]
        enabled_voice = self.enabled_voice_provider_names
        allowed_voice = {"volc_rtc", "openai", "gemini_live", "pipeline"}
        unknown_voice = sorted(set(enabled_voice) - allowed_voice)
        if unknown_voice:
            raise ValueError(
                "AGENT_ENABLED_VOICE_PROVIDERS contains unsupported providers: "
                + ", ".join(unknown_voice)
            )
        if self.realtime_voice_provider not in enabled_voice:
            raise ValueError(
                "AGENT_ENABLED_VOICE_PROVIDERS must include AGENT_REALTIME_VOICE_PROVIDER"
            )

        voice_values: list[tuple[str, str]] = []
        if "openai" in enabled_voice:
            voice_values.append(("OPENAI_API_KEY", self.openai_api_key))
        if "gemini_live" in enabled_voice:
            voice_values.append(("AGENT_GEMINI_API_KEY", self.gemini_api_key))
        if "pipeline" in enabled_voice:
            voice_values.append(("AGENT_HUOCHAN_API_KEY", self.huochan_api_key))
        if "volc_rtc" in enabled_voice:
            voice_values.extend(
                (
                    ("AGENT_RTC_APP_ID", self.rtc_app_id),
                    ("AGENT_RTC_APP_KEY", self.rtc_app_key),
                    ("AGENT_VOLC_ACCESS_KEY_ID", self.volc_access_key_id),
                    ("AGENT_VOLC_SECRET_ACCESS_KEY", self.volc_secret_access_key),
                    ("AGENT_HUOCHAN_S2S_APP_ID", self.huochan_s2s_app_id),
                    ("AGENT_HUOCHAN_S2S_TOKEN", self.huochan_s2s_token),
                    ("AGENT_VOLC_CALLBACK_URL", self.volc_callback_url),
                    ("AGENT_VOLC_CALLBACK_SECRET", self.volc_callback_secret),
                    ("AGENT_VOLC_FUNCTION_CALLBACK_URL", self.volc_function_callback_url),
                    ("AGENT_VOLC_FUNCTION_CALLBACK_SECRET", self.volc_function_callback_secret),
                    ("AGENT_VOLC_ARK_MODEL", self.volc_ark_model),
                    ("AGENT_VOLC_ARK_ENDPOINT_ID", self.volc_ark_endpoint_id),
                )
            )
        missing = common_missing + [name for name, value in voice_values if not value.strip()]
        if missing:
            raise ValueError(f"real provider mode missing required settings: {', '.join(missing)}")
        if not self.core_credential_agent_id.strip():
            raise ValueError("AGENT_CORE_CREDENTIAL_AGENT_ID must not be empty")
        if not self.required_core_scopes:
            raise ValueError("AGENT_CORE_CREDENTIAL_REQUIRED_SCOPES must not be empty")
        if "volc_rtc" in enabled_voice and len(self.rtc_app_id) != 24:
            raise ValueError("AGENT_RTC_APP_ID must be exactly 24 characters")
        if "gemini_live" in enabled_voice:
            if self.gemini_live_model != "gemini-3.1-flash-live-preview":
                raise ValueError("AGENT_GEMINI_LIVE_MODEL must be gemini-3.1-flash-live-preview")
            if self.gemini_base_url.rstrip("/") != ("https://generativelanguage.googleapis.com"):
                raise ValueError(
                    "AGENT_GEMINI_BASE_URL must use the official Google v1beta endpoint"
                )
        if "volc_rtc" in enabled_voice and len(self.volc_callback_secret) < 32:
            raise ValueError("AGENT_VOLC_CALLBACK_SECRET must contain at least 32 characters")
        if "volc_rtc" in enabled_voice and len(self.volc_function_callback_secret) < 32:
            raise ValueError(
                "AGENT_VOLC_FUNCTION_CALLBACK_SECRET must contain at least 32 characters"
            )
        if "volc_rtc" in enabled_voice and (
            not self.volc_ark_endpoint_id.startswith("ep-")
            or self.volc_ark_endpoint_id.startswith("ep-m-")
        ):
            raise ValueError(
                "AGENT_VOLC_ARK_ENDPOINT_ID must be a custom ep-* endpoint, not a preset ep-m-* endpoint"
            )
        if "volc_rtc" in enabled_voice:
            voice_config = load_volc_voice_config(
                override_json=self.volc_voice_chat_config_json,
                s2s_app_id=self.huochan_s2s_app_id,
                s2s_token=self.huochan_s2s_token,
                end_smooth_window_ms=self.volc_asr_end_smooth_window_ms,
            )
            validate_volc_voice_config(voice_config)

        import ipaddress
        from urllib.parse import urlparse

        parsed_ws = urlparse(self.rt_ws_base)
        host = (parsed_ws.hostname or "").lower()
        if parsed_ws.scheme != "wss" or not host:
            raise ValueError("real provider mode requires a public wss:// RT_WS_BASE")
        if host in {"localhost", "127.0.0.1", "::1"} or host.endswith((".localhost", ".local")):
            raise ValueError("real provider mode forbids localhost RT_WS_BASE")
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            address = None
        if address is not None and not address.is_global:
            raise ValueError("real provider mode requires a public RT_WS_BASE")
        if "volc_rtc" in enabled_voice:
            for setting_name, callback_url in (
                ("AGENT_VOLC_CALLBACK_URL", self.volc_callback_url),
                ("AGENT_VOLC_FUNCTION_CALLBACK_URL", self.volc_function_callback_url),
            ):
                callback = urlparse(callback_url)
                callback_host = (callback.hostname or "").lower()
                if (
                    callback.scheme != "https"
                    or not callback_host
                    or callback.username is not None
                    or callback.password is not None
                    or callback.query
                    or callback.fragment
                ):
                    raise ValueError(
                        f"{setting_name} must be a public https URL without credentials, "
                        "query, or fragment"
                    )
                if callback_host in {"localhost", "127.0.0.1", "::1"} or callback_host.endswith(
                    (".localhost", ".local")
                ):
                    raise ValueError(f"{setting_name} must not use a loopback host")
                try:
                    callback_address = ipaddress.ip_address(callback_host)
                except ValueError:
                    callback_address = None
                if callback_address is not None and not callback_address.is_global:
                    raise ValueError(f"{setting_name} must use a public host")
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """进程内单例配置。"""
    return Settings()
