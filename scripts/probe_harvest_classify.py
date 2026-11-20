"""真实 LLM 实测：三次「充值后重新生成」会不会归到同一条能力档案。

这是本次重设计的核心假设。如果三次同类任务被归成三个不同的 key，
那"技能不断被完善"就无从谈起 —— 只是换了个说法的「一次一条」。

预期：
  1~2 次 → accumulating，且 key 相同
  3 次   → drafted（够格产出草稿）
  4~5 次 → 另一类任务，key 不同，重新从 1 开始数

跑法：python scripts/probe_harvest_classify.py
"""
import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.skill_harvest import SkillDraftStore, harvest  # noqa: E402
from core.skill_ledger import CompetenceLedger  # noqa: E402


class FakeResult:
    def __init__(self, success=True, error=""):
        self.success = success
        self.data = {"ok": True}
        self.error = error


def mk(tools):
    results = {f"t{i}": FakeResult() for i in range(len(tools))}
    specs = {f"t{i}": {"type": t, "params": {"note": "x"}} for i, t in enumerate(tools)}
    return results, specs


CASES = [
    ("重新生成，我新充值了金额", ["video_generate", "video_download"], 3),
    ("充值好了，再跑一条", ["video_generate", "video_download"], 2),
    ("额度充上了，重新生成刚才那条", ["video_generate", "upload_artifact"], 4),
    ("帮我把这 5 个品批量上架到店铺", ["web_search", "browser_open"], 5),
    ("这几个品今天上新，你批量处理一下", ["web_search", "browser_open"], 5),
    # 第 6 次回到第一类，且这次真踩了新坑 —— 应该产出「修订」而不是又一条技能
    ("充值显示到账了但还是生成失败，你看看是不是没到账", ["video_generate", "video_download"], 6),
]


async def main():
    tmp = Path(tempfile.mkdtemp())
    ledger = CompetenceLedger(path=tmp / "ledger.json")
    drafts = SkillDraftStore(base_path=tmp / "drafts")

    first_key = ""
    for i, (text, tools, rounds) in enumerate(CASES, 1):
        if i == 6:
            # 模拟"第 3 次产出的草稿已经被批准" —— 修订只发生在已成技能之后。
            # 用动态 key：模型每次起名会漂（这正是为什么要喂 catalog 让它复用）。
            ledger.bind_skill(
                first_key,
                skill_id="skill_demo",
                skill_name="充值后重生成",
                version="1.0.0",
            )
        results, specs = mk(tools)
        out = await harvest(
            text,
            results,
            rounds=rounds,
            task_specs=specs,
            drafts=drafts,
            ledger=ledger,
            session_id="probe",
        )
        print(f"[{i}] {text}")
        print(f"    status={out.status:<12} key={out.competence_key}")
        if out.status == "drafted" and not first_key:
            first_key = out.competence_key
        print(f"    {out.reason}")
        comp = ledger.get(out.competence_key) if out.competence_key else None
        if comp and comp.get("insights"):
            print(f"    insight: {comp['insights'][-1][:100]}")
        print()

    print("=== 能力档案 ===")
    for c in ledger.all():
        print(f"  {c['key']:<34} {len(c['evidence'])} 次  「{c['title']}」 tools={c['tools']}")
    print()
    print("=== 待批草稿 ===")
    for d in drafts.list():
        flag = f"修订 {d['revision_of']}" if d.get("revision_of") else "新增"
        val = d.get("value") or {}
        vtag = "有价值" if val.get("valuable") else "无价值"
        print(f"  [{flag}][{vtag}] {d['name']}: {d['capability']}")
        print(f"     判定: {val.get('reason', '')[:120]}")
        print(f"     {d['method'][:140]}...")


asyncio.run(main())
