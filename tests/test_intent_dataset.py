"""意图识别和技能执行集成测试数据集

运行方式:
  python -m pytest tests/test_intent_dataset.py -v
"""
import pytest
from core.intent_detector import IntentCategory, IntentDetector, is_chitchat


# ===== 测试数据集 =====
INTENT_TEST_CASES = [
    # === 闲聊类 ===
    {
        "input": "你好",
        "expected_category": IntentCategory.GREETING,
        "description": "简短问候",
    },
    {
        "input": "嗨",
        "expected_category": IntentCategory.GREETING,
        "description": "简短问候2",
    },
    {
        "input": "再见",
        "expected_category": IntentCategory.FAREWELL,
        "description": "简短告别",
    },
    {
        "input": "谢谢",
        "expected_category": IntentCategory.THANKS,
        "description": "简短感谢",
    },
    {
        "input": "好的",
        "expected_category": IntentCategory.ACKNOWLEDGE,
        "description": "简短确认",
    },
    {
        "input": "你是谁",
        "expected_category": IntentCategory.SELF_QUERY,
        "description": "询问身份",
    },
    
    # === 教导类 ===
    {
        "input": "教我做饭",
        "expected_category": IntentCategory.TEACH,
        "description": "教导意图",
    },
    {
        "input": "教你一个技能",
        "expected_category": IntentCategory.TEACH,
        "description": "教导意图2",
    },
    
    # === 重试类 ===
    {
        "input": "重新回答",
        "expected_category": IntentCategory.RETRY,
        "description": "重试意图",
    },
    {
        "input": "再说一遍",
        "expected_category": IntentCategory.RETRY,
        "description": "重试意图2",
    },
    
    # === 天气类 ===
    {
        "input": "今天厦门天气怎么样",
        "expected_category": IntentCategory.WEATHER,
        "description": "天气查询",
    },
    {
        "input": "明天北京热不热",
        "expected_category": IntentCategory.WEATHER,
        "description": "天气查询2",
    },
    
    # === 搜索类 ===
    {
        "input": "什么是人工智能",
        "expected_category": IntentCategory.SEARCH,
        "description": "搜索查询",
    },
    {
        "input": "怎么学Python",
        "expected_category": IntentCategory.SEARCH,
        "description": "搜索查询2",
    },
    
    # === 复杂任务类(应交给LLM) ===
    {
        "input": "帮我分析一下明天的会议安排",
        "expected_need_llm": True,
        "description": "复杂任务-交给LLM",
    },
    {
        "input": "生成一个技能用来做每日的日报编写",
        "expected_need_llm": True,
        "description": "技能生成-交给LLM",
    },
    
    # === 边界情况 ===
    {
        "input": "好，我来教你怎么写日报",
        "expected_not_category": IntentCategory.ACKNOWLEDGE,
        "description": "教导场景不应识别为闲聊确认",
    },
    {
        "input": "",
        "expected_category": IntentCategory.UNKNOWN,
        "description": "空输入",
    },
    {
        "input": "   ",
        "expected_category": IntentCategory.UNKNOWN,
        "description": "空白输入",
    },
    {
        "input": "不要，我不想学这个",
        "expected_not_category": IntentCategory.NEGATIVE,
        "description": "否定+教导场景",
    },
]


class TestIntentDataset:
    """意图识别数据集测试"""

    @pytest.fixture
    def detector(self):
        return IntentDetector()

    @pytest.mark.parametrize("case", INTENT_TEST_CASES, ids=lambda c: c["description"])
    def test_intent(self, detector, case):
        """测试每一条意图识别用例"""
        result = detector.detect(case["input"])
        
        # 如果指定了 expected_category
        if "expected_category" in case:
            assert result.category == case["expected_category"], \
                f"输入: {case['input']}, 期望: {case['expected_category'].value}, 实际: {result.category.value}"
        
        # 如果指定了 expected_not_category
        if "expected_not_category" in case:
            assert result.category != case["expected_not_category"], \
                f"输入: {case['input']}, 不应识别为: {case['expected_not_category'].value}, 实际: {result.category.value}"
        
        # 如果指定了 expected_need_llm
        if "expected_need_llm" in case:
            assert result.need_llm == case["expected_need_llm"], \
                f"输入: {case['input']}, 期望 need_llm: {case['expected_need_llm']}, 实际: {result.need_llm}"


class TestIntentSummary:
    """测试摘要"""

    def test_dataset_summary(self):
        """打印测试数据集摘要"""
        print("\n" + "=" * 60)
        print("意图识别测试数据集摘要")
        print("=" * 60)
        
        categories = {}
        for case in INTENT_TEST_CASES:
            cat = case.get("expected_category", IntentCategory.UNKNOWN)
            cat_name = cat.value if isinstance(cat, IntentCategory) else "complex"
            categories[cat_name] = categories.get(cat_name, 0) + 1
        
        for cat, count in sorted(categories.items()):
            print(f"  {cat}: {count} 条")
        
        print(f"\n总计: {len(INTENT_TEST_CASES)} 条测试用例")
        print("=" * 60)
