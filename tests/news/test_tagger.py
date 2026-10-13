"""标签器测试。重点是 ASCII 短词的词边界——这是最容易出错的地方。"""

from __future__ import annotations

from app.news.tagger import keyword_tags, tags_for


def test_ascii_acronym_requires_word_boundary() -> None:
    # "said" / "Thailand" / "maintain" 都含子串 ai，绝不能命中 ai_tech
    for text in ("he said nothing", "Thailand exports rise", "maintain the position"):
        assert "ai_tech" not in keyword_tags(text)


def test_ascii_acronym_matches_as_standalone_word() -> None:
    assert "ai_tech" in keyword_tags("AI model training costs surge")
    assert "ai_tech" in keyword_tags("an AI-driven rally")  # 连字符是词边界


def test_mixed_case_term_is_case_sensitive() -> None:
    # "Fed" 大小写敏感：否则 "he fed the dog" 会被判成宏观新闻
    assert "macro" not in keyword_tags("he fed the dog this morning")
    assert "macro" in keyword_tags("Fed signals a rate cut")


def test_all_lowercase_term_is_case_insensitive() -> None:
    assert "macro" in keyword_tags("Tariff threat escalates")
    assert "macro" in keyword_tags("tariff threat escalates")


def test_cjk_term_matches_as_substring() -> None:
    assert "ai_tech" in keyword_tags("英伟达发布新一代算力芯片")
    assert "macro" in keyword_tags("美联储宣布降息25个基点")


def test_multiple_tags_can_match() -> None:
    tags = keyword_tags("美联储降息推动英伟达股价上涨")
    assert "macro" in tags and "ai_tech" in tags


def test_asset_type_maps_to_crypto_and_stocks() -> None:
    assert tags_for(title="BTC up", content="", asset_type="crypto", provenance="feeds") == (
        "crypto",
    )
    assert tags_for(title="AAPL up", content="", asset_type="tradfi", provenance="feeds") == (
        "stocks",
    )


def test_trending_provenance_yields_recommend() -> None:
    tags = tags_for(
        title="BTC breaks 71500", content="", asset_type="crypto",
        provenance="followin_trending",
    )
    assert tags[0] == "recommend" and "crypto" in tags


def test_tags_are_deduplicated_and_order_stable() -> None:
    tags = tags_for(
        title="美联储降息", content="美联储再次降息", asset_type="", provenance="feeds",
    )
    assert tags == ("macro",)


def test_no_signal_yields_empty() -> None:
    assert tags_for(title="hello", content="world", asset_type="", provenance="feeds") == ()


def test_yield_as_verb_not_macro() -> None:
    # "yield" 作动词时不该命中 macro：yield curve 和 bond yield 才是宏观信号
    for text in (
        "The government refused to yield to protesters' demands",
        "Drivers must yield to pedestrians at the crossing",
        "The chemical reaction had a low yield",
    ):
        assert "macro" not in keyword_tags(text)


def test_yield_curve_and_bond_yield_match() -> None:
    # 真正的宏观信号应该命中
    assert "macro" in keyword_tags("the yield curve inverted")
    assert "macro" in keyword_tags("bond yield reaches 5%")
    assert "macro" in keyword_tags("10-year Treasury yield climbs")


def test_treasury_as_noun_not_macro() -> None:
    # "Treasury" 作名词（博物馆等）时不该命中 macro
    assert "macro" not in keyword_tags("Treasury of the museum announced a new exhibit")


def test_treasury_department_and_yield_match() -> None:
    # 真正的宏观信号应该命中
    assert "macro" in keyword_tags("Treasury Department issued new guidance")
    assert "macro" in keyword_tags("Treasury yield hits new high")
    assert "macro" in keyword_tags("美国财政部宣布新政策")


def test_gemini_removed_from_ai_tech() -> None:
    # Gemini 不再命中 ai_tech（它是加密交易所，不是 AI）
    assert "ai_tech" not in keyword_tags("Gemini constellation rises in the sky tonight")
    assert "ai_tech" not in keyword_tags("Gemini exchange lists new token")


def test_claude_still_matches_ai_tech() -> None:
    # Claude 保留在规则里（可接受风险）
    assert "ai_tech" in keyword_tags("Claude model achieves new benchmark")


def test_crypto_domain_regression_no_treasury_macro() -> None:
    # 加密货币新闻里的 DAO treasury 不应该标为 macro
    assert "macro" not in keyword_tags("DAO treasury diversifies into ETH")


def test_crypto_domain_regression_no_gemini_ai_tech() -> None:
    # 加密交易所 Gemini 不应该标为 ai_tech
    assert "ai_tech" not in keyword_tags("Gemini exchange launches spot trading")


