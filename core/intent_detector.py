"""Intent Detector - 分层意图识别器

架构:
  Layer 0: 快速规则过滤 (无 LLM 调用)
  Layer 1: 轻量模型分类 (可选, 本地小模型)
  Layer 2: LLM 深度理解 (最后手段)

意图类别:
  - GREETING/FAREWELL/THANKS/ACKNOWLEDGE/NEGATIVE: 闲聊类
  - WEATHER/SEARCH/CALCULATE/TRANSLATE/CODE/CUSTOM: 任务类
  - TEACH: 教导类
  - RETRY: 重试类
  - UNKNOWN: 未知
"""
from enum import Enum
from dataclasses import dataclass, field
from typing import List, Optional, Dict, Any, Callable
import re

from infra.logger import get_logger


# ===== 意图类别 =====
class IntentCategory(Enum):
    # 闲聊类 - 快速返回
    GREETING = "greeting"           # 问候
    FAREWELL = "farewell"           # 告别
    THANKS = "thanks"               # 感谢
    ACKNOWLEDGE = "acknowledge"     # 确认/收到
    NEGATIVE = "negative"           # 拒绝
    SELF_QUERY = "self_query"       # 询问自己（你是谁）
    
    # 任务类 - 需要工具
    WEATHER = "weather"             # 天气查询
    SEARCH = "search"              # 搜索
    CALCULATE = "calculate"        # 计算
    TRANSLATE = "translate"        # 翻译
    CODE = "code"                  # 代码相关
    CUSTOM = "custom"              # 自定义技能
    
    # 特殊意图
    TEACH = "teach"                # 教导新技能
    RETRY = "retry"                # 重试
    CLARIFY = "clarify"            # 需要澄清
    
    # 未知
    UNKNOWN = "unknown"


# ===== 闲聊关键词 =====
_GREETING_KEYWORDS = [
    "hi", "hello", "你好", "您好", "嗨", "hey", "yo", "哟", "hi~", "hello~",
]
_FAREWELL_KEYWORDS = [
    "再见", "拜拜", "bye", "下次见", "回头见", "告辞", "拜拜啦", "再见啦",
]
_THANKS_KEYWORDS = [
    "谢谢", "感谢", "thanks", "thx", "多谢", "谢啦",
]
_ACKNOWLEDGE_KEYWORDS = [
    "ok", "好的", "好", "嗯", "行", "可以", "没问题", "收到",
    "了解", "明白", "知道了", "好的好的", "收到啦", "好的呀",
]
_NEGATIVE_KEYWORDS = [
    "不要", "不用", "算了", "不了", "不用了",
]
_SELF_QUERY_KEYWORDS = [
    "你是谁", "你叫什么", "你会什么", "你是机器人吗", "你是什么",
]

# ===== 教导关键词 =====
_TEACH_KEYWORDS = [
    "教我", "教会", "教你", "学一下", "教一下", "教会我",
    "我要学", " teach ", "learn",
]

# ===== 重试关键词 =====
_RETRY_KEYWORDS = [
    "重新", "retry", "again", "再说一遍", "再来一次", "重新回答", "重新执行",
]


@dataclass
class IntentResult:
    """意图识别结果"""
    category: IntentCategory
    confidence: float = 1.0        # 置信度 0-1
    detail: str = ""              # 详细描述
    need_llm: bool = False         # 是否需要 LLM 进一步处理
    tool_type: Optional[str] = None  # 需要的工具类型
    entities: Dict[str, Any] = field(default_factory=dict)  # 提取的实体


