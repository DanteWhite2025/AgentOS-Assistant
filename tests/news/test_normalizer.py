"""归一化测试。喂真实 fixtures，覆盖两个 route 的字段差异。"""

from __future__ import annotations

import json
from datetime import UTC
from pathlib import Path

from app.news.normalizer import (
    content_fingerprint,
    make_dedup_key,
    make_news_id,
    normalize_lang,
    normalize_title,
    rows_from,
    strip_content_html,
    strip_html,
    strip_market_quote_prefix,
    strip_summary_prefix,
    to_news_item,
)

_FIXTURES = Path(__file__).parent.parent / "fixtures"


def _payload(name: str) -> dict:
    return json.loads((_FIXTURES / name).read_text())


def test_strip_html_removes_tags() -> None:
    assert strip_html("<p><b>看涨观点</b>：流入加速。</p>") == "看涨观点：流入加速。"


def test_strip_html_unescapes_then_strips_double_encoded() -> None:
    # 先反转义再剥标签：双重编码的 <script> 不能作为文本残留
    assert "<script>" not in strip_html("&lt;script&gt;alert(1)&lt;/script&gt;ok")


def test_strip_html_collapses_whitespace() -> None:
    assert strip_html("a\n\n  b\t c") == "a b c"


def test_normalize_lang_unifies_zh_variants() -> None:
    assert normalize_lang("zh") == "zh-cn"
    assert normalize_lang("ZH-CN") == "zh-cn"
    assert normalize_lang("en") == "en"
    assert normalize_lang("") == ""


def test_normalize_title_folds_case_and_whitespace() -> None:
    assert normalize_title("  BTC   Breaks  71500 ") == "btc breaks 71500"


def test_news_id_prefers_url_and_is_stable() -> None:
    first = make_news_id(
        source_url="https://a.com/x", source_name="a", title="t", content="c1"
    )
    second = make_news_id(
        source_url="https://a.com/x", source_name="b", title="t", content="c2"
    )
    assert first == second  # 有 url 时正文变化不改身份


def test_news_id_falls_back_to_name_and_content_without_url() -> None:
    a = make_news_id(source_url="", source_name="tg", title="a", content="hello")
    b = make_news_id(source_url="", source_name="tg", title="b", content="world")
    assert a != b and len(a) == 64


def test_rows_from_flattens_both_buckets() -> None:
    rows = rows_from(_payload("followin_media.json"))
    assert len(rows) == 3  # 2 articles + 1 social


def test_search_route_row_maps_fully() -> None:
    rows = rows_from(_payload("followin_media.json"))
    item = to_news_item(rows[0], source="media")
    assert item is not None
    assert item.source_url.startswith("https://cointelegraph.com")
    assert item.source_name == "cointelegraph"
    assert item.lang == "zh-cn"          # 上游给的是 zh
    assert "<p>" not in item.content     # HTML 已剥
    assert item.published_at.tzinfo is UTC
    assert item.raw["content"].startswith("BTC")  # 原始行保留（含原始 HTML）


def test_trending_route_row_tolerates_missing_columns() -> None:
    rows = rows_from(_payload("followin_trending.json"))
    item = to_news_item(rows[0], source="trending")
    assert item is not None
    assert item.source_url == "" and item.source_name == "" and item.lang == ""
    assert "recommend" in item.tags and "crypto" in item.tags


def test_row_without_title_is_dropped() -> None:
    rows = rows_from(_payload("followin_media.json"))
    social = [row for row in rows if not row.get("title")]
    assert social and to_news_item(social[0], source="media") is None


def test_row_without_published_ts_is_dropped() -> None:
    assert to_news_item({"title": "t", "content": "c"}, source="media") is None


def test_dedup_key_falls_back_to_normalized_title_when_content_empty() -> None:
    """正文为空时（social bucket 常见）才回落到规范化标题。"""
    item = to_news_item(
        {"title": "  BTC  Breaks 71500 ", "content": "", "published_ts": 1787190519312},
        source="media",
    )
    assert item is not None and item.dedup_key == "btc breaks 71500"


