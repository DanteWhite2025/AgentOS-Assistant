"""搜索关键词归一化单测。"""

from __future__ import annotations

from app.tools.query_normalization import extract_search_keywords, normalize_search_query


def test_normalize_strips_common_filler_phrases():
    assert normalize_search_query("帮我查一下最近黄金价格") == "最近黄金价格"


def test_normalize_strips_trailing_question_particles():
    assert normalize_search_query("比特币为什么跌了呢？") == "比特币为什么跌了"


def test_normalize_falls_back_to_original_when_stripped_to_empty():
    assert normalize_search_query("请问吗？") == "请问吗？"


def test_normalize_truncates_overly_long_queries():
    long_query = "黄金" * 150
    result = normalize_search_query(long_query)
    assert len(result) == 200


def test_normalize_does_not_corrupt_words_containing_filler_substrings():
    # 中文没有单词边界，无锚点的子串替换会把"申请""邀请""不能否认"这类合法词斩断。
    assert normalize_search_query("某公司申请破产保护最新进展") == "某公司申请破产保护最新进展"
    assert normalize_search_query("美联储不能否认经济衰退风险") == "美联储不能否认经济衰退风险"
    assert normalize_search_query("SBF邀请三箭资本合作") == "SBF邀请三箭资本合作"


def test_normalize_only_strips_filler_at_leading_or_trailing_position():
    assert normalize_search_query("请问美联储下次议息时间") == "美联储下次议息时间"
    assert normalize_search_query("美联储下次议息时间是什么时候呢") == "美联储下次议息时间是什么时候"


def test_extract_keywords_splits_on_conjunctions():
    assert extract_search_keywords("黄金和比特币为什么涨了又跌了") == ["黄金", "比特币为什么涨了又跌了"]


def test_extract_keywords_filters_english_stopwords():
    assert extract_search_keywords("gold and bitcoin news") == ["gold", "bitcoin", "news"]


def test_extract_keywords_falls_back_to_whole_query_when_no_tokens_survive():
    assert extract_search_keywords("ETH") == ["ETH"]


def test_extract_keywords_respects_max_keywords():
    result = extract_search_keywords("黄金、原油、美债、股指、比特币", max_keywords=2)
    assert result == ["黄金", "原油"]
