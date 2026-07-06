# LLM Chat Skill

一个具有技能学习能力的 AI Agent，支持：

- 🌐 **网页抓取** - 读取网页内容
- 📄 **文件读取** - 支持 PDF、TXT、图片等
- 🧠 **记忆系统** - 记住用户偏好
- 📚 **技能学习** - 自动学习新技能
- 💾 **向量存储** - 支持语义检索
- 🔧 **代码执行** - 安全执行 Python 代码

## 快速开始

### 1. 安装依赖

```bash
pip install -r requirements.txt
```

### 2. 配置环境变量

创建 `.env` 文件：

```env
LLM_API_KEY=your_api_key
LLM_BASE_URL=https://api.openai.com/v1
LLM_MODEL=your_model
```

### 3. 运行

**命令行版本：**
```bash
python agent.py
```

**Web UI 版本：**
```bash
streamlit run app.py
```

## 项目结构

```
├── agent.py           # Agent 核心
├── app.py            # Web UI
├── config.py         # 配置
├── tools/
│   ├── registry.py   # 工具注册
│   ├── fetch.py      # 网页抓取
│   ├── skill.py       # 技能系统
│   ├── memory.py      # 记忆系统
│   ├── vector_store.py # 向量存储
│   └── code_runner.py # 代码执行
└── skills/           # 技能文件
```

## 核心功能

### 工具系统
Agent 拥有多种工具，可以根据任务自动调用：
- `fetch_webpage` - 抓取网页
- `read_file` / `read_pdf` / `read_image` - 读取文件
- `learn_skill` - 学习新技能
- `execute_skill` - 执行技能
- `run_code` - 执行代码
- `vector_*` - 向量存储操作

### 技能学习
Agent 可以根据需求自动学习新技能：
1. 分析需求
2. 搜索相关资料
3. 生成技能代码
4. 验证并保存

### 记忆系统
- 用户偏好学习
- 工具使用统计
- 上下文记忆

## License

MIT