# 修复 1：make_news_id 无 url 时纳入 title，防止碰撞
def test_news_id_includes_title_when_no_url_different_titles() -> None:
    """无 url、source_name 与 content 都为空、但 title 不同的两行 → ID 必须不同"""
    a = make_news_id(source_url="", source_name="", title="BTC rises", content="")
    b = make_news_id(source_url="", source_name="", title="ETH falls", content="")
    assert a != b


def test_news_id_includes_title_when_no_url_same_titles() -> None:
    """无 url、source_name 与 content 都为空、title 相同 → ID 相同"""
    a = make_news_id(source_url="", source_name="", title="BTC rises", content="")
    b = make_news_id(source_url="", source_name="", title="BTC rises", content="")
    assert a == b


def test_news_id_with_url_ignores_title_changes() -> None:
    """有 source_url 时，title 变化不改 ID（确认原有语义不破坏）"""
    first = make_news_id(
        source_url="https://a.com/x", source_name="a", title="old title", content="c1"
    )
    second = make_news_id(
        source_url="https://a.com/x", source_name="a", title="new title", content="c1"
    )
    assert first == second


# 修复 2：published_ts 校验排除 bool 与非正数
def test_row_with_published_ts_true_is_dropped() -> None:
    """published_ts 为 True（bool）→ 返回 None"""
    assert to_news_item({"title": "t", "content": "c", "published_ts": True}, source="media") is None


def test_row_with_published_ts_false_is_dropped() -> None:
    """published_ts 为 False（bool）→ 返回 None"""
    assert (
        to_news_item({"title": "t", "content": "c", "published_ts": False}, source="media")
        is None
    )


def test_row_with_published_ts_zero_is_dropped() -> None:
    """published_ts 为 0 → 返回 None"""
    assert to_news_item({"title": "t", "content": "c", "published_ts": 0}, source="media") is None


def test_row_with_negative_published_ts_is_dropped() -> None:
    """published_ts 为负数 → 返回 None"""
    assert to_news_item({"title": "t", "content": "c", "published_ts": -1}, source="media") is None


def test_row_with_valid_positive_published_ts_succeeds() -> None:
    """published_ts 为正常正数 → 正常产出（确认正常路径无破坏）"""
    item = to_news_item(
        {"title": "t", "content": "c", "published_ts": 1787190519312}, source="media"
    )
    assert item is not None


# 修复 3：crypto / stocks 需要吃关键词兜底 + asset_types / upstream_category 信号，
# 不能只靠 asset_type（search route 不返回它）。
def test_media_bitcoin_row_gets_crypto_tag_without_asset_type() -> None:
    """真实 fixture：media/search route 的 Bitcoin 新闻没有 asset_type 字段，
    此前 tags 恒为 ()，现在必须靠关键词兜底拿到 crypto。"""
    rows = rows_from(_payload("followin_media.json"))
    bitcoin_row = next(row for row in rows if "Bitcoin" in (row.get("title") or ""))
    assert bitcoin_row.get("asset_type", "") == ""
    item = to_news_item(bitcoin_row, source="media")
    assert item is not None
    assert "crypto" in item.tags


def test_stocks_row_without_asset_type_gets_stocks_tag_via_keyword() -> None:
    """构造一条美股新闻（无 asset_type，模拟 search route 现状），
    断言关键词兜底能打上 stocks。"""
    row = {
        "title": "苹果公司季度财报超预期，美股盘后上涨",
        "content": "苹果发布最新季度财报，营收超出市场预期，带动美股盘后上涨。",
        "published_ts": 1787190519312,
        "provenance": "feeds",
    }
    item = to_news_item(row, source="media")
    assert item is not None
    assert item.asset_type == ""
    assert "stocks" in item.tags