class IntentDetector:
    """分层意图识别器"""
    
    def __init__(self):
        self.logger = get_logger()
        self._greeting_pattern = self._build_pattern(_GREETING_KEYWORDS)
        self._farewell_pattern = self._build_pattern(_FAREWELL_KEYWORDS)
        self._thanks_pattern = self._build_pattern(_THANKS_KEYWORDS)
        self._acknowledge_pattern = self._build_pattern(_ACKNOWLEDGE_KEYWORDS)
        self._negative_pattern = self._build_pattern(_NEGATIVE_KEYWORDS)
        self._self_query_pattern = self._build_pattern(_SELF_QUERY_KEYWORDS)
        self._teach_pattern = self._build_pattern(_TEACH_KEYWORDS)
        self._retry_pattern = self._build_pattern(_RETRY_KEYWORDS)
        
        # 工具关键词映射
        self._tool_keywords = {
            "weather_query": ["天气", "下雨", "气温", "温度", "冷", "热", "晴"],
            "web_search": ["搜索", "查", "找", "什么是", "怎么", "如何", "为什么"],
        }
    
    def _build_pattern(self, keywords: List[str]) -> re.Pattern:
        """构建关键词匹配模式"""
        escaped = [re.escape(k) for k in keywords]
        return re.compile("|".join(escaped), re.IGNORECASE)
    
    # ===== Layer 0: 快速规则匹配 =====
    
    def detect(self, user_input: str, context: Optional[List[Dict]] = None) -> IntentResult:
        """主入口: 分层意图识别"""
        text = user_input.strip()
        
        if not text:
            return IntentResult(
                category=IntentCategory.UNKNOWN,
                detail="空输入",
            )
        
        # ===== Layer 0: 快速规则匹配 =====
        result = self._layer0_rule(text)
        if result:
            self.logger.info("IntentDetector", f"Layer0 规则匹配: {result.category.value}")
            return result
        
        # ===== Layer 1: 关键词扩展分析 =====
        result = self._layer1_keyword(text)
        if result:
            self.logger.info("IntentDetector", f"Layer1 关键词匹配: {result.category.value}")
            return result
        
        # ===== Layer 2: 需要 LLM 处理 =====
        self.logger.info("IntentDetector", "Layer2 需要 LLM 处理")
        return IntentResult(
            category=IntentCategory.UNKNOWN,
            need_llm=True,
            detail="需要 LLM 深度理解",
        )
    
    def _layer0_rule(self, text: str) -> Optional[IntentResult]:
        """Layer 0: 极快速规则匹配"""
        
        # 1. 纯闲聊判断: 短文本 + 闲聊关键词
        if len(text) <= 5:
            for pattern, category, detail in [
                (self._greeting_pattern, IntentCategory.GREETING, "简短问候"),
                (self._farewell_pattern, IntentCategory.FAREWELL, "简短告别"),
                (self._thanks_pattern, IntentCategory.THANKS, "简短感谢"),
                (self._acknowledge_pattern, IntentCategory.ACKNOWLEDGE, "简短确认"),
            ]:
                if pattern.search(text):
                    return IntentResult(
                        category=category,
                        confidence=0.95,
                        detail=detail,
                    )
        
        # 2. 教导关键词
        if self._teach_pattern.search(text):
            return IntentResult(
                category=IntentCategory.TEACH,
                confidence=0.95,
                detail="教导新技能",
            )
        
        # 3. 重试关键词
        if self._retry_pattern.search(text):
            return IntentResult(
                category=IntentCategory.RETRY,
                confidence=0.95,
                detail="重试上次任务",
            )
        
        return None
    
    def _layer1_keyword(self, text: str) -> Optional[IntentResult]:
        """Layer 1: 关键词扩展分析
        
        注意: 长文本中的短关键词(如"好")不应直接匹配闲聊,
        需要结合上下文判断
        """
        
        # 对于短文本(<=10字符),更严格匹配
        is_short = len(text) <= 10
        
        # 检查闲聊类别(短文本更严格)
        if self._greeting_pattern.search(text):
            # 排除包含教导意图的文本
            if not self._teach_pattern.search(text) and not self._has_skill_keywords(text):
                return IntentResult(
                    category=IntentCategory.GREETING,
                    confidence=0.9,
                    detail="问候",
                )
        
        if self._farewell_pattern.search(text):
            return IntentResult(
                category=IntentCategory.FAREWELL,
                confidence=0.9,
                detail="告别",
            )
        
        if self._thanks_pattern.search(text):
            return IntentResult(
                category=IntentCategory.THANKS,
                confidence=0.9,
                detail="感谢",
            )
        
        # ACKNOWLEDGE 关键词: 仅在短文本或纯确认场景中匹配
        if self._acknowledge_pattern.search(text):
            # 长文本包含"好"字但不单独成句 → 不匹配闲聊
            if len(text) > 10 and "好" in text and "。" not in text and "，" not in text:
                pass  # 不匹配闲聊
            elif self._has_teach_intent(text):
                pass  # 教导场景不匹配闲聊
            else:
                return IntentResult(
                    category=IntentCategory.ACKNOWLEDGE,
                    confidence=0.85,
                    detail="确认/收到",
                )
        
        if self._negative_pattern.search(text):
            # 排除教导场景
            if not self._has_teach_intent(text):
                return IntentResult(
                    category=IntentCategory.NEGATIVE,
                    confidence=0.85,
                    detail="拒绝/否定",
                )
        
        if self._self_query_pattern.search(text):
            return IntentResult(
                category=IntentCategory.SELF_QUERY,
                confidence=0.9,
                detail="询问身份",
            )
        
        # 检查工具关键词
        for tool_type, keywords in self._tool_keywords.items():
            for kw in keywords:
                if kw in text:
                    return IntentResult(
                        category=IntentCategory.SEARCH if tool_type == "web_search" else IntentCategory.WEATHER,
                        confidence=0.7,
                        detail=f"检测到{tool_type}关键词",
                        tool_type=tool_type,
                    )
        
        return None
    
    def _has_skill_keywords(self, text: str) -> bool:
        """检查是否包含技能相关关键词"""
        skill_keywords = ["技能", "生成", "创建", "编写", "设计", "实现"]
        return any(kw in text for kw in skill_keywords)
    
    def _has_teach_intent(self, text: str) -> bool:
        """检查是否包含教导意图"""
        teach_phrases = [
            "生成", "创建", "编写", "教", "教会", "学",
            "技能", "方法", "流程", "步骤", "原则",
        ]
        return any(phrase in text for phrase in teach_phrases)
    
    def _extract_entities(self, text: str) -> Dict[str, Any]:
        """提取实体 (LLM 前的简单提取)"""
        entities = {}
        
        # 简单城市提取
        cities = ["北京", "上海", "深圳", "广州", "杭州", "厦门", "成都", "武汉", "西安", "南京"]
        for city in cities:
            if city in text:
                entities["city"] = city
                break
        
        # 简单日期提取
        date_patterns = [
            (r"今天", "today"),
            (r"明天", "tomorrow"),
            (r"后天", "day_after_tomorrow"),
            (r"昨天", "yesterday"),
        ]
        for pattern, value in date_patterns:
            if re.search(pattern, text):
                entities["date"] = value
                break
        
        return entities


# ===== 闲聊判断辅助函数 =====
def is_chitchat(category: IntentCategory) -> bool:
    """判断是否为闲聊类别"""
    return category in (
        IntentCategory.GREETING,
        IntentCategory.FAREWELL,
        IntentCategory.THANKS,
        IntentCategory.ACKNOWLEDGE,
        IntentCategory.NEGATIVE,
        IntentCategory.SELF_QUERY,
    )


def is_task(category: IntentCategory) -> bool:
    """判断是否为任务类别"""
    return category in (
        IntentCategory.WEATHER,
        IntentCategory.SEARCH,
        IntentCategory.CALCULATE,
        IntentCategory.TRANSLATE,
        IntentCategory.CODE,
        IntentCategory.CUSTOM,
    )


# ===== 单例 =====
_intent_detector: Optional[IntentDetector] = None


def get_intent_detector() -> IntentDetector:
    """获取意图检测器单例"""
    global _intent_detector
    if _intent_detector is None:
        _intent_detector = IntentDetector()
    return _intent_detector
