"""使用示例"""
from agent import StreamingAgent, chat


def example_basic():
    """基础使用"""
    print("=== 示例1: 使用 chat 便捷函数 ===")
    
    # 使用流式输出（默认）
    result = chat("帮我分析 https://en.wikipedia.org/wiki/B-2_Spirit 这架飞机")
    print(result)
    print()


def example_stream():
    """示例: 自定义流式回调"""
    print("=== 示例2: 自定义流式回调 ===")
    
    agent = StreamingAgent()
    
    def my_callback(stage: str, text: str):
        if stage == "thinking":
            print(f"[思考] {text}", end="", flush=True)
        elif stage == "answer":
            print(f"[回答] {text}", end="", flush=True)
        elif stage == "done":
            print()  # 换行
    
    result = agent.run_stream(
        "提取 C:/data/report.txt 里的关键信息",
        callback=my_callback
    )
    print()


def example_non_stream():
    """示例: 非流式调用"""
    print("=== 示例3: 非流式调用 ===")
    
    agent = StreamingAgent()
    result = agent.run("帮我看看 https://news.example.com/article 这篇文章的主要内容")
    print(result)
    print()


if __name__ == "__main__":
    # 基础示例
    example_basic()
    
    # 自定义回调示例
    # example_stream()
    
    # 非流式示例
    # example_non_stream()
