"""会话标题生成（首轮结束后异步触发）。

**为什么异步而不是挂在回合里**：统一网关单次实测 1.5~3 分钟（见
app/agents/llm.py 的超时注释），挂在回合响应路径上等于把首字延迟拖到分钟级。
标题只在「历史」弹窗里出现，晚几分钟到完全无感。

**这一层的契约是「尽力而为」**：生成不出来就什么都不做——会话行建的时候
已经写了首句截断值（app/history/store.py 的 fallback_title），保持原样即是
降级，不需要额外的回退分支，也不需要在读路径上算任何东西。

同一份工程约定见 app/news/title_translator.py：那边是「翻不出就留英文」，
这边是「生不出就留首句」，两者都属于「宁可留个次优值，也不能因为网关抖动
就写坏数据或阻断主流程」。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any, Final, Protocol

from app.agents.llm import LLMMessage
from app.history.store import fallback_title

logger = logging.getLogger(__name__)

# 模型爱把 JSON 包在 ```json 围栏或一段客套话里。只取最外层的花括号块，
# 比要求模型「只输出 JSON」可靠——那个要求实测拦不住。
_JSON_OBJECT_RE: Final = re.compile(r"\{.*\}", re.DOTALL)

# 与 agent_conversations.title 的列宽一致。模型不听话写一整段的话，
# 不截断就是直接写库失败（PG 会报 value too long）。
TITLE_COLUMN_LIMIT: Final[int] = 200

_SYSTEM_PROMPT: Final = (
    "你是对话归档助手，给一轮金融问答起一个简短的会话标题。\n"
    "要求：\n"
    "1. 12 个汉字以内，是标题不是句子：不加句号、引号、书名号\n"
    "2. 必须点出这轮聊的具体标的或主题（BTC / ETH / 持仓 / 资金费率等）\n"
    "3. 保留代币与股票代码的原写法\n"
    "4. 不要用「关于」「咨询」「询问」这类没有信息量的词开头\n"
    "5. 只输出 JSON 对象，键为 title，不要输出别的内容\n"
    '示例输出：\n{"title": "BTC行情分析"}'
)

# 送进 prompt 的正文上限。答案可能有 800 字，全塞进去纯属白烧 token——
# 起标题只需要看出主题，开头一段足够。
_EXCERPT_LIMIT: Final[int] = 200


class _Completer(Protocol):
    async def complete(
        self, system: str, messages: tuple[LLMMessage, ...], tools: tuple[str, ...]
    ) -> Any: ...


class _TitleSink(Protocol):
    async def set_title(
        self, session_id: str, title: str, *, only_if: str | None = None
    ) -> None: ...


class ConversationTitler:
    """给会话生成语义标题。任何失败都只是「标题没升级」，从不抛错。"""

    def __init__(self, llm: _Completer, store: _TitleSink) -> None:
        self._llm = llm
        self._store = store

    async def generate(self, session_id: str, user_text: str, assistant_text: str) -> None:
        title = await self._ask(user_text, assistant_text)
        if title is None:
            return
        try:
            # 守卫：只在标题还是建行时那个降级值的时候才升级。用户可能在这
            # 1.5~3 分钟里手动改了名，无条件写会把他改的名字冲掉而毫无反馈。
            #
            # 守卫值必须与 record_turn 建行时写入的值逐字一致，所以两边共用
            # fallback_title —— 各算一次截断、规则稍有出入的话守卫永远不匹配，
            # 表现是标题永远升级不了，而且完全静默。
            await self._store.set_title(session_id, title, only_if=fallback_title(user_text))
        except asyncio.CancelledError:
            raise
        except Exception:
            # 这是个 fire-and-forget 任务，抛出去只会变成无人接收的
            # task exception，还会在日志里以更难读的形式出现。
            logger.warning(
                "会话标题回写失败，保留降级标题 session_id=%s", session_id, exc_info=True
            )

    async def _ask(self, user_text: str, assistant_text: str) -> str | None:
        """问模型要一个标题。拿不到可用结果一律返回 None。"""

        # 两侧都给：只给问题的话，「帮我看看」这种开场白生不出有信息量的标题。
        prompt = (
            f"用户提问：{user_text[:_EXCERPT_LIMIT]}\n"
            f"助手回答：{assistant_text[:_EXCERPT_LIMIT]}"
        )
        try:
            result = await self._llm.complete(
                _SYSTEM_PROMPT, (LLMMessage(role="user", content=prompt),), ()
            )
        except asyncio.CancelledError:
            # 容器关停走的是这条路，不能被当成普通失败咽掉
            raise
        except Exception:
            logger.warning("会话标题生成失败，保留降级标题", exc_info=True)
            return None
        return _parse_title(getattr(result, "text", ""))


def _parse_title(text: str) -> str | None:
    """从模型回复里抽出标题。任何一步不可用都返回 None。"""

    match = _JSON_OBJECT_RE.search(text or "")
    if match is None:
        # 空回复是真实存在的一种失败：max_tokens 被隐藏推理吃光时，网关
        # 会安静地回一个空字符串（见 llm-unified-gateway 的实测记录）。
        logger.warning("会话标题回复里没有 JSON 对象，保留降级标题")
        return None
    try:
        parsed = json.loads(match.group())
    except json.JSONDecodeError:
        logger.warning("会话标题回复不是合法 JSON，保留降级标题")
        return None
    if not isinstance(parsed, dict):
        return None
    title = parsed.get("title")
    if not isinstance(title, str) or not title.strip():
        # 空标题会把列表项的主文案清掉，比留着首句截断值差得多。
        return None
    return title.strip()[:TITLE_COLUMN_LIMIT]
