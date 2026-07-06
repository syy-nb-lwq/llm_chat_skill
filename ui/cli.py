"""命令行界面"""
from openai import OpenAI

from core.agent import Agent
from core.plugin import PluginRegistry
from plugins.web import WebPlugin
from plugins.file import FilePlugin
from plugins.code import CodePlugin
from infra.config import config


def main():
    print("=" * 60)
    print("AI Agent - 命令行版")
    print("=" * 60)
    print()
    
    # 初始化插件
    registry = PluginRegistry()
    registry.register(WebPlugin())
    registry.register(FilePlugin())
    registry.register(CodePlugin())
    
    # 初始化 LLM 客户端
    client = OpenAI(
        api_key=config.llm_api_key,
        base_url=config.llm_base_url
    )
    
    # 初始化 Agent
    agent = Agent(
        llm_client=client,
        registry=registry,
        system_prompt="""你是一个智能助手，有多种工具可以使用：
- fetch_webpage: 抓取网页
- read_file: 读取本地文件
- run_code: 执行 Python 代码

有需要获取外部信息时，先调用工具。
回答要简洁、准确。"""
    )
    
    print("输入 'quit' 退出\n")
    
    while True:
        try:
            user_input = input("你: ").strip()
            
            if not user_input:
                continue
            
            if user_input.lower() in ['quit', 'exit', 'q']:
                print("再见!")
                break
            
            if user_input.lower() == 'reset':
                agent.reset()
                print("对话已重置\n")
                continue
            
            print()
            response = agent.chat(user_input)
            print(f"助手: {response}\n")
            
        except KeyboardInterrupt:
            print("\n再见!")
            break
        except Exception as e:
            print(f"错误: {e}\n")


if __name__ == "__main__":
    main()
