"""把书面文本改写成「能被 TTS 念对」的朗读文本。

为什么需要这一层：pipeline 链路的发声环节是火山 TTS，它是一个独立的商用合成
引擎 —— 只接收一串裸文本，看不到、也不执行任何系统提示词。所以「品牌名要念成
什么」这件事，在提示词里写多少遍都没用，只能在文本送进合成器之前改写。

只改朗读文本，不改显示文本：屏幕上和历史记录里始终是规范写法「YesUP」，
被替换的那一份只活到 TTS 请求体里。
"""

from __future__ import annotations

import re
from typing import Final

# 品牌名念法。用中文音节而不是英文拼写 —— 这是实测结论，不是审美选择：
# 豆包 TTS 2.0 不支持 SSML，这条链路上没有任何音素级发音控制手段，能决定读音的
# 只有送进去的文本本身。英文写法交给它的英文 G2P 就完全不可控，实测
# 「YesUP」念成「yesuv」，换成「Yes Up」又念成「Yes you」「Yes 优屁」——
# 再换第 N 个英文写法还是碰运气。而念汉字是中文 TTS 的基本功，结果是确定的。
DEFAULT_BRAND_SPOKEN_AS: Final = "耶斯阿普"

# 只认独立出现的品牌词：
# * 前后不许接字母数字 —— 「YesUPX」是别的词，不是品牌名；
# * 后面不许接连字符 —— 「yesup-core」是内部标识，念成「Yes Up-core」更怪。
# 中间允许一个空格：模型偶尔会自己写成「Yes Up」，而且线上曾经把这个写法当成
# 朗读文本发出去过，两种形态都要能收敛回同一个读音。
_BRAND: Final = re.compile(r"(?<![0-9A-Za-z])yes ?up(?![0-9A-Za-z-])", re.IGNORECASE)


def normalize_for_speech(text: str, *, brand_spoken_as: str = DEFAULT_BRAND_SPOKEN_AS) -> str:
    """返回 ``text`` 的朗读版本。原串不变。

    ``brand_spoken_as`` 留成参数是因为真机听感只有发上去才知道：换读法应该是
    改一个配置项，而不是再发一次版。
    """

    return _BRAND.sub(brand_spoken_as, text)
