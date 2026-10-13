"""送进 TTS 之前的朗读文本归一化。

火山 TTS 只认字面文本，看不到任何系统提示词 —— 品牌名怎么念，完全由它自己的
发音规则决定，实测会把「YesUP」念成「yesuv」。所以读音只能在「文本 → TTS」
这条边界上修：屏幕上仍然是 YesUP，送去合成的换成能被念对的写法。

这里守两件事：
* **该换的都换**：模型写出来的大小写变体（YesUP / YesUp / YESUP）都要命中；
* **不该换的别碰**：内部标识（yesup-core）不是品牌词，换了会念出破折号。
"""

from __future__ import annotations

import pytest

from app.voice.speech_text import DEFAULT_BRAND_SPOKEN_AS, normalize_for_speech


@pytest.mark.parametrize(
    "written",
    ["YesUP", "YesUp", "YESUP", "yesup", "Yesup", "Yes Up", "yes up"],
)
def test_every_written_variant_is_spoken_the_same_way(written: str) -> None:
    """模型不一定每次都写标准大小写，读音不能因此漂移。

    「Yes Up」也在列：它当过一版朗读文本，线上实测被念成「Yes you」，
    所以模型若自己写出这个形态，同样要收敛回统一读音。
    """

    spoken = normalize_for_speech(f"你好，我是 {written} 智能助理。")

    assert spoken == f"你好，我是 {DEFAULT_BRAND_SPOKEN_AS} 智能助理。"


def test_the_default_reading_is_chinese_syllables_not_english() -> None:
    """这一条钉的是踩过的坑，不是审美偏好。

    豆包 TTS 2.0 不支持 SSML，这条链路没有音素级控制，读音完全取决于送进去的
    文本。英文写法走英文 G2P 不可控（YesUP→「yesuv」、Yes Up→「Yes you」/
    「Yes 优屁」），汉字走中文 TTS 才是确定的。改回英文写法前先看这条注释。
    """

    assert DEFAULT_BRAND_SPOKEN_AS == "耶斯阿普"
    assert DEFAULT_BRAND_SPOKEN_AS.isascii() is False


def test_the_reading_is_configurable_without_touching_code() -> None:
    """真机听感只有发上去才知道，改读法不该再发一次版。

    透传链路（vars → workflow → set_env → Settings）由
    tests/test_deploy_workflow_contract.py 守着 —— 漏了那一段，这个参数就只是
    个摆设：改 var 不报错、也不生效。
    """

    spoken = normalize_for_speech("YesUP 为您播报。", brand_spoken_as="耶斯·阿普")

    assert spoken == "耶斯·阿普 为您播报。"


@pytest.mark.parametrize("internal", ["yesup-core", "yesup-agent-runtime", "YesUPX"])
def test_internal_identifiers_are_left_alone(internal: str) -> None:
    """它们不是对外品牌名，替换只会让念出来更怪。"""

    assert normalize_for_speech(internal) == internal


def test_text_without_the_brand_is_returned_unchanged() -> None:
    """绝大多数句子不含品牌词，不能因为归一化产生任何差异。"""

    original = "BTC 当前市场价格为 77,104.5 USDC。"

    assert normalize_for_speech(original) == original


def test_repeated_mentions_are_all_replaced() -> None:
    original = "YesUP 智能助理，YesUP 为您播报。"

    assert normalize_for_speech(original) == "耶斯阿普 智能助理，耶斯阿普 为您播报。"
