# LLM Chat Skill

具有自主学习能力的智能助手 Agent。

## 特性

- 🌐 **网页抓取** - 读取网页内容
- 📄 **文件读取** - 支持 PDF、TXT 等
- 🔧 **代码执行** - 安全执行 Python 代码
- 🔌 **插件系统** - 易于扩展

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
├── core/           # 核心
│   ├── agent.py    # Agent 核心
│   ├── plugin.py  # 插件基类
│   └── context.py # 上下文
├── plugins/        # 插件
│   ├── web.py     # 网页插件
│   ├── file.py    # 文件插件
│   └── code.py    # 代码插件
├── storage/        # 存储
│   ├── skill.py   # 技能存储
│   └── memory.py  # 记忆存储
├── infra/         # 基础设施
│   └── config.py  # 配置
├── ui/            # 界面
│   ├── cli.py     # 命令行
│   └── app.py    # Web UI
```

## License

MIT
