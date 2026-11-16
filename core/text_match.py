"""CJK 友好的文本匹配

**为什么需要它**:FTS5 默认的 `unicode61` 分词器把连续汉字当成**一个** token。
所以「查询北京天气的记录」和「北京天气」永远匹配不上 ——
`unicode61` 切不出「北京」,`trigram` 又要求 ≥3 字符,两字中文同样无解。

结果:一个只面向中文的产品,全文检索通道等于**没有**。

所以中文部分放在 Python 侧做**二元组(bigram)**打分:
「北京天气」→ 北京 / 京天 / 天气,命中比例即得分。
英文/数字仍走整词匹配。

这套逻辑原本只在会话检索里用(`core/session_memory.py`),
长期记忆检索(`core/memory_db.py`)漏了 —— 抽到这里给两边共用。
"""
import re
from typing import List


#: 连续的 ASCII 字母/数字/下划线
_ASCII_RE = re.compile(r"[a-z0-9_]+")

#: 连续的 CJK 汉字(含扩展 A 区)
_CJK_RE = re.compile(r"[一-鿿㐀-䶿]+")


def query_fragments(query: str) -> List[str]:
    """把查询切成检索片段。

    - ASCII / 数字词:整词(长度 ≥2)
    - CJK 连续段:切二元组("北京天气" → 北京 / 京天 / 天气)
      单字段(如「查」)保留原样,否则两字查询会一个片段都没有
    """
    frags: List[str] = []
    seen = set()
    for word in _ASCII_RE.findall((query or "").lower()):
        if len(word) >= 2 and word not in seen:
            seen.add(word)
            frags.append(word)
    for seg in _CJK_RE.findall(query or ""):
        if len(seg) == 1:
            if seg not in seen:
                seen.add(seg)
                frags.append(seg)
            continue
        for i in range(len(seg) - 1):
            piece = seg[i:i + 2]
            if piece not in seen:
                seen.add(piece)
                frags.append(seg[i:i + 2])
    return frags


def score_fragments(content: str, fragments: List[str]) -> float:
    """命中比例(0~1)。"""
    if not fragments:
        return 0.0
    low = (content or "").lower()
    if not low:
        return 0.0
    hits = sum(1 for f in fragments if f in low)
    return hits / len(fragments)
