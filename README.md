# Skill Agent

**技能 = 方法论 + 步骤 + 代码（可选）**

## 核心理念

技能不只是代码，更是一套完成任务的方法论。

## 架构

```
┌──────────────────────────────────────┐
│          Skill Agent                      │
├──────────────────────────────────────┤
│  接收任务                            │
│  ↓                                  │
│  分析任务 → 选择/生成技能              │
│  ↓                                  │
│  方法论 → 指导分析思路                │
│  步骤 → 指导执行流程                │
│  代码 → 可选的执行补充                │
└──────────────────────────────────────┘
```

## 技能定义

| 字段 | 说明 |
|------|------|
| method | 分析问题的方法论 |
| steps | 处理步骤 |
| code | 可选的执行代码 |

## 快速开始

```bash
pip install -r requirements.txt
cp .env.example .env
# 配置 API Key

# CLI
python ui/cli.py

# Web
streamlit run ui/app.py
```

## 项目结构

```
├── core/          # Agent 核心
│   └── agent.py
├── storage/       # 存储
│   └── skill.py
├── infra/         # 配置
│   └── config.py
└── ui/            # 界面
```

## License

MIT