# 修复 2：ASCII 缩写紧贴汉字时也要能命中（CJK 字符属于 Python re 的 \w，
# ASCII 词若用 \b 做边界，紧贴汉字时没有词边界，导致全部漏标）
def test_ascii_acronym_adjacent_to_cjk_matches() -> None:
    cases = (
        ("CPI数据公布", "macro"),
        ("GDP增速放缓", "macro"),
        ("FOMC会议纪要显示", "macro"),
        ("NVDA财报超预期", "ai_tech"),
        ("美国AI产业政策", "ai_tech"),
        ("OpenAI发布新模型", "ai_tech"),
        ("Powell讲话偏鹰", "macro"),
        ("ChatGPT用户破亿", "ai_tech"),
        ("AMD发布新品", "ai_tech"),
    )
    for text, tag in cases:
        assert tag in keyword_tags(text), f"{text!r} 应命中 {tag}"


def test_ascii_boundary_lookaround_still_rejects_substrings() -> None:
    # 换成 lookaround 后，原有防误报断言必须继续通过
    assert "macro" not in keyword_tags("he fed the dog")
    assert "macro" not in keyword_tags("Thailand exports rise")
    assert "ai_tech" not in keyword_tags("maintain the position")
    assert "macro" not in keyword_tags("he fed the dog")
    # GDPR 不应命中 GDP；AMDX 不应命中 AMD（词尾仍贴着别的 ASCII 字符）
    assert "macro" not in keyword_tags("the new GDPR rules take effect")
    assert "ai_tech" not in keyword_tags("AMDX ticker rallies")


def test_multi_word_macro_terms_are_case_insensitive() -> None:
    # 多词条目（含大写字母）大小写不敏感，不同于单词条目的规则
    assert "macro" in keyword_tags("treasury yield falls")
    assert "macro" in keyword_tags("the YIELD CURVE inverted sharply")
    assert "macro" in keyword_tags("treasury department issued new guidance")
    assert "macro" in keyword_tags("bond YIELD reaches 5%")


# 修复 3：crypto / stocks 两个 tag 只靠上游 asset_type 在生产里几乎全空
# （search route 不返回 asset_type，占大头的 media/research 全部裸奔）。
# 补关键词规则兜底，并把 asset_types / upstream_category 纳入判定。
def test_crypto_keyword_matches_bitcoin_without_asset_type() -> None:
    # 真实 fixture 场景：search route 的 Bitcoin 新闻没有 asset_type
    assert "crypto" in keyword_tags("Bitcoin price reaches 72.5k as inflows accelerate")
    assert "crypto" in keyword_tags("BTC 隔夜大涨，突破 6.9 万美元关口")
    assert "crypto" in keyword_tags("以太坊网络完成升级")
    assert "crypto" in keyword_tags("ETH gas fees drop sharply")
    assert "crypto" in keyword_tags("Solana 生态 DeFi 协议锁仓量创新高")
    assert "crypto" in keyword_tags("交易所新增稳定币交易对")
    assert "crypto" in keyword_tags("多家机构布局加密货币托管业务")


def test_crypto_keyword_avoids_common_word_false_positives() -> None:
    # eth / defi 用了 lookaround 边界，但仍需确认常见英文词不会被子串命中
    assert "crypto" not in keyword_tags("aesthetic renovation trends for 2026")
    assert "crypto" not in keyword_tags("the method used in this study is sound")
    assert "crypto" not in keyword_tags("analysts define the new policy framework")
    assert "crypto" not in keyword_tags("the budget deficit widened last quarter")


def test_stocks_keyword_matches_without_asset_type() -> None:
    assert "stocks" in keyword_tags("纳斯达克指数收盘上涨")
    assert "stocks" in keyword_tags("标普500再创新高")
    assert "stocks" in keyword_tags("道指小幅下跌")
    assert "stocks" in keyword_tags("美股三大指数集体收涨")
    assert "stocks" in keyword_tags("苹果公司季度财报超预期")
    assert "stocks" in keyword_tags("英伟达 ETF 盘前上涨3%")
    assert "stocks" in keyword_tags("科技股盘后交易活跃")


def test_asset_types_signal_maps_to_crypto() -> None:
    # trending route 独有字段：asset_types 非空即视为 crypto 信号
    assert tags_for(
        title="Niu Lai Meme market value surpasses 52 million", content="",
        asset_type="", provenance="followin_trending", asset_types=("牛来", "BSC", "MEME"),
    )[:1] != ("stocks",)
    tags = tags_for(
        title="something", content="", asset_type="", provenance="feeds",
        asset_types=("BTC",),
    )
    assert "crypto" in tags


def test_upstream_category_majorcoin_maps_to_crypto() -> None:
    tags = tags_for(
        title="something", content="", asset_type="", provenance="feeds",
        upstream_category="majorcoin",
    )
    assert "crypto" in tags


def test_tags_for_new_params_are_optional_and_backward_compatible() -> None:
    # 现有调用点（不传新参数）不能因签名变化报错
    assert tags_for(title="BTC up", content="", asset_type="crypto", provenance="feeds") == (
        "crypto",
    )