def test_trending_asset_types_field_feeds_into_crypto_tag() -> None:
    """真实构造：asset_type 缺失但 asset_types 非空（模拟上游某天漏填
    asset_type 但仍带 asset_types 的情形）——必须靠 asset_types 兜底拿到 crypto。"""
    row = {
        "title": "Random token surges",
        "content": "无关键词内容",
        "published_ts": 1787190519312,
        "provenance": "followin_trending",
        "asset_types": ["牛来", "BSC", "MEME"],
    }
    item = to_news_item(row, source="trending")
    assert item is not None
    assert "crypto" in item.tags


def test_upstream_category_majorcoin_feeds_into_crypto_tag() -> None:
    """asset_type 与 asset_types 都缺失，只有 upstream_category=majorcoin。"""
    row = {
        "title": "Random token surges",
        "content": "无关键词内容",
        "published_ts": 1787190519312,
        "provenance": "followin_trending",
        "category": "majorcoin",
    }
    item = to_news_item(row, source="trending")
    assert item is not None
    assert "crypto" in item.tags


# 修复 5：published_ts 只有下界校验，畸形上界值会让 datetime.fromtimestamp
# 抛 OverflowError / OSError / ValueError，异常从行级冒到 route 级会拖垮整批
# （docstring 承诺「脏行返回 None，不拖垮整批」，实现没做到）。
def test_row_with_published_ts_overflow_error_is_dropped() -> None:
    """ts=1e30：datetime.fromtimestamp 抛 OverflowError"""
    item = to_news_item(
        {"title": "t", "content": "c", "published_ts": 1e30}, source="media"
    )
    assert item is None


def test_row_with_published_ts_os_error_is_dropped() -> None:
    """ts=10**20：datetime.fromtimestamp 抛 OSError"""
    item = to_news_item(
        {"title": "t", "content": "c", "published_ts": 10**20}, source="media"
    )
    assert item is None


def test_row_with_published_ts_value_error_is_dropped() -> None:
    """ts=253402300800000000：year 8031969 超出 datetime 支持范围，抛 ValueError"""
    item = to_news_item(
        {"title": "t", "content": "c", "published_ts": 253402300800000000},
        source="media",
    )
    assert item is None


def test_one_malformed_row_does_not_drop_the_rest_of_the_batch() -> None:
    """核心价值：同一批里 1 条畸形 + 若干正常，正常的仍全部入库（route 级不受影响）。"""
    rows = [
        {"title": "正常新闻1", "content": "c1", "published_ts": 1787190519312},
        {"title": "畸形新闻", "content": "c2", "published_ts": 1e30},
        {"title": "正常新闻2", "content": "c3", "published_ts": 1787190526095},
    ]
    items = [to_news_item(row, source="media") for row in rows]
    surviving = [item for item in items if item is not None]
    assert len(surviving) == 2
    assert {item.title for item in surviving} == {"正常新闻1", "正常新闻2"}


# 修复 6：上游内容信任边界只做了一半——title/content 过了 strip_html，
# source_name/source_url 没人处理。source_url 客户端几乎必然当 href 用，
# 不做 scheme 校验就是存储型 XSS 通道（javascript: / data: / vbscript:）。
def test_source_url_javascript_scheme_becomes_empty() -> None:
    row = {
        "title": "t", "content": "c", "published_ts": 1787190519312,
        "source_url": "javascript:alert(document.cookie)",
    }
    item = to_news_item(row, source="media")
    assert item is not None and item.source_url == ""


def test_source_url_data_scheme_becomes_empty() -> None:
    row = {
        "title": "t", "content": "c", "published_ts": 1787190519312,
        "source_url": "data:text/html,<script>alert(1)</script>",
    }
    item = to_news_item(row, source="media")
    assert item is not None and item.source_url == ""


