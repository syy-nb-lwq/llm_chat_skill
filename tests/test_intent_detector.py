"""意图检测器测试"""
import pytest
from core.intent_detector import (
    IntentCategory, IntentDetector, get_intent_detector,
    is_chitchat, is_task
)


class TestIntentDetector:
    """分层意图识别测试"""

    @pytest.fixture
    def detector(self):
        return IntentDetector()

    # ===== Layer 0: 快速规则测试 =====

    def test_greeting_short(self, detector):
        """简短问候 → GREETING"""
        result = detector.detect("你好")
        assert result.category == IntentCategory.GREETING
        assert result.confidence > 0.9

    def test_farewell_short(self, detector):
        """简短告别 → FAREWELL"""
        result = detector.detect("再见")
        assert result.category == IntentCategory.FAREWELL

    def test_thanks_short(self, detector):
        """简短感谢 → THANKS"""
        result = detector.detect("谢谢")
        assert result.category == IntentCategory.THANKS

    def test_acknowledge_short(self, detector):
        """简短确认 → ACKNOWLEDGE"""
        result = detector.detect("好的")
        assert result.category == IntentCategory.ACKNOWLEDGE

    def test_teach_keyword(self, detector):
        """教导关键词 → TEACH"""
        result = detector.detect("教我做饭")
        assert result.category == IntentCategory.TEACH

    def test_retry_keyword(self, detector):
        """重试关键词 → RETRY"""
        result = detector.detect("重新回答")
        assert result.category == IntentCategory.RETRY

    # ===== Layer 1: 关键词扩展测试 =====

    def test_greeting_long(self, detector):
        """长问候 → GREETING"""
        result = detector.detect("你好,今天天气怎么样?")
        assert result.category == IntentCategory.GREETING

    def test_farewell_long(self, detector):
        """长告别 → FAREWELL"""
        result = detector.detect("我先走了,再见")
        assert result.category == IntentCategory.FAREWELL

    def test_self_query(self, detector):
        """询问身份 → SELF_QUERY"""
        result = detector.detect("你是谁")
        assert result.category == IntentCategory.SELF_QUERY

    def test_weather_keywords(self, detector):
        """天气关键词 → WEATHER"""
        result = detector.detect("今天厦门热不热")
        assert result.category == IntentCategory.WEATHER

    def test_search_keywords(self, detector):
        """搜索关键词 → SEARCH"""
        result = detector.detect("什么是人工智能")
        assert result.category == IntentCategory.SEARCH

    # ===== Layer 2: 需要 LLM =====

    def test_complex_input(self, detector):
        """复杂输入 → 需要 LLM"""
        result = detector.detect("帮我分析一下明天的会议安排")
        # 需要 LLM 判断具体任务类型
        assert result.need_llm == True

    # ===== 工具函数测试 =====

    def test_is_chitchat(self):
        """is_chitchat 工具函数"""
        assert is_chitchat(IntentCategory.GREETING) == True
        assert is_chitchat(IntentCategory.FAREWELL) == True
        assert is_chitchat(IntentCategory.THANKS) == True
        assert is_chitchat(IntentCategory.WEATHER) == False

    def test_is_task(self):
        """is_task 工具函数"""
        assert is_task(IntentCategory.WEATHER) == True
        assert is_task(IntentCategory.SEARCH) == True
        assert is_task(IntentCategory.GREETING) == False


class TestIntentDetectorEdgeCases:
    """边界情况测试"""

    def test_empty_input(self):
        detector = IntentDetector()
        result = detector.detect("")
        assert result.category == IntentCategory.UNKNOWN

    def test_whitespace_only(self):
        detector = IntentDetector()
        result = detector.detect("   ")
        assert result.category == IntentCategory.UNKNOWN

    def test_mixed_intent(self):
        """混合意图: 问候 + 任务"""
        detector = IntentDetector()
        result = detector.detect("你好,帮我查下天气")
        # 应该识别为 GREETING(优先) 或需要 LLM
        assert result.category in (IntentCategory.GREETING, IntentCategory.UNKNOWN)

    def test_teach_with_acknowledgement_word(self):
        """教导场景: 包含"好"字但不是闲聊"""
        detector = IntentDetector()
        result = detector.detect("好，我来教你怎么写日报")
        # 应该识别为 TEACH，不是 ACKNOWLEDGE 闲聊
        assert result.category == IntentCategory.TEACH

    def test_skill_generation_not_chitchat(self):
        """技能生成场景: 包含"生成"等词不应是闲聊"""
        detector = IntentDetector()
        result = detector.detect("生成一个技能用来做每日的日报编写")
        # 应该交给 LLM 处理
        assert result.need_llm == True


class TestEntityExtraction:
    """实体提取测试"""

    def test_city_extraction(self):
        detector = IntentDetector()
        entities = detector._extract_entities("厦门今天天气")
        assert "city" in entities
        assert entities["city"] == "厦门"

    def test_date_extraction(self):
        detector = IntentDetector()
        entities = detector._extract_entities("明天北京天气")
        assert "date" in entities
        assert entities["date"] == "tomorrow"

    def test_multiple_entities(self):
        detector = IntentDetector()
        entities = detector._extract_entities("后天上海温度")
        assert "city" in entities
        assert "date" in entities


class TestSingleton:
    """单例测试"""

    def test_singleton(self):
        d1 = get_intent_detector()
        d2 = get_intent_detector()
        assert d1 is d2
