"""命令行界面

C-02: 增加 `diagnose` 子命令展示 provider / feature flag / 工具源 / embedding 状态。
保持无参数时仍走原 chat 循环,向后兼容 `ui.cli_main` 调用方。
"""
import argparse
import sys

from core.agent import Agent


def _cmd_diagnose() -> int:
    """打印配置/工具/embedding 诊断信息,返回退出码。"""
    from infra.config import config, ConfigError
    # Windows 默认 stdout 可能是 GBK,无法打印部分 Unicode 符号;
    # 这里用 ASCII 标记,保证跨编码可读。
    OK = "[OK]"
    NO = "[--]"

    print("=" * 60)
    print("Skill Agent 诊断")
    print("=" * 60)

    # ---- 1. Provider ----
    print("\n[Provider]")
    print(f"  default_provider : {config.default_provider}")
    print(f"  openai_base_url  : {config.openai_base_url}")
    print(f"  openai_model     : {config.openai_model}")
    print(f"  openai_api_key   : {'(set)' if config.openai_api_key else '(MISSING)'}")
    if config.anthropic_api_key:
        print(f"  anthropic_model  : {config.anthropic_model}")
        print(f"  anthropic_api_key: (set)")
    if config.multi_provider_enabled:
        print(f"  multi_provider   : enabled")

    # ---- 2. Feature flags ----
    print("\n[Feature flags]")
    flag_names = [
        "self_evolution_enabled",
        "multi_provider_enabled",
        "semantic_memory_enabled",
        "soul_enabled",
        "skill_dag_enabled",
        "tool_cache_enabled",
        "mcp_enabled",
    ]
    for name in flag_names:
        val = getattr(config, name, None)
        marker = OK if val else NO
        print(f"  {marker} {name:<28} = {val}")

    # ---- 3. 工具源 ----
    print("\n[Tool sources]")
    try:
        from tools.hub import get_tool_hub
        hub = get_tool_hub()
        summary = hub.health_summary()
        print(f"  total_sources   : {summary.get('total_sources', 0)}")
        print(f"  connected       : {summary.get('connected', 0)}")
        print(f"  failed          : {summary.get('failed', 0)}")
        print(f"  disconnected    : {summary.get('disconnected', 0)}")
        print(f"  has_failures    : {summary.get('has_failures', False)}")
        sources = hub.get_source_status()
        if sources:
            print("  ---")
            for src_name, st in sources.items():
                state = st.get("state", "unknown")
                enabled = "enabled" if st.get("enabled") else "disabled"
                tool_count = st.get("tool_count", 0)
                err = st.get("error") or ""
                err_str = f" err={err}" if err else ""
                print(f"  - {src_name:<24} [{state}/{enabled}] tools={tool_count}{err_str}")
        else:
            print("  (no sources registered)")
    except Exception as e:
        print(f"  (failed to query tool hub: {e})")

    # ---- 4. Embedding ----
    print("\n[Embedding]")
    print(f"  embedding_provider : {config.embedding_provider}")
    print(f"  embedding_model    : {config.embedding_model}")
    print(f"  embedding_dimension : {config.embedding_dimension}")
    try:
        from infra.embedding import get_embedding_service
        svc = get_embedding_service()
        if svc is None:
            print("  service_instance   : (not initialized)")
        else:
            print(f"  service_instance   : {type(svc).__name__}")
            print(f"  dimension          : {svc.dimension}")
    except Exception as e:
        print(f"  service_instance   : (error: {e})")

    # ---- 5. 配置合法性 ----
    print("\n[Config validation]")
    try:
        config.validate()
        print(f"  {OK} validate() passed")
    except ConfigError as e:
        print(f"  {NO} validate() FAILED: {e}")
        return 2

    print("\n" + "=" * 60)
    return 0


def _cmd_chat() -> int:
    """原 chat 循环,保持向后兼容。"""
    print("=" * 50)
    print("📚 Skill Agent")
    print("=" * 50)

    agent = Agent()

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
    return 0


def main(argv=None) -> int:
    """主入口:支持 `chat`(默认)/ `diagnose` 两个子命令。"""
    parser = argparse.ArgumentParser(
        prog="skill-agent",
        description="Skill Agent CLI(C-02: diagnose 子命令展示运行时状态)",
    )
    sub = parser.add_subparsers(dest="cmd")

    sub.add_parser("chat", help="进入交互式聊天(默认行为)")
    sub.add_parser("diagnose", help="打印 provider / feature / 工具源 / embedding 状态")

    args = parser.parse_args(argv)

    # 无子命令时走 chat,保持向后兼容
    if args.cmd is None or args.cmd == "chat":
        return _cmd_chat()
    if args.cmd == "diagnose":
        return _cmd_diagnose()
    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
