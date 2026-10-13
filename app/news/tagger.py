"""关键词规则打标。

`news` 工具不返回任何行业/主题维度（macro 只存在于 metrics 工具），
所以 ai_tech 与 macro 必须自建规则。recommend 吃 provenance。

crypto/stocks **不能**只吃上游 asset_type：实测 search route（media/research，
2900 条/天的主力）不返回 asset_type，只有 trending route（约 50 条/天）有 ——
纯吃 asset_type 会让这两个 tag 在生产里几乎全空。所以这两个 tag 同时吃三路信号
（见 `tags_for`）：
1. 关键词规则表（本文件，兜住占大头的 search route）
2. 上游 asset_type 字段（crypto / tradfi）
3. trending route 独有的 asset_types（非空即视为 crypto）与
   upstream_category（majorcoin 视为 crypto）——设计文档 §8 允许规则表迭代。

**大小写与词边界规则**（这是本模块唯一的复杂点）：
- 含 CJK 的词：直接子串匹配（中文没有词边界概念）
- ASCII 词的边界不用 `\\b`：Python re 把 CJK 字符归入 `\\w`，"CPI数据公布"
  这类 ASCII 缩写紧贴汉字的写法在 `\\b` 下没有词边界，会被系统性漏标（本项目
  语料是 Followin 自产的中文摘要，这种写法是常态）。改用只认 ASCII 字母数字的
  lookaround：`(?<![A-Za-z0-9]){term}(?![A-Za-z0-9])`，两侧只要不是
  `[A-Za-z0-9]` 就算边界，汉字、标点、连字符、字符串首尾都满足。
- 单词条目（无空格）含大写字母：**大小写敏感**
  —— 否则 "Fed" 会命中 "he fed the dog"，"AI" 会命中 "said"/"Thailand"
- 单词条目全小写：大小写无关（如 tariff / inflation）
- 多词条目（如 "yield curve" / "Treasury yield"）：**大小写无关**
  —— 它们不会像 "Fed" 那样撞上高频英文常用词，敏感反而漏检全小写写法

代价：全大写缩写（单词条目）要求原文大写（新闻标题里 CPI/GDP 基本都大写），
lowercase 的 "cpi" 不命中。这是刻意取舍，误报比漏报更难排查。
"""

from __future__ import annotations

import re
from typing import Final

from app.news.models import NewsTag

_KEYWORD_RULES: Final[dict[NewsTag, tuple[str, ...]]] = {
    "ai_tech": (
        "NVDA", "NVIDIA", "英伟达", "AMD", "TSMC", "台积电", "OpenAI", "ChatGPT",
        "Anthropic", "Claude", "LLM", "GPU", "算力", "芯片", "半导体",
        "AI", "人工智能", "大模型", "数据中心",
    ),
    # 不用裸 "黄金"：中文里"黄金周""黄金时代""黄金搭档"等习语高频出现，
    # 与商品黄金无关，改用限定词组 "现货黄金"；不用裸 "Gold"：英文同样有
    # "Gold Medal"（奖牌）"Gold Standard"（卓越标准的习语）等标题大小写
    # 撞词，改用 "gold price" / "spot gold" 限定词组。
    "macro": (
        "CPI", "PPI", "PCE", "GDP", "非农", "NFP", "FOMC", "美联储", "Fed",
        "降息", "加息", "利率", "关税", "tariff", "通胀", "inflation", "失业率",
        "国债", "yield curve", "bond yield", "收益率", "央行", "财政部", "Powell", "鲍威尔",
        "Treasury yield", "Treasury Department", "美国财政部",
        "XAU", "现货黄金", "gold price", "spot gold", "原油", "Crude Oil", "WTI", "Brent",
        "美元指数", "DXY", "美债", "10Y Treasury",
    ),
    # 词的选取刻意避开本仓库已踩过的坑：
    # - 不用 "Gemini"（此前在 ai_tech 里因它是加密交易所被移除，这里也不引入）
    # - 不用裸 "yield" / "Treasury"（macro 规则已验证会撞 DeFi yield farming /
    #   DAO treasury，此处同理不单独收录）
    # - 不用 "链上"：中文里"产业链上""供应链上"等非加密语境高频出现同一子串，
    #   会系统性误报
    # - 不用裸 "清算"：中文里证券清算所/期货清算/公司清算都用这个词，不是
    #   加密特有信号，收录会系统性误标非加密的股票/宏观新闻
    # - 不用裸 "L2"/"Layer 2"：前者撞 CPU 二级缓存、机器学习 L2 正则化，
    #   后者撞 OSI 网络模型的"数据链路层"——都是 ai_tech 语料高频词，
    #   改用更具体的 "Layer 2 rollup"（这是以太坊扩容技术的专属术语，
    #   网络分层语境不会用 rollup 这个词）
    # - 不用裸 "Base"：英文里 "base rate"（基准利率）、"customer base"、
    #   句首大写的 "Base ..." 太常见，裸词收录误报率极高
    # - 不用裸 "以太"：撞"以太网"（Ethernet，网络技术高频词），改用完整的
    #   "以太坊"（已收录，无此冲突）
    # - 不用裸 "空投"：中文里救灾/军事物资空投是这个词的主要用法，
    #   改用限定词组 "代币空投"；英文 "Airdrop" 在本语料无此歧义，保留
    # - 不用裸 "Meme"：撞 "meme stocks"（游戏驿站式散户炒作股，是股票概念
    #   不是加密概念），改用限定词组 "Meme coin" / "模因币"
    "crypto": (
        "BTC", "bitcoin", "比特币", "比特", "ETH", "ethereum", "以太坊",
        "Solana", "SOL", "BNB", "XRP",
        # DOGE 同时是加密货币代码和美国政府部门缩写（Department of
        # Government Efficiency）；TON 同时是英文计量单位"吨"（大小写敏感
        # 缓解但不能完全消除）；SEC 监管所有证券而不只是加密（会让纯股票类
        # SEC 新闻也带上 crypto 标签）；大额转账是较通用的银行业术语；
        # 爆仓在传统杠杆期货/外汇交易里也常用——以上均为接受的已知误报风险，
        # 认为发生频率可接受，暂不单独消解。
        "DOGE", "TON", "SEC", "大额转账", "爆仓",
        "稳定币", "DeFi", "加密货币", "链上资产",
        "现货ETF", "ETF净流入", "ETF流出", "巨鲸", "资金费率",
        "Web3", "Layer 2 rollup", "Meme coin", "模因", "模因币", "RWA", "AI+Crypto",
        "代币解锁", "代币空投", "Airdrop",
        "CFTC", "币安", "Binance", "Coinbase",
    ),
    # 不用裸 "苹果"：中文里"黄金周吃苹果""苹果丰收"等水果语境常见，误报率高，
    # 改用限定词组 "苹果公司"；不用裸 "评级"：债券/ESG/酒店评级等非股票语境
    # 太常见，不是股票特有信号；"标普500" 被已有的 "标普" 覆盖，不重复收录；
    # 不用裸 "Earnings"：泛指任何收入来源（如打工收入），不是财报特有信号，
    # 改用限定词组 "earnings report" / "earnings season"。
    "stocks": (
        "纳斯达克", "标普", "道指", "美股", "财报", "财报超预期", "ETF", "盘前", "盘后",
        "NVDA", "英伟达", "TSLA", "特斯拉", "AAPL", "苹果公司", "MSFT", "微软",
        "GOOG", "谷歌", "AMZN", "亚马逊", "META", "AMD", "TSMC", "台积电",
        "道琼斯", "Nasdaq", "S&P 500", "earnings report", "earnings season",
    ),
}