# 本地新闻库扩充：美股 + 加密板块关键词补充。


def test_crypto_expansion_keywords_match() -> None:
    cases = (
        "SOL 突破新高带动山寨币反弹",
        "BNB 生态活跃度上升",
        "XRP 诉讼案迎来新进展",
        "现货ETF净流入创单日新高",
        "某交易所遭遇巨鲸转账",
        "资金费率转负，市场情绪谨慎",
        "Web3 项目融资再创新高",
        "以太坊 Layer 2 rollup 项目锁仓量上升",
        "Meme coin 板块集体拉升",
        "模因币概念股集体拉升",
        "RWA 赛道成为下一个叙事",
        "某项目完成代币解锁",
        "某平台完成代币空投",
        "Airdrop 预期升温",
        "CFTC 加强加密监管",
        "币安上线新交易对",
        "Coinbase 股价随比特币波动",
    )
    for text in cases:
        assert "crypto" in keyword_tags(text), f"{text!r} 应命中 crypto"


def test_crypto_expansion_accepted_residual_risks() -> None:
    # 以下几个词存在已知、评估过可接受的误报风险（见 tagger.py 内联注释），
    # 这里只确认加密语境本身命中，不代表豁免了对应的误报场景。
    assert "crypto" in keyword_tags("DOGE 突破 0.5 美元关口")  # 也是美国政府部门缩写
    assert "crypto" in keyword_tags("TON 生态用户数突破新纪录")  # 也是英文计量单位"吨"
    assert "crypto" in keyword_tags("SEC 起诉某交易所")  # SEC 监管范围不止加密
    assert "crypto" in keyword_tags("多头爆仓超十亿美元")  # 传统杠杆期货也用这个词
    assert "crypto" in keyword_tags("某交易所遭遇巨鲸大额转账")  # 银行业通用术语


def test_crypto_expansion_does_not_add_risky_bare_terms() -> None:
    # 明确排除的裸词不应该被规则表收录：清算/L2/Base/以太/空投/Meme
    # 泛化程度太高，会跨领域误报（验证方式：构造真实的碰撞语境，而不只是
    # 确认词表里没有这个字符串）
    assert "crypto" not in keyword_tags("证券清算所公布最新结算规则")
    assert "crypto" not in keyword_tags("模型训练中加入 L2 正则化项")
    assert "crypto" not in keyword_tags("Base rate held steady by the central bank")
    assert "crypto" not in keyword_tags("公司新一代以太网交换机发布")
    assert "crypto" not in keyword_tags("救援物资空投至灾区")
    assert "crypto" not in keyword_tags("Meme stocks surge as retail traders pile in")
    assert "crypto" not in keyword_tags("The new switch operates at Layer 2 of the OSI model")


def test_stocks_expansion_keywords_match() -> None:
    cases = (
        "TSLA 交付量超预期",
        "AAPL 新品发布会临近",
        "MSFT 云业务增长强劲",
        "GOOG 广告收入回暖",
        "AMZN 电商业务持续扩张",
        "META 元宇宙投入加大",
        "苹果公司季度营收创新高",
        "道琼斯指数收盘走高",
        "Nasdaq 科技股普涨",
        "S&P 500 再创历史新高",
        "多家公司 earnings report 集中发布",
        "本季度进入 earnings season",
    )
    for text in cases:
        assert "stocks" in keyword_tags(text), f"{text!r} 应命中 stocks"


def test_stocks_expansion_does_not_add_risky_bare_terms() -> None:
    # 裸"苹果"撞水果语境；裸"Earnings"泛指任何收入来源，不是财报特有信号
    assert "stocks" not in keyword_tags("黄金周期间苹果销量大增")
    assert "stocks" not in keyword_tags("Earnings from his side job helped pay rent")


def test_macro_expansion_keywords_match() -> None:
    cases = (
        "gold price hits fresh record",
        "spot gold price surges",
        "现货黄金价格突破新高",
        "原油库存超预期增加",
        "Crude Oil prices tumble",
        "WTI 原油期货收跌",
        "Brent 原油价格反弹",
        "美元指数走强施压风险资产",
        "DXY 指数触及年内高点",
        "美债收益率飙升",
        "10Y Treasury yield climbs above 5%",
    )
    for text in cases:
        assert "macro" in keyword_tags(text), f"{text!r} 应命中 macro"


def test_macro_expansion_does_not_add_risky_bare_terms() -> None:
    # 裸"黄金"/"Gold"都会撞中英文各自的习语，改用限定词组
    assert "macro" not in keyword_tags("十一黄金周旅游订单大增")
    assert "macro" not in keyword_tags("这是属于他的黄金时代")
    assert "macro" not in keyword_tags("Team USA wins Gold Medal in relay")
    assert "macro" not in keyword_tags("This policy is the Gold Standard for reform")