def test_source_url_vbscript_scheme_becomes_empty() -> None:
    row = {
        "title": "t", "content": "c", "published_ts": 1787190519312,
        "source_url": "vbscript:msgbox(1)",
    }
    item = to_news_item(row, source="media")
    assert item is not None and item.source_url == ""


def test_source_url_relative_path_becomes_empty() -> None:
    row = {
        "title": "t", "content": "c", "published_ts": 1787190519312,
        "source_url": "/relative/path",
    }
    item = to_news_item(row, source="media")
    assert item is not None and item.source_url == ""


def test_source_url_empty_stays_empty() -> None:
    row = {"title": "t", "content": "c", "published_ts": 1787190519312, "source_url": ""}
    item = to_news_item(row, source="media")
    assert item is not None and item.source_url == ""


def test_source_url_https_is_preserved() -> None:
    row = {
        "title": "t", "content": "c", "published_ts": 1787190519312,
        "source_url": "https://cointelegraph.com/x",
    }
    item = to_news_item(row, source="media")
    assert item is not None and item.source_url == "https://cointelegraph.com/x"


def test_source_url_http_is_preserved() -> None:
    row = {
        "title": "t", "content": "c", "published_ts": 1787190519312,
        "source_url": "http://example.com/x",
    }
    item = to_news_item(row, source="media")
    assert item is not None and item.source_url == "http://example.com/x"


def test_malformed_source_url_does_not_drop_the_row() -> None:
    """脏 url 不该让整行丢弃——其它内容仍有价值。"""
    row = {
        "title": "t", "content": "c", "published_ts": 1787190519312,
        "source_url": "javascript:alert(1)",
    }
    item = to_news_item(row, source="media")
    assert item is not None
    assert item.title == "t"


def test_source_name_html_is_stripped() -> None:
    row = {
        "title": "t", "content": "c", "published_ts": 1787190519312,
        "source_name": "<img src=x onerror=alert(1)>cointelegraph",
    }
    item = to_news_item(row, source="media")
    assert item is not None
    assert "<img" not in item.source_name
    assert "cointelegraph" in item.source_name


# ---------------------------------------------------------------------------
# 摘要前缀清洗（Followin trending 自产摘要固定以「发生了什么」开场）
# ---------------------------------------------------------------------------


def test_strip_summary_prefix_removes_question_mark_variant() -> None:
    # 真实形态：strip_html 折叠换行后，前缀与正文之间是一个空格
    assert (
        strip_summary_prefix("发生了什么？ UBIK为Robinhood链AI概念代币。")
        == "UBIK为Robinhood链AI概念代币。"
    )


def test_strip_summary_prefix_removes_colon_and_bare_variants() -> None:
    assert strip_summary_prefix("发生了什么：X 上涨") == "X 上涨"
    assert strip_summary_prefix("发生了什么? X 上涨") == "X 上涨"
    assert strip_summary_prefix("发生了什么X 上涨") == "X 上涨"
    assert strip_summary_prefix("发生了什么\nX 上涨") == "X 上涨"


def test_strip_summary_prefix_keeps_occurrences_after_the_start() -> None:
    """只剥开场句。正文中间的同样字样是真内容，删了会改变语义。"""
    text = "美股大跌。发生了什么？流动性收紧。"
    assert strip_summary_prefix(text) == text


def test_strip_summary_prefix_on_prefix_only_content_yields_empty() -> None:
    assert strip_summary_prefix("发生了什么？") == ""


def test_strip_summary_prefix_leaves_normal_content_untouched() -> None:
    assert strip_summary_prefix("BTC 突破 71500。") == "BTC 突破 71500。"
    assert strip_summary_prefix("") == ""


def test_to_news_item_strips_summary_prefix_from_content() -> None:
    """端到端：入库正文不该再带这句开场。"""
    item = to_news_item(
        {
            "title": "UBIK price up",
            "content": "发生了什么？\nUBIK为Robinhood链AI概念代币。",
            "published_ts": 1787190519312,
        },
        source="trending",
    )
    assert item is not None
    assert item.content == "UBIK为Robinhood链AI概念代币。"
    # 原始行不动：raw 是排障依据，必须保留上游原样
    assert item.raw["content"].startswith("发生了什么")


