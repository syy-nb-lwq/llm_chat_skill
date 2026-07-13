"""SelfReflectLoop - 主动反思循环:在低负载时主动复盘近期经验,生成洞察"""
import asyncio
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

from infra.logger import get_logger


# ---- Feature Flag ----
SELF_EVOLUTION_ENABLED = False


def _load_flag():
    try:
        from infra.config import config
        return bool(config.self_evolution_enabled)
    except Exception:
        return False


def get_self_evolution_enabled() -> bool:
    return _load_flag()


@dataclass
class ReflectionReport:
    """反思报告"""
    id: str
    timestamp: str
    trigger_reason: str  # "high_failure_count" / "same_scenario_3x" / "user_request"
    failure_count: int
    success_count: int
    high_freq_failures: List[Dict]  # 高频失败场景
    skill_suggestions: List[Dict]   # 可优化技能建议
    strengthened_skills: List[str]   # 强化技能(成功率高的)
    memory_summary: str             # 记忆摘要
    pending_patches_count: int


@dataclass
class ReflectConfig:
    """反思配置"""
    # 触发条件
    failure_threshold: int = 5       # 24h 内失败超过此值触发
    same_scenario_threshold: int = 3  # 同一场景失败超过此值触发
    check_interval_s: int = 3600     # 检查间隔(秒),默认 1 小时

    # 限制
    max_auto_merge_per_day: int = 3  # 单日最多自动合并次数
    min_confidence_for_auto: float = 0.85  # 自动合并的最低置信度