def _compile(term: str) -> re.Pattern[str]:
    escaped = re.escape(term)
    if not term.isascii():
        return re.compile(escaped)
    # Python re 把 CJK 字符归入 \w，导致 \b 在 ASCII 词紧贴汉字时不生效
    # （例如"CPI数据公布"里 CPI 与"数"之间没有 \b）。改用只认 ASCII
    # 字母数字的 lookaround 做边界，两侧只要不是 [A-Za-z0-9] 就算边界，
    # 汉字、标点、连字符、字符串首尾都满足。
    boundary = rf"(?<![A-Za-z0-9]){escaped}(?![A-Za-z0-9])"
    if " " in term:
        # 多词条目（yield curve / Treasury yield 等）大小写不敏感：
        # 它们不会像 "Fed" 那样撞上高频英文常用词，敏感反而漏检全小写写法。
        return re.compile(boundary, re.IGNORECASE)
    flags = 0 if any(char.isupper() for char in term) else re.IGNORECASE
    return re.compile(boundary, flags)


_COMPILED: Final[dict[NewsTag, tuple[re.Pattern[str], ...]]] = {
    tag: tuple(_compile(term) for term in terms) for tag, terms in _KEYWORD_RULES.items()
}


def keyword_tags(text: str) -> tuple[NewsTag, ...]:
    """按规则表匹配出 tag。命中多个则全给。"""
    return tuple(
        tag for tag, patterns in _COMPILED.items()
        if any(pattern.search(text) for pattern in patterns)
    )


def tags_for(
    *,
    title: str,
    content: str,
    asset_type: str,
    provenance: str,
    asset_types: tuple[str, ...] = (),
    upstream_category: str = "",
) -> tuple[NewsTag, ...]:
    """一条新闻的完整标签。去重且保序。

    `asset_types` 与 `upstream_category` 是 trending route 独有字段，新增为
    可选参数以保持向后兼容——现有调用点（`normalizer.to_news_item`）与现有
    测试不传这两个参数时行为不变。
    """
    found: list[NewsTag] = []
    if provenance == "followin_trending":
        found.append("recommend")
    normalized_asset = asset_type.strip().lower()
    normalized_category = upstream_category.strip().lower()
    if normalized_asset == "crypto" or normalized_category == "majorcoin" or asset_types:
        found.append("crypto")
    elif normalized_asset == "tradfi":
        found.append("stocks")
    found.extend(keyword_tags(f"{title}\n{content}"))
    return tuple(dict.fromkeys(found))