# ---------------------------------------------------------------------------
# dedup_key：正文指纹（上游对同一事件会生成多个英文标题变体）
# ---------------------------------------------------------------------------


def test_dedup_key_is_identical_for_same_content_under_different_titles() -> None:
    """真实场景：staging 上 UBIK 同一条摘要配了两个不同英文标题，
    旧的标题式 dedup_key 认不出来，两条都入库。"""
    content = "UBIK为Robinhood链AI概念代币。近期因AI叙事受到关注。"
    first = make_dedup_key(title="UBIK's price increased fivefold.", content=content)
    second = make_dedup_key(
        title="UBIK is the AI concept token for the Robinhood blockchain.", content=content
    )
    assert first == second


def test_dedup_key_differs_for_different_content() -> None:
    same_title = "Market update"
    assert make_dedup_key(title=same_title, content="BTC 涨了") != make_dedup_key(
        title=same_title, content="ETH 跌了"
    )


def test_dedup_key_ignores_whitespace_and_case_differences() -> None:
    assert make_dedup_key(title="t", content="BTC  突破\n71500") == make_dedup_key(
        title="t", content="btc 突破 71500"
    )


def test_dedup_key_ignores_trailing_additions_beyond_the_core() -> None:
    """指纹只吃正文开头的核心摘要：上游会在尾部追加实时行情
    （「最新行情（截至 09-08 11:57）」），追加不该让同一条新闻反复入库。"""
    core = "SK Hynix Inc.是全球领先的存储芯片制造商。" * 10  # 去空白后超过指纹取样长度
    assert make_dedup_key(title="t", content=core) == make_dedup_key(
        title="t", content=core + "最新行情（截至 09-08 11:57）：涨5.8%。"
    )


def test_content_fingerprint_is_empty_for_blank_content() -> None:
    assert content_fingerprint("") == ""
    assert content_fingerprint("   \n ") == ""


def test_dedup_key_falls_back_to_title_for_blank_content() -> None:
    assert make_dedup_key(title="  BTC  Up ", content="") == "btc up"


def test_to_news_item_uses_content_fingerprint_as_dedup_key() -> None:
    row = {
        "title": "Some english title",
        "content": "中文摘要正文",
        "published_ts": 1787190519312,
    }
    item = to_news_item(row, source="trending")
    assert item is not None
    assert item.dedup_key == make_dedup_key(title=row["title"], content="中文摘要正文")


# ---------------------------------------------------------------------------
# strip_market_quote_prefix：正文动态行情前缀清洗
# ---------------------------------------------------------------------------


def test_strip_market_quote_prefix_removes_leading_quote_and_exposes_core_body() -> None:
    text = (
        "最新行情（截至 09-09 03:59）：激光产品需求持续强劲，Lumentum Holdings Inc.涨11%。"
        "Lumentum Holdings Inc.是一家光通信与激光器制造商。"
    )
    assert strip_market_quote_prefix(text) == "Lumentum Holdings Inc.是一家光通信与激光器制造商。"


def test_strip_market_quote_prefix_handles_float_percentage_without_premature_truncation() -> None:
    text = (
        "最新行情（截至 09-08 19:32）：台积电与三星将采用ASML新一代光刻机，INTC盘前涨4.3%、ASML涨3.2%。"
        "台积电与三星宣布将采用ASML最新High NA EUV光刻机。"
    )
    assert strip_market_quote_prefix(text) == "台积电与三星宣布将采用ASML最新High NA EUV光刻机。"


