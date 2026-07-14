"""Soul 身份系统单元测试"""
import pytest
from pathlib import Path
from unittest.mock import patch

from core.soul import Soul, SoulLoader, get_soul_loader, reset_soul_loader


class TestSoul:
    """Soul 数据结构测试"""
    
    def test_default_soul(self):
        """测试默认 Soul"""
        soul = Soul()
        assert soul.name == "Agent"
        assert "专业" in soul.personality
    
    def test_to_system_prompt(self):
        """测试转换为 system prompt"""
        soul = Soul(
            name="小智",
            personality="活泼开朗",
            communication_style="幽默风趣",
            values=["用户第一"],
            expertise=["编程"],
            standing_instructions=["先确认需求"],
            boundaries=["不撒谎"],
        )
        
        prompt = soul.to_system_prompt()
        assert "小智" in prompt
        assert "活泼开朗" in prompt
        assert "用户第一" in prompt
        assert "编程" in prompt
        assert "先确认需求" in prompt
        assert "不撒谎" in prompt


class TestSoulLoader:
    """SoulLoader 测试"""
    
    def setup_method(self):
        reset_soul_loader()
    
    def teardown_method(self):
        reset_soul_loader()
    
    def test_load_default(self):
        """测试加载不存在的文件返回默认"""
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            loader = SoulLoader(soul_path=Path(tmpdir) / "nonexistent.md")
            soul = loader.load()
            assert soul.name == "Agent"
    
    def test_load_with_frontmatter(self):
        """测试加载带 front-matter 的文件"""
        import tempfile
        content = '''---
name: "测试Agent"
personality: "测试性格"
communication_style: "测试风格"
---

# 核心价值观
- 价值1
- 价值2

# 专业领域
- 领域1
'''
        with tempfile.TemporaryDirectory() as tmpdir:
            soul_path = Path(tmpdir) / "SOUL.md"
            soul_path.write_text(content, encoding="utf-8")
            
            loader = SoulLoader(soul_path=soul_path)
            soul = loader.load()
            
            assert soul.name == "测试Agent"
            assert "价值1" in soul.values
            assert "领域1" in soul.expertise
    
    def test_hot_reload(self):
        """测试热重载"""
        import tempfile
        import time
        
        with tempfile.TemporaryDirectory() as tmpdir:
            soul_path = Path(tmpdir) / "SOUL.md"
            soul_path.write_text('---\nname: "初始"\n---\n', encoding="utf-8")
            
            loader = SoulLoader(soul_path=soul_path)
            soul1 = loader.load()
            assert soul1.name == "初始"
            
            # 修改文件
            time.sleep(0.1)  # 确保 mtime 不同
            soul_path.write_text('---\nname: "修改后"\n---\n', encoding="utf-8")
            
            soul2 = loader.load()
            assert soul2.name == "修改后"
    
    def test_save(self):
        """测试保存"""
        import tempfile
        
        with tempfile.TemporaryDirectory() as tmpdir:
            soul_path = Path(tmpdir) / "SOUL.md"
            loader = SoulLoader(soul_path=soul_path)
            
            soul = Soul(
                name="保存测试",
                personality="测试性格",
                values=["价值1"],
            )
            
            loader.save(soul)
            
            # 重新加载
            loader2 = SoulLoader(soul_path=soul_path)
            soul2 = loader2.load()
            
            assert soul2.name == "保存测试"
            assert "价值1" in soul2.values


class TestSoulIntegration:
    """集成测试"""
    
    def setup_method(self):
        reset_soul_loader()
    
    def test_singleton(self):
        """测试单例"""
        loader1 = get_soul_loader()
        loader2 = get_soul_loader()
        assert loader1 is loader2
    
    def test_with_real_soul_file(self):
        """测试使用真实的 soul 文件"""
        soul_path = Path(__file__).parent.parent / "soul" / "SOUL.md"
        if soul_path.exists():
            loader = SoulLoader(soul_path=soul_path)
            soul = loader.load()
            
            assert soul.name == "小智"
            assert "用户隐私" in soul.values or "用户隐私第一" in soul.values
    
    def test_parse_complex_soul(self):
        """测试解析复杂的 Soul 文件"""
        import tempfile
        
        content = '''---
name: "复杂Agent"
personality: "多才多艺"
communication_style: "专业但不失幽默"
---

# 核心价值观
- 诚实守信
- 持续改进
- 团队合作

# 专业领域
- Python 编程
- 数据分析
- 机器学习

# 持续性指令
- 每次回答前思考
- 不确定时主动询问
- 复杂问题分步骤

# 行为边界
- 不编造事实
- 不伤害用户
'''
        with tempfile.TemporaryDirectory() as tmpdir:
            soul_path = Path(tmpdir) / "SOUL.md"
            soul_path.write_text(content, encoding="utf-8")
            
            loader = SoulLoader(soul_path=soul_path)
            soul = loader.load()
            
            assert soul.name == "复杂Agent"
            assert len(soul.values) == 3
            assert len(soul.expertise) == 3
            assert len(soul.standing_instructions) == 3
            assert len(soul.boundaries) == 2
            
            # 测试转换为 prompt
            prompt = soul.to_system_prompt()
            assert "复杂Agent" in prompt
            assert "诚实守信" in prompt
            assert "Python 编程" in prompt
