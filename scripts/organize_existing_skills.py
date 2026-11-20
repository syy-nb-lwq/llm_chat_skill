"""归纳整理存量 skill 草稿：合并同类、去掉冗余、清除密钥。

背景：记忆机制重设计前攒下 15 条草稿，全是旧机制"做完一件事就产一条"的产物。
其中 7 条讲的是同一件"视频生成后可靠交付"的事，另有重复的记忆读取/调研类。
两条 origin 里还明文存着真实 API key 和内网地址 —— 必须清掉。

处理策略（人工逐条审过，不是算法聚类）：
  - 3 条归纳后的技能：把同类草稿的判断合并成一条，去掉工具名/密钥/站点细节
  - 12 条删掉：被合并的重复项，以及本就不该是 skill 的（记忆读取、纯调研）

安全：
  - 执行前完整备份到 memory/skills/drafts.bak-<时间戳>/（含密钥的原始文件也在备份里，
    因此备份目录权限同源码目录，只作回滚用；清理完成后由用户决定是否删备份）
  - 幂等：标记写入整理后的草稿 metadata，重跑会识别并跳过
  - 新草稿 id 用内容 hash 派生，和旧 id 不同，便于区分"整理前/后"

用法：python scripts/organize_existing_skills.py [--apply]
      不带 --apply 是 dry-run，只打印计划。
"""
import argparse
import hashlib
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.skill_harvest import assess_method_quality, assess_value  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DRAFTS = ROOT / "memory" / "skills" / "drafts"
ORGANIZED_MARK = "reorganized_20261120"

# ---------------------------------------------------------------------------
# 归纳后的 3 条。method 都按"判断 + 坑 + 完成标准"写，不点名工具、不带密钥。
# ---------------------------------------------------------------------------
KEPT = [
    {
        "old_ids": [
            "skill_ebc8c20a3c",  # 视频重试交付法
            "skill_725d56be42",  # 自主闭环交付法
            "skill_3b7c41c7d8",  # 充值后重生成
            "skill_d9ff4fc530",  # 充值完成处理
            "skill_af9718ddf1",  # 新接口生成发布（含密钥）
            "skill_faa6cd2c1d",  # 国内站视频生成
            "skill_981cdf7bfe",  # AI短剧视频生成（含密钥）
        ],
        "name": "视频产物可靠交付",
        "capability": "视频/媒体产物生成后如何可靠交付，避免误报完成",
        "insight": (
            "充值恢复的只是执行条件，不会自动替换上次失败的产物；"
            "发布失败要先分清是产物不合格还是调用偶发失败，前者回去改内容、后者直接重试。"
        ),
        "method": (
            "视频类任务有三个独立的失败面，必须逐个确认，不能把『脚本跑完』当成『交付完成』。"
            "第一，产物层：生成成功提示不等于产物可用，要把产物重新读回、核对内容，读不回就是内容本身有问题。"
            "第二，发布层：发布失败先分辨是产物不合格（路径、格式、内容）还是调用偶发失败——"
            "看起来该直接重试，其实前者回去改内容重试也没用，后者才该直接重试同名发布，不要因为一次失败就终止任务。"
            "第三，状态层：用户说『充值好了』『额度到了』只恢复执行条件，不会自动替换之前失败的产物，"
            "必须重新生成并取回验证，拿旧的失败产物凑数等于没做。"
            "完成标准是产物可读回、内容核对一致、发布返回成功三件事同时成立，缺任何一项都不算交付。"
            "阶段性产物要尽早交付成可检验对象，而不是全部做完再一次性给出。"
        ),
        "patterns": [
            "视频还没交付", "生成并发布视频", "充值后重新生成",
            "发布失败了怎么办", "把视频发给我", "额度到了再跑一次",
        ],
        "tags": ["harvested", "reorganized"],
    },
    {
        "old_ids": [
            "skill_77f972fb3a",  # 对话式电商工具搭建
            "skill_1b56c836b1",  # 本地AI小说工具
        ],
        "name": "对话式工具搭建",
        "capability": "用户要一套工具让你能对话式做事时，怎么拆、怎么验、怎么交付",
        "insight": (
            "先定义工具再验证调用，别边想边造；工具建好必须真的调一次确认可用，"
            "否则交付的是一堆跑不起来的空壳。"
        ),
        "method": (
            "遇到『帮我写些工具，让我能对话式做某件事』这类请求，先拆场景再动手，不要边想边写。"
            "先按业务环节列出缺哪些能力，每项定义一个工具：它解决什么、需要什么参数、为什么需要；"
            "关键判断是——工具不是越多越好，能覆盖用户说的那个闭环就行，别把一次性需求也做成工具。"
            "踩过的坑是工具建好后不验证就交付，用户对着对话一用就报错；"
            "所以每个工具都要实际调用一次，确认参数和返回符合预期，跑不通就当场修或删。"
            "完成标准是：工具集能被独立调用成功，且用户知道每个工具在什么场景用。"
            "交付时把工具说明和使用场景一起给，而不是只丢一堆代码。"
        ),
        "patterns": [
            "帮我写几个工具", "对话式做某件事", "搭一个助手",
            "写工具让我能用", "做一套能聊天操作的工具", "本地就能用",
        ],
        "tags": ["harvested", "reorganized"],
    },
    {
        "old_ids": [
            "skill_3a4e5651da",  # 选品工具能力盘点
            "skill_e1cddc8994",  # 选品上架两步法
        ],
        "name": "选品评分与上架文案",
        "capability": "用评分维度筛选候选商品，再产出可上架的商品信息",
        "insight": (
            "选品评分要先按成本、售价、佣金、广告、运费、退货率、竞争度多维测算，"
            "单看某一项（比如热度）会选出看着火但赚钱的品。"
        ),
        "method": (
            "选品不是凭感觉挑，要把判断拆成可核对的维度再下结论。"
            "先按成本、售价、佣金、广告费、运费、退货率、竞争度、热度逐项测算候选商品，"
            "判断标准是综合利润空间和结构，不能只看热度高就上——这是最常见的坑。"
            "筛出的商品再结合关键词和使用场景产出上架文案与商品信息。"
            "如果一次给多个候选，要逐个打分、分别给结论，不要混成一句笼统建议。"
            "完成标准是：每个候选都有可追溯的评分依据，且筛出的品能直接拿去上架，缺任何一项都不算做完。"
        ),
        "patterns": [
            "帮我选品", "给商品打分", "选品并上架", "生成上架信息",
            "这个商品能上架吗", "批量选品",
        ],
        "tags": ["harvested", "reorganized"],
    },
]

