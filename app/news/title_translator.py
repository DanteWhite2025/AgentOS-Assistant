"""英文标题 → 中文标题（入库前一次性翻译）。

**为什么在入库时翻而不是查询时翻**：查询是用户点开新闻页的同步路径，
统一网关当前实测单次 1.5~3 分钟（见 llm.py 里的超时注释），挂在读路径上
等于把新闻页拖死；而同一条新闻会被成千次读取、只入库一次。

**为什么只翻标题**：Followin trending 的正文本来就是中文结构化摘要，
缺的只有标题。判据见 `needs_zh_title`。

**这一层的契约是「尽力而为」**：翻译不成就返回空映射，调用方保留英文原
标题照常入库。宁可列表里有一行英文，也不能因为网关抖动就让新闻停更——
这个模块的上游（Followin 采集）已经因为「把可选信号当硬前置条件」静默
停更过一周，同样的错不能在这里再犯一次。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Sequence
from typing import Any, Final, Protocol

from app.agents.llm import LLMMessage

logger = logging.getLogger(__name__)

_CJK_RE: Final = re.compile(r"[一-鿿]")
# 模型爱把 JSON 包在 ```json 围栏或一段客套话里。只取最外层的花括号块，
# 比要求模型"只输出 JSON"可靠——那个要求实测拦不住。
_JSON_OBJECT_RE: Final = re.compile(r"\{.*\}", re.DOTALL)
# 键的容错：模型会回 "1" / " 1 " / "1." / "#1"，都当同一个编号。
_INDEX_RE: Final = re.compile(r"\d+")

_SYSTEM_PROMPT: Final = (
    "你是财经新闻编辑，把英文新闻标题翻译成简体中文标题。\n"
    "要求：\n"
    "1. 保留数字、百分比、代币与股票代码（BTC / NVDA 等）、公司名的通用译法\n"
    "2. 是新闻标题不是句子：不加句号，不加引号，不做解释与扩写\n"
    "3. 保持原意，不推测原文没说的因果\n"
    "4. 只输出 JSON 对象，键是输入编号的字符串，值是中文标题，不要输出别的内容\n"
    '示例输入：\n1. BTC breaks 71500\n示例输出：\n{"1": "BTC 突破 71500"}'
)

_ITEM_SYSTEM_PROMPT: Final = (
    "你是财经新闻编辑，请将输入的英文新闻翻译为简体中文标题，并将正文提炼翻译为客观精简的中文资讯摘要（约100~150字）。\n"
    "要求：\n"
    "1. 保留数字、百分比、代币与股票代码（BTC / NVDA 等）及公司通用译名\n"
    "2. 标题简洁有力，不加句号引号；摘要流畅通顺，保留核心事实与关键数据/论据\n"
    "3. 严禁主观推测或编造未提及的内容\n"
    "4. 只输出合法 JSON 对象，键是输入编号字符串，值是包含 title 与 summary 的对象：\n"
    '{\n  "1": {"title": "中文标题", "summary": "中文精炼摘要"}\n}\n'
    "不要输出任何其他解释或额外文字。"
)

# 单批条数。纯标题 20 条；正文带长文本时缩减为 5 条，避免超出单次输出 token 上限
_DEFAULT_BATCH_SIZE: Final = 20
_ITEM_BATCH_SIZE: Final = 5
_CONTENT_EXCERPT_MAX_CHARS: Final = 1200


def _has_cjk(text: str) -> bool:
    return bool(_CJK_RE.search(text or ""))


def needs_zh_title(*, title: str, content: str = "") -> bool:
    """判断一条新闻是否需要翻译标题（向后兼容保留）。"""
    return bool(title) and not _has_cjk(title)


def needs_zh_translation(*, title: str, content: str = "") -> bool:
    """判断一条新闻是否需要中文化（标题或正文缺少中文）。"""
    need_title = bool(title) and not _has_cjk(title)
    need_content = bool(content) and not _has_cjk(content)
    return need_title or need_content


class _Completer(Protocol):
    async def complete(
        self, system: str, messages: tuple[LLMMessage, ...], tools: tuple[str, ...]
    ) -> Any: ...


class NewsTitleTranslator:
    """批量翻译新闻标题与正文摘要。失败降级为保留原文，从不抛错。"""

    def __init__(
        self,
        llm: _Completer,
        *,
        batch_size: int = _DEFAULT_BATCH_SIZE,
        item_batch_size: int = _ITEM_BATCH_SIZE,
    ) -> None:
        self._llm = llm
        self._batch_size = max(1, batch_size)
        self._item_batch_size = max(1, item_batch_size)

    async def translate(self, titles: Sequence[str]) -> dict[str, str]:
        """返回 {英文标题: 中文标题}。翻不出来的标题不出现在结果里。"""
        unique = [title for title in dict.fromkeys(titles) if title]
        if not unique:
            return {}
        translated: dict[str, str] = {}
        for start in range(0, len(unique), self._batch_size):
            batch = unique[start : start + self._batch_size]
            translated.update(await self._translate_batch(batch))
        return translated

    async def _translate_batch(self, batch: list[str]) -> dict[str, str]:
        """一批一次纯标题调用。"""
        prompt = "\n".join(f"{index}. {title}" for index, title in enumerate(batch, 1))
        try:
            result = await self._llm.complete(
                _SYSTEM_PROMPT, (LLMMessage(role="user", content=prompt),), ()
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("新闻标题翻译失败，保留英文原标题 条数=%d", len(batch), exc_info=True)
            return {}
        return _pair_with_titles(_parse_indexed_json(getattr(result, "text", "")), batch)

    async def translate_items(
        self, items: Sequence[Any]
    ) -> dict[str, tuple[str, str]]:
        """批量翻译新闻条目。返回 {news_id: (zh_title, zh_summary)}。

        - 正文已有中文的条目：只走快速纯标题翻译通道，省 token 且单批容量大；
        - 正文为英文的条目：走标题+正文精炼摘要联合翻译通道。
        """
        if not items:
            return {}

        results: dict[str, tuple[str, str]] = {}
        title_only_items: list[Any] = []
        full_trans_items: list[Any] = []

        for item in items:
            has_body_cjk = _has_cjk(getattr(item, "content", ""))
            has_title_cjk = _has_cjk(getattr(item, "title", ""))
            if has_body_cjk and not has_title_cjk:
                title_only_items.append(item)
            elif not has_body_cjk:
                full_trans_items.append(item)

        # 1. 纯标题翻译路径（正文已有中文，仅缺标题）
        if title_only_items:
            titles = [getattr(it, "title", "") for it in title_only_items]
            zh_titles = await self.translate(titles)
            for it in title_only_items:
                nid = getattr(it, "news_id", "")
                t = getattr(it, "title", "")
                if t in zh_titles:
                    results[nid] = (zh_titles[t], "")

        # 2. 联合翻译路径（正文缺中文，需要提炼摘要）
        if full_trans_items:
            for start in range(0, len(full_trans_items), self._item_batch_size):
                batch = full_trans_items[start : start + self._item_batch_size]
                batch_res = await self._translate_items_batch(batch)
                results.update(batch_res)

        return results

    async def _translate_items_batch(
        self, batch: list[Any]
    ) -> dict[str, tuple[str, str]]:
        prompt_parts: list[str] = []
        for index, item in enumerate(batch, 1):
            title = getattr(item, "title", "").strip()
            content = getattr(item, "content", "").strip()[:_CONTENT_EXCERPT_MAX_CHARS]
            prompt_parts.append(f"{index}. Title: {title}\nContent: {content}\n")
        prompt = "\n".join(prompt_parts)

        try:
            result = await self._llm.complete(
                _ITEM_SYSTEM_PROMPT, (LLMMessage(role="user", content=prompt),), ()
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("新闻条目联合翻译失败，保留原文 条数=%d", len(batch), exc_info=True)
            return {}

        return _pair_with_items(_parse_indexed_json(getattr(result, "text", "")), batch)


def _parse_indexed_json(text: str) -> dict[str, Any]:
    """从模型回复里抽出编号 → 译文的对象。拿不到就返回空 dict。"""
    match = _JSON_OBJECT_RE.search(text or "")
    if match is None:
        logger.warning("翻译回复里没有 JSON 对象，保留原文")
        return {}
    try:
        parsed = json.loads(match.group())
    except json.JSONDecodeError:
        logger.warning("翻译回复不是合法 JSON，保留原文")
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _pair_with_titles(parsed: dict[str, Any], batch: list[str]) -> dict[str, str]:
    """把编号键映射回标题，并丢掉不可用的译文。"""
    paired: dict[str, str] = {}
    for key, value in parsed.items():
        index_match = _INDEX_RE.search(str(key))
        if index_match is None or not isinstance(value, str):
            continue
        index = int(index_match.group())
        if not 1 <= index <= len(batch):
            continue
        candidate = value.strip()
        source = batch[index - 1]
        if not candidate or candidate == source or not _has_cjk(candidate):
            continue
        paired[source] = candidate
    if len(paired) < len(batch):
        logger.info("标题翻译部分未采纳 请求=%d 采纳=%d", len(batch), len(paired))
    return paired


def _pair_with_items(
    parsed: dict[str, Any], batch: list[Any]
) -> dict[str, tuple[str, str]]:
    """把编号键映射回条目，解析中文标题与中文摘要。"""
    paired: dict[str, tuple[str, str]] = {}
    for key, value in parsed.items():
        index_match = _INDEX_RE.search(str(key))
        if index_match is None:
            continue
        index = int(index_match.group())
        if not 1 <= index <= len(batch):
            continue
        target_item = batch[index - 1]

        zh_title = ""
        zh_summary = ""
        if isinstance(value, dict):
            t = str(value.get("title") or "").strip()
            s = str(value.get("summary") or value.get("content") or "").strip()
            if _has_cjk(t):
                zh_title = t
            if _has_cjk(s):
                zh_summary = s
        elif isinstance(value, str):
            candidate = value.strip()
            if _has_cjk(candidate):
                zh_summary = candidate

        # 如果原本标题就有中文，模型未给标题时沿用原中文标题
        orig_title = getattr(target_item, "title", "")
        if not zh_title and _has_cjk(orig_title):
            zh_title = orig_title

        # 只要翻译出了中文正文摘要或中文标题，即采纳
        if zh_title or zh_summary:
            nid = getattr(target_item, "news_id", "")
            paired[nid] = (zh_title, zh_summary)

    if len(paired) < len(batch):
        logger.info("新闻条目联合翻译部分未采纳 请求=%d 采纳=%d", len(batch), len(paired))
    return paired
