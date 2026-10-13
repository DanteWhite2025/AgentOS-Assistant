"""上游原始行 → NewsItem。

Followin 的字段是 route-dependent 的：带 query/sources 的 search route 有
source_url / source_lang / source_name，空 query 的 trending route 没有，
但独有 asset_types。所以这里对每一列都按缺失容忍处理，原始行整份留在 raw 里。

title 与 published_ts 是硬要求：没标题渲染不出新闻卡片（social bucket 的
title 实测 100% 为空），没时间戳排不了序也分不了页 —— 这两种行直接丢弃。
"""

from __future__ import annotations

import hashlib
import html
import logging
import re
from datetime import UTC, datetime
from typing import Any, Final
from urllib.parse import urlparse

from app.news.models import NewsItem
from app.news.tagger import tags_for

logger = logging.getLogger(__name__)

_TAG_RE: Final = re.compile(r"<[^>]+>")
_WS_RE: Final = re.compile(r"\s+")


def strip_html(raw: str) -> str:
    """剥标签并折叠空白（单行字段专用：title / source_name）。

    先反转义再剥标签：上游正文里混着 <p><b> 这类真标签，也可能出现
    &lt;script&gt; 这类转义标签；先 unescape 能让两者都被同一条规则清掉，
    避免转义过的标记以文本形式残留下来。
    """
    return _WS_RE.sub(" ", _TAG_RE.sub("", html.unescape(raw or ""))).strip()


# 块级/换行标签 → 段落换行。只转换会带来实际排版断行的标签（含 <br> 与常见
# 块级容器），其余行内标签（<b>、<a> 等）仍交给 _TAG_RE 无痕剥除。
#
# 标签名后用 (?![\w-]) 而非单纯的 \s*>：既要容忍 <p class="x">、<br clear="all">
# 这类带属性的真实变体（否则这类开标签落到 _TAG_RE 里被无痕删掉、前后文本会不
# 该地黏在一起——若同一段落里紧接着没有配对的属性化闭合标签兜底换行，就会丢段
# 落间隔），又要避免把 <table-wrapper> 这种自定义标签误判成 <table>。
_BLOCK_BREAK_RE: Final = re.compile(
    r"<\s*/?\s*(?:br|p|div|li|tr|h[1-6]|blockquote|section|article|header|footer|table|ul|ol)"
    r"(?![\w-])[^>]*>",
    re.IGNORECASE,
)
_HORIZONTAL_WS_RE: Final = re.compile(r"[ \t]+")
_EXCESS_BLANK_LINES_RE: Final = re.compile(r"\n{3,}")