def test_strip_market_quote_prefix_handles_abbreviations_like_inc() -> None:
    text = (
        "最新行情（截至 09-05 04:00）：推出无方向盘自动驾驶Cybercab，Tesla Inc.跌6%。"
        "Tesla（TSLA）在奥斯汀正式推出无方向盘的自动驾驶Cybercab。"
    )
    assert strip_market_quote_prefix(text) == "Tesla（TSLA）在奥斯汀正式推出无方向盘的自动驾驶Cybercab。"


def test_strip_market_quote_prefix_falls_back_to_header_only_when_no_trailing_body() -> None:
    text = "最新行情（截至 09-08 11:57）：涨5.8%。"
    assert strip_market_quote_prefix(text) == "涨5.8%。"


def test_strip_market_quote_prefix_leaves_regular_content_intact() -> None:
    text = "美联储主席鲍威尔在杰克逊霍尔会议上发表讲话。"
    assert strip_market_quote_prefix(text) == text


def test_strip_market_quote_prefix_skips_interior_newline_before_true_sentence_end() -> None:
    """行情句内部若被 <br> 转换出的 \\n 断开，也不能提前截断——真正的句末终止符
    （句号）必须优先于中途出现的换行，否则波动的涨跌幅会漏进 content，
    dedup_key 就不稳了（PR 评审发现的真实回归）。"""
    text = (
        "最新行情（截至 09-09 03:14）：激光器需求持续强劲，\n"
        "Lumentum Holdings Inc.涨12%。Lumentum Holdings Inc.是一家光通信与激光器制造商。"
    )
    assert strip_market_quote_prefix(text) == "Lumentum Holdings Inc.是一家光通信与激光器制造商。"


def test_strip_market_quote_prefix_english_period_lookahead_excludes_newline() -> None:
    """英文缩写句点（如 Inc.）后面如果紧跟一个 <br> 转换出的换行，\\s 的 lookahead
    会把 \\n 当成"句点后接空白"而误判成句末——必须只认真正的水平空白/字符串
    结尾，换行不算数，否则退化到跟 \\n 直接参与终止符候选一样的老问题。"""
    text = (
        "最新行情（截至 09-09 03:14）：激光器需求持续强劲，Lumentum Holdings Inc.\n"
        "涨12%。Lumentum Holdings Inc.是一家光通信与激光器制造商。"
    )
    assert strip_market_quote_prefix(text) == "Lumentum Holdings Inc.是一家光通信与激光器制造商。"


def test_market_quote_with_br_after_abbreviation_period_shares_identical_dedup_key() -> None:
    """真实回归场景：<br> 恰好紧跟在缩写句点（Inc.）后面，断点前的句点绝不能
    被误判成句末终止符，否则波动的涨跌幅又会漏进 content。"""
    row1 = {
        "title": "Laser Demand Strong; Lumentum Up 12%",
        "content": (
            "最新行情（截至 09-09 03:14）：激光器需求持续强劲，Lumentum Holdings Inc.<br>"
            "涨12%。Lumentum Holdings Inc.是一家光通信与激光器制造商。"
        ),
        "published_ts": 1787215718000,
    }
    row2 = {
        "title": "Laser Demand Strong; Lumentum Up 11%",
        "content": (
            "最新行情（截至 09-09 03:59）：激光器需求持续强劲，Lumentum Holdings Inc.<br>"
            "涨11%。Lumentum Holdings Inc.是一家光通信与激光器制造商。"
        ),
        "published_ts": 1787218418000,
    }
    item1 = to_news_item(row1, source="trending")
    item2 = to_news_item(row2, source="trending")
    assert item1 is not None and item2 is not None
    assert item1.content == "Lumentum Holdings Inc.是一家光通信与激光器制造商。"
    assert item1.dedup_key == item2.dedup_key


