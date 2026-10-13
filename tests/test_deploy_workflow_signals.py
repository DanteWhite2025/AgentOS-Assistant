"""部署脚本的状态信号。

这类问题所有代码评审都看不见 —— 评审看 app/ 和 tests/，workflow 不在任何
任务的文件清单里。所以用测试把关键信号钉住。

背景（2026-08-22 实测踩过）：凭证预检排在 `up -d` 之前，失败时脚本 exit 1、
新容器根本没起。此时 CI 是红的、但 /healthz 返回 ok（旧容器还在跑），
极易误判成「部署了但坏了」，实际是「新代码压根没上线」。
"""

from __future__ import annotations

import pathlib

WORKFLOW = pathlib.Path(__file__).resolve().parents[1] / ".github/workflows/deploy-staging.yml"
GEMINI_PREFLIGHT = (
    pathlib.Path(__file__).resolve().parents[1] / "scripts" / "check_gemini_live_connectivity.py"
)


def _text() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def test_preflight_failure_states_that_new_code_is_not_live() -> None:
    """预检失败必须明说新代码没上线，否则下次还会误判。"""
    text = _text()
    assert "core_credential_preflight" in text
    assert "未上线" in text


def test_preflight_failure_is_trapped_rather_than_bare_set_e() -> None:
    """裸 set -e 直接退出不会留下任何解释，必须显式捕获失败分支。"""
    assert "if ! sudo docker compose -p app run --rm --no-deps agent-runtime" in _text()


def test_deployment_announces_when_new_image_is_live() -> None:
    """反向误判同样要防：up -d 之后的失败，新代码其实已经上线了。"""
    text = _text()
    assert "已上线" in text
    assert text.index("已上线") > text.index("up -d --no-build")


def test_llm_gateway_env_is_still_injected() -> None:
    """网关三项仍必须走 env 块 / envs 清单 / set_env 三处。"""
    text = _text()
    for name in ("AGENT_LLM_BASE_URL", "AGENT_LLM_API_KEY", "AGENT_LLM_MODEL"):
        assert f"set_env {name}" in text
    assert "LLM_BASE_URL,LLM_KEY,LLM_MODEL" in text


def test_gemini_live_can_be_enabled_alongside_the_default_provider() -> None:
    """Gemini Live 的启用清单、Secret、endpoint、model 必须作为一组部署。"""
    text = _text()
    assert "GEMINI_KEY: ${{ secrets.AGENT_GEMINI_API_KEY }}" in text
    assert (
        "VOICE_PROVIDER,ENABLED_VOICE_PROVIDERS,GEMINI_KEY,GEMINI_BASE_URL,GEMINI_LIVE_MODEL"
        in text
    )
    # 渠道缺省仍是 gemini_live，但由仓库 var 决定 —— 切渠道不用改代码。
    assert "VOICE_PROVIDER: ${{ vars.AGENT_REALTIME_VOICE_PROVIDER || 'gemini_live' }}" in text
    assert 'set_env AGENT_REALTIME_VOICE_PROVIDER "$VOICE_PROVIDER"' in text
    assert 'set_env AGENT_ENABLED_VOICE_PROVIDERS "$ENABLED_VOICE_PROVIDERS"' in text
    for name in ("AGENT_GEMINI_API_KEY", "AGENT_GEMINI_BASE_URL", "AGENT_GEMINI_LIVE_MODEL"):
        assert f'set_env_optional {name} "$' in text
    assert "gemini-3.1-flash-live-preview" in text
    assert "gemini-2.5-flash-native-audio-preview" not in text


def test_gemini_credentials_are_required_only_when_the_mode_is_enabled() -> None:
    """Key 过期或被清空时，只有启用 Gemini 能力的部署该失败。

    无条件 set_env 会让任何渠道的部署都被一把 Gemini Key 拖住 —— 而这把 key
    是 AI Studio 临时票格式，本来就有过期风险。
    """
    text = _text()
    assert 'set_env AGENT_GEMINI_API_KEY "$GEMINI_KEY"' not in text
    assert 'case ",$ENABLED_VOICE_PROVIDERS," in' in text
    assert "*,gemini_live,*)" in text
    assert '[ -z "$GEMINI_KEY" ]' in text


def test_gemini_credential_guard_runs_before_the_container_is_replaced() -> None:
    """凭证校验必须排在 up -d 之前，否则旧容器已经被换掉才发现配置不全。"""
    text = _text()
    assert text.index('[ -z "$GEMINI_KEY" ]') < text.index("compose -p app up -d")


def test_gemini_preflight_uses_working_v1beta_wire_without_printing_credentials() -> None:
    text = GEMINI_PREFLIGHT.read_text(encoding="utf-8")
    assert 'f"{base_url}/v1beta/auth_tokens"' in text
    assert "gemini-3.1-flash-live-preview" in text
    assert "v1beta.GenerativeService.BidiGenerateContentConstrained" in text
    assert '"bidiGenerateContentSetup"' in text
    assert '"generationConfig"' in text
    assert "setupComplete" in text
    assert "liveConnectConstraints" not in text
    assert "response.text" not in text
    assert "_mask" not in text


def test_gemini_preflight_blocks_deployment_before_replacing_the_live_container() -> None:
    text = _text()
    command = "python -m app.ops.gemini_live_preflight"
    assert command in text
    assert text.index(command) < text.index("docker compose -p app up -d --no-build")
    assert "Gemini Live v1beta 预检失败：新代码未上线" in text
