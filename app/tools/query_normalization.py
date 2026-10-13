"""搜索关键词归一化 —— 剥离口语化的请求外壳词，提升 Tavily / 本地新闻库的检索命中率。

用户原话（如「帮我查一下最近黄金和比特币为什么涨了又跌了」）直接透传给搜索引擎，
噪音词会稀释检索相关性，也会让缓存 key 因措辞不同而错失命中。这里只做确定性的
规则清洗，不引入额外的 LLM 调用（否则会拖慢刚做完的联网搜索快速通道）。
"""

from __future__ import annotations

import re

_LEADING_FILLER_PATTERN = re.compile(
    "^(?:帮我查一下|帮我查查|帮我查|帮我|请问|请你|麻烦你|麻烦|我想知道|我想问|"
    "可以告诉我吗|可以告诉我|能不能|能否|你能不能|你能|你可以|查一下|查查)+"
)
_TRAILING_FILLER_PATTERN = re.compile("(?:好吗|吗|呢|啊|？|\\?|！|!)+$")
_SPLIT_PATTERN = re.compile(r"[和与、，,\s]+")
_STOPWORDS = {"and", "the", "a", "an", "of", "for", "to", "is", "are"}
_MAX_QUERY_LENGTH = 200

# 时效性/资讯类噪音词：对 Tavily 检索本身无害（保留与否不影响语义搜索），
# 但会污染本地新闻库的子串匹配（"比特币最新消息" 匹配不到标题里的"比特币"），
# 所以只在拆关键词时剥离，不进 normalize_search_query。
_TOPIC_NOISE_PATTERN = re.compile("最近|最新|今天|今日|本周|刚刚|新闻|动态|消息")


def normalize_search_query(raw: str) -> str:
    """剥离常见请求外壳词（"帮我查一下"…"吗？"），保留核心诉求，并裁剪长度。

    只在句首/句尾剥离，绝不在句子中间做无边界的子串替换——中文没有单词
    边界，中间替换会把"申请""邀请""不能否认"这类合法词斩断（例如把
    "不能否认" 里的"能否"删掉，语义直接反转）。
    """
    text = raw.strip()
    text = _LEADING_FILLER_PATTERN.sub("", text).strip()
    text = _TRAILING_FILLER_PATTERN.sub("", text).strip()
    normalized = text or raw.strip()
    return normalized[:_MAX_QUERY_LENGTH]


def extract_search_keywords(raw: str, *, max_keywords: int = 3) -> list[str]:
    """从归一化后的查询里拆出候选关键词（如「黄金」「比特币」），供本地新闻库做子串匹配。

    拆不出有效词（如整句本身就是一个专有名词）时，回退为归一化后的整句。
    """
    normalized = normalize_search_query(raw)
    de_noised = _TOPIC_NOISE_PATTERN.sub("", normalized)
    tokens = [
        token
        for token in _SPLIT_PATTERN.split(de_noised)
        if len(token) >= 2 and token.lower() not in _STOPWORDS
    ]
    return tokens[:max_keywords] or [normalized]