class SelfReflectLoop:
    """主动反思循环

    职责:
    1. 在低负载时(如 Session 空闲、凌晨),主动复盘近期经验,生成洞察
    2. 检测高频失败场景,生成 SkillPatch
    3. 评估 Skill 表现,建议优化

    触发条件(满足任一):
    - 24h 内失败记录 > 5 条
    - 同一场景失败 3 次
    - 用户请求"复盘近期表现"

    安全护栏:
    - 不直接修改 system_prompt
    - Skill 修改需有 source="merged" 标记
    - 单日最多自动合并 3 次
    """

    def __init__(
        self,
        config: Optional[ReflectConfig] = None,
    ):
        self.logger = get_logger()
        self.config = config or ReflectConfig()
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._daily_merge_count = 0
        self._last_reset_date = datetime.now().date()

    async def start(self):
        """启动反思循环(后台运行)"""
        if not get_self_evolution_enabled():
            self.logger.info("SelfReflectLoop", "自我进化未启用,跳过启动")
            return

        if self._running:
            return

        self._running = True
        self._task = asyncio.create_task(self._loop())
        self.logger.info("SelfReflectLoop", f"启动反思循环,检查间隔: {self.config.check_interval_s}s")

    async def stop(self):
        """停止反思循环"""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self.logger.info("SelfReflectLoop", "反思循环已停止")

    async def _loop(self):
        """反思循环主函数"""
        while self._running:
            try:
                # 重置每日计数
                today = datetime.now().date()
                if today > self._last_reset_date:
                    self._daily_merge_count = 0
                    self._last_reset_date = today

                # 检查是否需要反思
                report = await self.check_and_reflect()
                if report:
                    self.logger.info(
                        "SelfReflectLoop",
                        f"生成反思报告: {report.id}, 触发原因: {report.trigger_reason}"
                    )
                    # 保存报告
                    self._save_report(report)

            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error("SelfReflectLoop", f"反思循环异常: {e}")

            await asyncio.sleep(self.config.check_interval_s)

    async def check_and_reflect(self) -> Optional[ReflectionReport]:
        """检查是否需要反思,如果需要则生成报告"""
        from core.memory import get_memory_store

        store = get_memory_store()

        # 获取近期失败记录
        recent_failures = store.get_recent_failures(top_k=50)

        # 检查触发条件
        trigger_reason = self._check_triggers(recent_failures)
        if not trigger_reason:
            return None

        # 生成反思报告
        report = await self._generate_report(recent_failures, store, trigger_reason)
        return report

    def _check_triggers(self, failures: List) -> Optional[str]:
        """检查触发条件"""
        # 条件1: 24h 内失败超过阈值
        if len(failures) >= self.config.failure_threshold:
            return "high_failure_count"

        # 条件2: 同一场景失败超过阈值
        scenario_counts: Dict[str, int] = {}
        for f in failures:
            scenario = getattr(f, 'scenario', '') or ''
            if scenario:
                scenario_counts[scenario] = scenario_counts.get(scenario, 0) + 1

        for scenario, count in scenario_counts.items():
            if count >= self.config.same_scenario_threshold:
                return f"same_scenario_3x:{scenario}"

        return None

    async def _generate_report(
        self,
        failures: List,
        store,
        trigger_reason: str,
    ) -> ReflectionReport:
        """生成反思报告"""
        # 统计高频失败场景
        high_freq = self._analyze_failures(failures)

        # 获取成功记录
        stats = store.get_stats()

        # 获取待审阅的 patches
        pending = store.get_pending_patches()

        # 生成洞察
        insights = await self._generate_insights(high_freq, failures)

        report = ReflectionReport(
            id=f"reflect_{datetime.now().strftime('%Y%m%d%H%M%S')}",
            timestamp=datetime.now().isoformat(),
            trigger_reason=trigger_reason,
            failure_count=len(failures),
            success_count=stats.get("total_successes", 0),
            high_freq_failures=high_freq,
            skill_suggestions=insights["skill_suggestions"],
            strengthened_skills=insights["strengthened_skills"],
            memory_summary=f"共 {len(failures)} 条失败记录, {stats.get('total_successes', 0)} 条成功记录",
            pending_patches_count=len(pending),
        )

        return report

    def _analyze_failures(self, failures: List) -> List[Dict]:
        """分析失败记录,提取高频场景"""
        # 按场景分组统计
        by_scenario: Dict[str, List] = {}
        for f in failures:
            scenario = getattr(f, 'scenario', '') or 'unknown'
            if scenario not in by_scenario:
                by_scenario[scenario] = []
            by_scenario[scenario].append(f)

        # 提取高频场景
        high_freq = []
        for scenario, records in by_scenario.items():
            if len(records) >= 2:  # 至少 2 次失败
                # 提取共同诊断
                diagnoses = [getattr(r, 'diagnosis', '') or '' for r in records]
                common_diagnosis = self._find_common_pattern(diagnoses)

                high_freq.append({
                    "scenario": scenario,
                    "count": len(records),
                    "common_diagnosis": common_diagnosis,
                    "latest_trace_id": getattr(records[0], 'trace_id', ''),
                })

        # 按失败次数降序
        high_freq.sort(key=lambda x: -x["count"])
        return high_freq[:5]  # 最多 5 个高频场景

    def _find_common_pattern(self, strings: List[str]) -> str:
        """找出一组字符串的共同模式"""
        if not strings:
            return ""

        # 简单策略:找最短字符串的关键词
        shortest = min(strings, key=len)
        words = shortest.split()

        # 找所有字符串都包含的词
        common = []
        for word in words:
            if len(word) > 2 and all(word in s for s in strings):
                common.append(word)

        return " ".join(common[:3]) if common else strings[0][:50]

    async def _generate_insights(
        self,
        high_freq: List[Dict],
        failures: List,
    ) -> Dict[str, Any]:
        """生成洞察和建议"""
        insights = {
            "skill_suggestions": [],
            "strengthened_skills": [],
        }

        # 从高频失败生成建议
        for hf in high_freq[:3]:
            suggestion = {
                "target_skill": hf.get("scenario", ""),
                "diagnosis": hf.get("common_diagnosis", ""),
                "recommendation": f"建议优化 {hf['scenario']} 技能,避免 {hf['common_diagnosis']}",
            }
            insights["skill_suggestions"].append(suggestion)

        # 找出成功率高的技能(无需改动)
        # 这需要更复杂的分析,暂时留空
        insights["strengthened_skills"] = []

        return insights

    def _save_report(self, report: ReflectionReport):
        """保存反思报告"""
        from pathlib import Path
        import json

        base = Path(__file__).parent.parent / "memory" / "reflections"
        base.mkdir(parents=True, exist_ok=True)

        month_dir = base / datetime.now().strftime("%Y-%m")
        month_dir.mkdir(parents=True, exist_ok=True)

        path = month_dir / f"{report.id}.json"
        path.write_text(
            json.dumps(asdict(report), ensure_ascii=False),
            encoding="utf-8"
        )
        self.logger.info("SelfReflectLoop", f"保存反思报告: {report.id}")

    async def request_reflection(self) -> Optional[ReflectionReport]:
        """用户请求复盘:立即生成反思报告"""
        if not get_self_evolution_enabled():
            return None

        from core.memory import get_memory_store
        store = get_memory_store()
        failures = store.get_recent_failures(top_k=50)

        report = await self._generate_report(
            failures, store, "user_request"
        )
        self._save_report(report)
        return report


def start_reflect_loop():
    """启动反思循环的便捷函数"""
    loop = SelfReflectLoop()
    try:
        asyncio.run(loop.start())
    except Exception as e:
        from infra.logger import get_logger
        get_logger().error("SelfReflectLoop", f"启动失败: {e}")
    return loop
