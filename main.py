"""主入口 - 命令行交互"""
from agent import StreamingAgent
import traceback


def main():
    """命令行交互入口"""
    print("=" * 60)
    print("多数据源智能抽取 Agent (流式思考版)")
    print("=" * 60)
    print("支持的输入：")
    print("  - 网页: https://example.com/article")
    print("  - 文本文件: data/report.txt")
    print("  - 图片: image/screenshot.png")
    print("  - PDF: docs/report.pdf")
    print()
    print("思考过程将实时显示，绿色为正式回答，蓝色为思考过程")
    print("-" * 60)

    agent = StreamingAgent()

    # 流式回调
    def stream_callback(stage: str, text: str):
        if stage == "thinking_start":
            print("\033[36m", end="", flush=True)
        elif stage == "thinking":
            print(f"{text}", end="", flush=True)
        elif stage == "answer_start":
            print("\033[32m", end="", flush=True)
        elif stage == "answer":
            print(f"{text}", end="", flush=True)
        elif stage == "done":
            print("\033[0m", end="", flush=True)
            print()

    while True:
        try:
            user_input = input("\n你: ").strip()

            if user_input.lower() in ["quit", "exit", "q"]:
                print("再见!")
                break

            if not user_input:
                continue

            print("\n" + "-" * 40)
            
            try:
                # 详细调试
                print(f"[调试] 原始输入: {repr(user_input)}")
                
                # 清理后的文本
                clean = user_input.replace('\n', ' ').replace('\r', ' ')
                clean = ' '.join(clean.split())
                print(f"[调试] 清理后: {repr(clean)}")
                
                # 解析输入
                source, question = agent.parse_input(user_input)
                print(f"[调试] 检测到数据源: {source}")
                print(f"[调试] 问题: {question}")
                
                if source == "unknown":
                    print("[错误] 无法识别数据源")
                    # 尝试显示更多信息
                    import re
                    # 检测 URL
                    urls = re.findall(r'https?://\S+', clean)
                    print(f"[调试] 检测到的URL: {urls}")
                    # 检测 Windows 路径
                    win_paths = re.findall(r'[A-Z]:\\[^ ]+', clean, re.IGNORECASE)
                    print(f"[调试] 检测到的Windows路径: {win_paths}")
                    # 检测文件扩展名
                    exts = re.findall(r'\.[a-zA-Z0-9]+', clean)
                    print(f"[调试] 检测到的扩展名: {exts}")
                    continue
                
                print("-" * 40)
                print("思考过程:")
                print("-" * 40)
                
                # 获取内容
                content, source_desc = agent.get_content(source)
                print(f"[调试] 文件描述: {source_desc}")
                if content.startswith(("文件不存在", "不支持", "获取失败", "未知")):
                    print(f"[错误] {content}")
                    continue
                print(f"[调试] 内容长度: {len(content)} 字符")
                
                # 执行
                agent.run_stream(user_input, callback=stream_callback)
                
            except Exception as e:
                print(f"\n\033[31m[错误] {str(e)}\033[0m")
                print(f"\n[详细错误]:")
                traceback.print_exc()

        except KeyboardInterrupt:
            print("\n\n再见!")
            break
        except Exception as e:
            print(f"\n[错误] {str(e)}")
            traceback.print_exc()


if __name__ == "__main__":
    main()