def test_market_quote_with_embedded_br_before_percentage_shares_identical_dedup_key() -> None:
    """真实回归场景：行情播报句被上游一个 <br> 断成两半，断点恰好在真正句号
    之前——转换出的 \\n 不该让 strip_market_quote_prefix 提前止步于波动数字。"""
    row1 = {
        "title": "Laser Demand Strong; Lumentum Up 12%",
        "content": (
            "最新行情（截至 09-09 03:14）：激光器需求持续强劲，<br>"
            "Lumentum Holdings Inc.涨12%。Lumentum Holdings Inc.是一家光通信与激光器制造商。"
        ),
        "published_ts": 1787215718000,
    }
    row2 = {
        "title": "Laser Demand Strong; Lumentum Up 11%",
        "content": (
            "最新行情（截至 09-09 03:59）：激光器需求持续强劲，<br>"
            "Lumentum Holdings Inc.涨11%。Lumentum Holdings Inc.是一家光通信与激光器制造商。"
        ),
        "published_ts": 1787218418000,
    }
    item1 = to_news_item(row1, source="trending")
    item2 = to_news_item(row2, source="trending")
    assert item1 is not None and item2 is not None
    assert item1.content == "Lumentum Holdings Inc.是一家光通信与激光器制造商。"
    assert item1.dedup_key == item2.dedup_key


def test_market_quote_updates_share_identical_dedup_key() -> None:
    """真实线上场景验证：相隔 45 分钟的两次行情波动推送，归一化后 dedup_key 完全相同。

    避免不同时间戳和涨幅（03:14 涨12% vs 03:59 涨11%）反复新建行导致列表刷屏。
    """
    row1 = {
        "title": "Laser Demand Strong; Lumentum Up 12%",
        "content": (
            "发生了什么？\n"
            "最新行情（截至 09-09 03:14）：激光器产品需求持续强劲，Lumentum Holdings Inc.涨12%。"
            "Lumentum Holdings Inc.是一家光通信与激光器制造商。"
        ),
        "published_ts": 1787215718000,
    }
    row2 = {
        "title": "Laser Demand Strong; Lumentum Up 11%",
        "content": (
            "发生了什么？\n"
            "最新行情（截至 09-09 03:59）：激光器产品需求持续强劲，Lumentum Holdings Inc.涨11%。"
            "Lumentum Holdings Inc.是一家光通信与激光器制造商。"
        ),
        "published_ts": 1787218418000,
    }
    item1 = to_news_item(row1, source="trending")
    item2 = to_news_item(row2, source="trending")
    assert item1 is not None and item2 is not None
    assert item1.dedup_key == item2.dedup_key


# ---------------------------------------------------------------------------
# strip_content_html：正文专用清洗，保留段落换行（移动端按 \n 分块渲染）
# ---------------------------------------------------------------------------


def test_strip_content_html_converts_br_to_newline() -> None:
    assert strip_content_html("第一行<br>第二行<br/>第三行<br />第四行") == (
        "第一行\n第二行\n第三行\n第四行"
    )


def test_strip_content_html_converts_p_tags_to_paragraph_breaks() -> None:
    assert strip_content_html("<p>第一段。</p><p>第二段。</p>") == "第一段。\n\n第二段。"


def test_strip_content_html_preserves_existing_raw_newlines() -> None:
    assert strip_content_html("第一段。\n第二段。") == "第一段。\n第二段。"


def test_strip_content_html_collapses_only_horizontal_whitespace_within_a_line() -> None:
    assert strip_content_html("a\t\t b   c\nd") == "a b c\nd"


def test_strip_content_html_normalizes_excess_blank_lines_to_at_most_one() -> None:
    assert strip_content_html("第一段\n\n\n\n第二段") == "第一段\n\n第二段"


def test_strip_content_html_trims_each_line_and_overall_content() -> None:
    assert strip_content_html("  \n  第一行  \n  第二行  \n  ") == "第一行\n第二行"


def test_strip_content_html_does_not_split_single_paragraph_flash_news() -> None:
    """短快讯本身就是单段——不该被人为切成一句一行。"""
    text = "苹果公司季度财报超预期，营收超出市场预期4.3%，带动美股盘后上涨。"
    assert strip_content_html(text) == text


