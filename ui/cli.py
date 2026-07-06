"""命令行界面"""
from openai import OpenAI

from core.agent import Agent
from infra.config import config


def main():
    print("=" * 50)
    print("📚 Skill Agent")
    print("=" * 50)
    
    client = OpenAI(api_key=config.llm_api_key, base_url=config.llm_base_url)
    agent = Agent(llm_client=client)
    
    print("输入 quit 退出\n")
    
    while True:
        try:
            task = input("任务: ").strip()
            if not task:
                continue
            
            if task.lower() in ["quit", "exit"]:
                print("再见!")
                break
            
            if task.lower() == "reset":
                agent.reset()
                print("已重置\n")
                continue
            
            print()
            result = agent.chat(task)
            print(f"\n结果:\n{result}\n")
            print("-" * 50)
            
        except KeyboardInterrupt:
            print("\n再见!")
            break
        except Exception as e:
            print(f"错误: {e}\n")


if __name__ == "__main__":
    main()