# 明确删掉的：被上面合并的之外，本就不该是 skill 的
DROP_REASONS = {
    "skill_5a53df500c": "用户偏好读取 —— 记忆系统已有能力，不是 skill",
    "skill_865efe56af": "用户偏好回忆法 —— 记忆系统已有能力，不是 skill",
    "skill_d6d1c867c9": "产品快速调研 —— 纯搜索抓取，planner 自会，无迁移判断",
    "skill_1faf6a3199": "AI长篇小说工具调研 —— 一次性方案调研，非可复用方法",
}

ALL_KNOWN = {i for k in KEPT for i in k["old_ids"]} | set(DROP_REASONS)


def new_id(name: str) -> str:
    h = hashlib.sha1(name.encode("utf-8")).hexdigest()[:10]
    return f"skill_{h}"


def make_draft(cfg: dict, origin: str) -> dict:
    from skills.models import Skill

    q = assess_method_quality(
        cfg["method"], origin=origin[:200], patterns=cfg["patterns"]
    )
    v = assess_value(cfg["method"], insight=cfg["insight"])
    sk = Skill(
        name=cfg["name"],
        version="1.0.0",
        capability=cfg["capability"][:120],
        method=cfg["method"],
        patterns=cfg["patterns"],
        tags=cfg["tags"],
        steps=[],
        examples=[origin[:200]],
        regression=[],
        id=new_id(cfg["name"]),
        source="harvested",
        author="reorganized",
        created_at=datetime.now().isoformat(),
        updated_at=datetime.now().isoformat(),
    )
    setattr(sk, "quality", q)
    setattr(sk, "value", v)
    from dataclasses import asdict

    return {
        "skill": asdict(sk),
        "origin": "归纳自 " + ", ".join(cfg["old_ids"]) + f"；原话示例：{origin[:80]}",
        "quality": q,
        "value": v,
        "reorganized_from": cfg["old_ids"],
        "organize_mark": ORGANIZED_MARK,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真正执行；不带则 dry-run")
    args = ap.parse_args()

    files = sorted(DRAFTS.glob("*.json"))
    by_id = {}
    for f in files:
        d = json.loads(f.read_text(encoding="utf-8"))
        by_id[f.stem] = d

    # 幂等：已有整理标记就直接退出
    if any(d.get("organize_mark") == ORGANIZED_MARK for d in by_id.values()):
        print("检测到整理标记，已整理过，跳过")
        return

    unknown = set(by_id) - ALL_KNOWN
    if unknown:
        print("有未识别的草稿，拒绝执行（避免误删）:", sorted(unknown))
        return

    print(f"发现 {len(files)} 条草稿，将归纳为 {len(KEPT)} 条、删除 {len(files) - len(KEPT)} 条")
    for cfg in KEPT:
        q = assess_method_quality(cfg["method"], patterns=cfg["patterns"])
        v = assess_value(cfg["method"], insight=cfg["insight"])
        print(f"  保留「{cfg['name']}」 <- {len(cfg['old_ids'])} 条 | "
              f"价值={v['valuable']} 质量reject={q['reject']}")
        if q["reject"] or not v["valuable"]:
            print("  !! 闸门未过，终止")
            return
    for oid, why in DROP_REASONS.items():
        print(f"  删除「{oid}」: {why}")

    if not args.apply:
        print("\n[dry-run] 加 --apply 才会真正执行")
        return

    # 备份（含密钥的原始文件）
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = DRAFTS.parent / f"drafts.bak-{stamp}"
    shutil.copytree(DRAFTS, backup)
    print(f"\n已备份 -> {backup}")

    # 删除所有旧草稿
    for f in files:
        f.unlink()

    # 写归纳后的草稿
    for cfg in KEPT:
        origin = ""
        for oid in cfg["old_ids"]:
            old = by_id.get(oid) or {}
            origin = origin or (old.get("origin") or "")
        payload = make_draft(cfg, origin)
        (DRAFTS / f"{payload['skill']['id']}.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"  写入 {payload['skill']['id']}.json  {cfg['name']}")

    print(f"\n完成：{len(files)} -> {len(KEPT)}")


if __name__ == "__main__":
    main()