def test_strip_content_html_does_not_break_on_decimal_percentage_or_abbreviation() -> None:
    """绝不能在句号后粗暴加 \\n：会误伤 4.3% 这种浮点数和 Inc. 这种缩写。"""
    text = "INTC盘前涨4.3%，Tesla Inc.同时下跌，Lumentum Holdings Inc.持平。"
    result = strip_content_html(text)
    assert "\n" not in result
    assert result == text


def test_strip_content_html_unescapes_entities_like_strip_html() -> None:
    assert "<script>" not in strip_content_html("&lt;script&gt;alert(1)&lt;/script&gt;ok")


def test_strip_content_html_empty_input_yields_empty_string() -> None:
    assert strip_content_html("") == ""
    assert strip_content_html(None) == ""  # type: ignore[arg-type]


def test_strip_content_html_tolerates_attributes_on_block_tags() -> None:
    assert (
        strip_content_html('<p class="x">第一段。</p><p id="y">第二段。</p>')
        == "第一段。\n\n第二段。"
    )


def test_strip_content_html_tolerates_attributes_on_br() -> None:
    assert strip_content_html('第一行<br clear="all">第二行') == "第一行\n第二行"


def test_strip_content_html_is_case_insensitive_for_block_tags() -> None:
    assert strip_content_html("<P>第一段</P><BR/>第二段") == "第一段\n\n第二段"


def test_strip_content_html_does_not_treat_custom_tag_as_block_tag() -> None:
    """<table-wrapper> 不是 <table>——标签名后紧跟连字符不该被当块级标签误判。"""
    assert strip_content_html("<table-wrapper>内容</table-wrapper>") == "内容"


def test_to_news_item_content_preserves_paragraph_breaks() -> None:
    row = {
        "title": "t",
        "content": "<p>第一段说明。</p><p>第二段说明。</p>",
        "published_ts": 1787190519312,
    }
    item = to_news_item(row, source="media")
    assert item is not None
    assert item.content == "第一段说明。\n\n第二段说明。"


def test_to_news_item_title_and_source_name_stay_single_line_despite_multiline_source() -> None:
    row = {
        "title": "<p>标题</p>",
        "content": "<p>正文第一段。</p><p>正文第二段。</p>",
        "source_name": "<p>来源名</p>",
        "published_ts": 1787190519312,
    }
    item = to_news_item(row, source="media")
    assert item is not None
    assert "\n" not in item.title
    assert "\n" not in item.source_name


def test_to_news_item_dedup_key_unaffected_by_paragraph_break_style() -> None:
    """指纹算法本就对全部空白无差别剥离，段落换行不该动摇去重逻辑。"""
    with_tags = to_news_item(
        {
            "title": "t1",
            "content": "<p>核心内容一致。</p><p>只是段落标签不同。</p>",
            "published_ts": 1787190519312,
        },
        source="media",
    )
    without_tags = to_news_item(
        {
            "title": "t2",
            "content": "核心内容一致。只是段落标签不同。",
            "published_ts": 1787190519312,
        },
        source="media",
    )
    assert with_tags is not None and without_tags is not None
    assert with_tags.dedup_key == without_tags.dedup_key


def test_news_id_identity_unaffected_by_paragraph_preserving_content_cleanup() -> None:
    """身份口径（trending route 无 source_url 时）必须继续用旧的单行折叠正文，
    否则清洗规则升级会让全部存量行算出新 id，整批重新入库刷屏。"""
    row = {
        "title": "t",
        "content": "<p>第一段。</p><p>第二段。</p>",
        "published_ts": 1787190519312,
        "source_name": "src",
    }
    item = to_news_item(row, source="trending")
    assert item is not None
    expected_id = make_news_id(
        source_url="",
        source_name="src",
        title="t",
        content=strip_html(row["content"]),
    )
    assert item.news_id == expected_id

