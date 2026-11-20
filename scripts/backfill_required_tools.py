"""给存量 3 个技能回填 required_tools（幂等）。

这 3 个技能是归纳整理时重写的，只写了方法论（method），没写工具清单。
而子智能体现在按技能的 required_tools 裁剪工具表 —— 空清单会导致不裁剪，
所以必须回填。工具来自各技能方法论里明确涉及的能力。

用法：python scripts/backfill_required_tools.py
"""
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
USER = ROOT / "skills" / "user"

# 技能名 -> 需要的工具（来自方法论里描述的实际动作）
TOOLS = {
    "视频产物可靠交付": ["python_exec", "publish_artifact", "read_document"],
    "对话式工具搭建": ["create_tool", "python_exec", "web_search"],
    "选品评分与上架文案": ["ecom_product_scorer", "ecom_listing_gen"],
}


def main():
    changed = 0
    for name, tools in TOOLS.items():
        for f in USER.glob(f"{name}@*.yaml"):
            data = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
            if data.get("required_tools"):
                print(f"  跳过（已有）: {name}")
                continue
            data["required_tools"] = tools
            f.write_text(
                yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
                encoding="utf-8",
            )
            changed += 1
            print(f"  回填: {name} -> {tools}")
    print(f"完成，变更 {changed} 个")


if __name__ == "__main__":
    main()