def strip_content_html(raw: str) -> str:
    """正文专用清洗：保留段落换行，供移动端按 \\n 分块渲染。

    与 strip_html 的区别：那个函数无差别折叠所有空白，正文若走它就会把
    <p>/<br> 结构与上游原有的换行全部压成一整行长文本。这里先把块级/换行
    标签转成 \\n 再剥其余标签，之后只折叠行内水平空白（[ \\t]+），换行本身
    不动；不按「句号」加行——会误伤 4.3% 这类浮点数和 Inc. 这类缩写，也会把
    上游本就是单段的短快讯拆得七零八落。
    """
    text = html.unescape(raw or "")
    text = _BLOCK_BREAK_RE.sub("\n", text)
    text = _TAG_RE.sub("", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = (_HORIZONTAL_WS_RE.sub(" ", line).strip() for line in text.split("\n"))
    text = _EXCESS_BLANK_LINES_RE.sub("\n\n", "\n".join(lines))
    return text.strip()


def normalize_lang(value: str) -> str:
    """上游 zh 与 zh-cn 混用，统一成 zh-cn。"""
    normalized = (value or "").strip().lower()
    return "zh-cn" if normalized == "zh" else normalized


_ALLOWED_URL_SCHEMES: Final = frozenset({"http", "https"})


def sanitize_source_url(value: str) -> str:
    """上游 HTML/URL 不可信（设计文档 §5/§15）：source_url 客户端几乎必然当
    href 用，不做 scheme 校验就是存储型 XSS 通道
    （如 `javascript:alert(document.cookie)`）。只放行 http/https，
    不合规一律返回空字符串——不存原值、不抛异常，脏 url 不该让整行丢弃。
    """
    candidate = (value or "").strip()
    if not candidate:
        return ""
    try:
        parsed = urlparse(candidate)
    except ValueError:
        return ""
    if parsed.scheme.lower() not in _ALLOWED_URL_SCHEMES or not parsed.netloc:
        return ""
    return candidate


def normalize_title(title: str) -> str:
    """标题规范化：去首尾空白、折叠内部空白、大小写无关。"""
    return _WS_RE.sub(" ", title or "").strip().casefold()


# Followin trending 自产的中文摘要固定以「发生了什么」开场（staging 实测
# 推荐流 47/50 条命中，其他源 1/50）。这句对每张卡片都一样，占掉列表页最
# 显眼的第一行却不携带任何信息，产品明确要求去掉。
#
# 只在开头剥（`^` 锚定）：正文中间出现的同样字样是真内容
# （「美股大跌。发生了什么？流动性收紧。」），删掉会改变语义。
_SUMMARY_PREFIX_RE: Final = re.compile(r"^\s*发生了什么\s*[？?：:！!。.、,，\-—]*\s*")


def _strip_leading_match(pattern: re.Pattern[str], content: str) -> str:
    """按前缀正则剥字符——但边界定位用「\\n 等长替换成空格」的探测视图。

    这两条前缀正则（摘要开场句 / 行情前缀）是针对单行折叠正文设计和验证的；
    正文保留段落换行后，若直接拿保留换行的原文去匹配，字符位置不会变——
    \\n → 空格是等长替换——但正则里的 \\s* 反而会稳稳吃掉这段真实换行，
    行为与折叠版一致。用探测视图只是为了不让后续改动无意间依赖 \\s 之外
    的字符类；真正剥掉的字符仍按匹配长度从原文（保留换行）里切。
    """
    if not content:
        return ""
    match = pattern.match(content.replace("\n", " "))
    return content[match.end() :] if match else content


def strip_summary_prefix(content: str) -> str:
    """剥掉上游摘要的固定开场句。没有这句则原样返回。"""
    return _strip_leading_match(_SUMMARY_PREFIX_RE, content or "").strip()


# 行情快讯动态前缀（如「最新行情（截至 09-09 03:59）：激光产品需求持续强劲，Lumentum Holdings Inc.涨11%。Lumentum...」）。
#
# 该行情前缀包含每隔数十分钟变动一次的时间戳与涨跌幅（03:14 vs 03:59、涨12% vs 涨11%），
# 且位于正文最开头。若不清洗，它会直接击穿正文核心指纹（前 160 字），导致同一事件或主体的
# 快讯每轮采集都算出不同的 dedup_key、反复作为新行入库造成列表刷屏。
#
# 句子终止符必须避开浮点数（如 4.3% 中的小数点）和公司英文缩写（如 Tesla Inc. 中的小数点）：
# 只认中文全角句号 `。`，或后接水平空白（空格/制表符）/文本末尾的英文句号
# `\.(?=[ \t]|$|\Z)`。
#
# 换行 `\n` 特意排除在外——既不放进终止符本身，也不放进英文句号的 lookahead
# （`[ \t]` 而非 `\s`）：正文保留段落换行后，行情播报句内部可能被上游一个
# <br> 断开，断点既可能落在句子中途，也可能恰好紧跟在缩写句点（Inc./Corp.
# 这类）后面——若 \n 能满足 `\s` 的 lookahead，缩写句点会被误判成句末，
# 懒惰匹配提前止步，把波动的涨跌幅漏进结果里，让同一事件反复算出不同
# dedup_key（PR 评审发现的真实回归，且发现了两次不同的触发路径）。\n 只在
# 整句找不到任何句末标点时才作为退化边界使用，见 strip_market_quote_prefix。
_MARKET_QUOTE_HEADER_RE: Final = re.compile(
    r"^\s*最新行情\s*[（(]\s*截至[^）)]+[）)]\s*[：:]\s*",
)
_SENTENCE_TERMINATOR_RE: Final = re.compile(r"。|\.(?=[ \t]|$|\Z)")


def strip_market_quote_prefix(content: str) -> str:
    """剥掉上游正文开头的动态行情前缀。没有则原样返回。

    优先剥掉整句动态行情总结，露出真正的新闻主体背景（多轮采集下主体完全一致，稳住指纹）；
    若剥掉后正文为空（极简行情快讯无后续正文），则回退到仅剥离时间戳头，保留行情文案，
    绝不返回空内容。

    已知取舍：本函数假定行情总结句本身以「。」收尾（上游实测样本全部如此，
    见上方注释里的真实例子）。若上游哪天出现行情句本身不带「。」、又被
    <br> 断开的畸形行，_SENTENCE_TERMINATOR_RE 会一路找到真正正文里的第一个
    句号才停手，可能连带吞掉正文的第一句。之所以接受这个代价：
    (1) 它不影响 dedup_key 稳定性——两次采集会一致地吞掉同一句，不会像
    「\\n 提前截断」那样让波动的涨跌幅漏出来；(2) 反过来放宽会让 \\n 重新
    参与终止符判定，正是这段逻辑要修的那个真实 bug（PR 评审两次抓到）。
    """
    text = content or ""
    header_match = _MARKET_QUOTE_HEADER_RE.match(text.replace("\n", " "))
    if not header_match:
        return text.strip()
    body = text[header_match.end() :]

    terminator = _SENTENCE_TERMINATOR_RE.search(body)
    if terminator:
        quote_end = terminator.end()
    else:
        newline_pos = body.find("\n")
        quote_end = newline_pos + 1 if newline_pos != -1 else len(body)

    stripped = body[quote_end:].strip()
    return stripped or body.strip()


# 指纹取样长度。上游会在正文尾部追加实时行情（「最新行情（截至 09-08 11:57）：
# 涨5.8%」），全文 hash 会让同一条新闻每次刷新都算成新的一条；只吃开头的核心
# 摘要能稳住身份。取 160 字：短于此的摘要整条参与，够长到不会把两条不同的
# 新闻撞在一起。
_FINGERPRINT_CHARS: Final = 160


def content_fingerprint(content: str) -> str:
    """正文核心指纹。正文为空返回空串，由调用方回落到标题。

    规范化时**删掉**所有空白而不是折叠：上游同一段摘要在不同 route 里的
    换行/空格位置会变（trending 用 \\n 分段，search route 已经是一行），
    折叠成单空格仍会算出两个不同的指纹。
    """
    core = _WS_RE.sub("", content or "").casefold()[:_FINGERPRINT_CHARS]
    return hashlib.sha256(core.encode()).hexdigest()[:32] if core else ""


def make_dedup_key(*, title: str, content: str) -> str:
    """跨 route 合并键：正文指纹优先，正文为空时回落规范化标题。

    原先只用标题：上游对同一事件会生成多个英文标题变体（staging 实测
    UBIK 同一段摘要配了两个不同英文标题），标题式的键认不出来，两条都入库，
    用户在推荐流里看到重复新闻。正文指纹能合掉这类；而 social bucket 的
    正文可能为空，那时只剩标题可用。
    """
    return content_fingerprint(content) or normalize_title(title)


def make_news_id(
    *, source_url: str, source_name: str, title: str, content: str
) -> str:
    """身份优先用 source_url（实测稳定唯一）；无 url 时纳入 title 防止碰撞。"""
    seed = source_url.strip() or f"{source_name.strip()}|{title}|{content[:256]}"
    return hashlib.sha256(seed.encode()).hexdigest()


def rows_from(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """把 results 下的 articles / social 两个 bucket 摊平成一个列表。"""
    results = payload.get("results") or {}
    rows: list[dict[str, Any]] = []
    for bucket in results.values():
        if isinstance(bucket, list):
            rows.extend(row for row in bucket if isinstance(row, dict))
    return rows


def _parse_published_at(published_ts: float) -> datetime | None:
    """毫秒时间戳 → UTC datetime。畸形上界值（如 1e30）会让
    `datetime.fromtimestamp` 抛 `OverflowError` / `OSError` / `ValueError`，
    这里兜住三者，交由调用方按脏行处理，不让异常冒到 route 级拖垮整批。
    """
    try:
        return datetime.fromtimestamp(published_ts / 1000, UTC)
    except (OverflowError, OSError, ValueError):
        return None


def to_news_item(row: dict[str, Any], *, source: str) -> NewsItem | None:
    """单行归一化。脏行返回 None，由调用方跳过，不拖垮整批。"""
    title = strip_html(str(row.get("title") or ""))
    if not title:
        logger.debug("news 归一化丢弃一行：缺少标题 source=%s", source)
        return None

    published_ts = row.get("published_ts")
    if (
        isinstance(published_ts, bool)
        or not isinstance(published_ts, (int, float))
        or published_ts <= 0
    ):
        logger.debug(
            "news 归一化丢弃一行：published_ts 非法 source=%s published_ts=%r",
            source, published_ts,
        )
        return None

    published_at = _parse_published_at(published_ts)
    if published_at is None:
        logger.debug(
            "news 归一化丢弃一行：published_ts 超出可表示范围 source=%s published_ts=%r",
            source, published_ts,
        )
        return None

    # 身份用未剥前缀的正文算，展示用剥过的。两者必须分开：news_id 进了
    # 主键，若跟着清洗规则变，一次上线就会让全部无 url 的存量行（trending
    # route 全部如此）算出新 id、整批重新入库，用户看到的是满屏重复。
    #
    # raw_content 特意继续走旧的单行折叠口径（strip_html）而非保留段落的
    # strip_content_html——保留换行只是展示层的诉求，身份口径不能跟着漂移。
    content_raw = str(row.get("content") or "")
    raw_content = strip_html(content_raw)
    content = strip_market_quote_prefix(strip_summary_prefix(strip_content_html(content_raw)))
    source_url = sanitize_source_url(str(row.get("source_url") or ""))
    source_name = strip_html(str(row.get("source_name") or ""))
    asset_type = str(row.get("asset_type") or "").strip()
    provenance = str(row.get("provenance") or "").strip()
    upstream_category = str(row.get("category") or "").strip()
    raw_asset_types = row.get("asset_types")
    asset_types = (
        tuple(str(value) for value in raw_asset_types)
        if isinstance(raw_asset_types, list)
        else ()
    )

    return NewsItem(
        news_id=make_news_id(
            source_url=source_url,
            source_name=source_name,
            title=title,
            content=raw_content,
        ),
        dedup_key=make_dedup_key(title=title, content=content),
        source=source,
        title=title,
        content=content,
        published_at=published_at,
        source_name=source_name,
        source_url=source_url,
        source_quality=str(row.get("source_quality") or "").strip(),
        lang=normalize_lang(str(row.get("source_lang") or "")),
        asset_type=asset_type,
        upstream_category=upstream_category,
        tags=tags_for(
            title=title,
            content=content,
            asset_type=asset_type,
            provenance=provenance,
            asset_types=asset_types,
            upstream_category=upstream_category,
        ),
        raw=row,
    )
