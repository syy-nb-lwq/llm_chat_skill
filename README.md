# LLM Chat Skill

具有**技能学习能力**的智能助手 Agent。

## 核心理念

**技能 = 方法论 + 处理流程 + 代码（可选）**

技能不只是代码，更是一套完成任务的方法论。

## 特性

- 🌐 **网页抓取** - 读取网页内容
- 📄 **文件读取** - 支持 PDF、TXT 等
- 🔧 **代码执行** - 安全执行 Python 代码
- 🔌 **插件系统** - 易于扩展
- 📚 **技能系统** - 方法论 + 流程 + 代码
- 🧠 **能力分析** - 分析任务需要的能力

## 快速开始

```bash
# 安装依赖
pip install -r requirements.txt

# 配置
cp .env.example .env
# 编辑 .env 填入 API Key

# 运行 CLI
python main.py

# 或运行 Web UI
streamlit run ui/app.py
```

## 项目结构

```
llm_chat_skill/
├── core/              # 核心
│   ├── agent.py        # Agent 核心
│   ├── plugin.py       # 插件基类
│   ├── context.py      # 上下文
│   └── capability.py   # 能力分析
├── plugins/            # 插件
│   ├── web.py         # 网页
│   ├── file.py        # 文件
│   └── code.py        # 代码
├── storage/            # 存储
│   ├── skill.py       # 技能存储
│   └── memory.py      # 记忆存储
├── ui/                # 界面
│   ├── cli.py         # 命令行
│   └── app.py        # Web UI
```

## 技能定义

```python
skill = Skill(
    name="数据分析",
    description="分析数据的常用流程",
    method="1. 理解数据结构\n2. 数据清洗\n3. 统计分析",
    steps=["理解数据", "数据清洗", "可视化"],
    code="",  # 可选
    tags=["分析", "数据"]
)
```

## License

MIT
