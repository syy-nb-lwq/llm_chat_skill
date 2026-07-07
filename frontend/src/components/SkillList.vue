<template>
  <div class="skill-list">
    <h4>已学技能</h4>
    <div v-if="skills.length === 0" class="empty">暂无已学技能</div>
    <div v-for="skill in skills" :key="skill.name" class="skill-item">
      <strong>{{ skill.name }}</strong>
      <span class="skill-tags">{{ (skill.tags || []).join(', ') || '通用' }}</span>
      <p v-if="skill.capability" class="capability">{{ skill.capability }}</p>
    </div>
  </div>
</template>

<script>
export default {
  name: 'SkillList',
  props: { skills: { type: Array, default: () => [] } },
}
</script>

<style scoped>
.skill-list h4 { margin: 16px 0 8px 0; font-size: 14px; color: #555; }
.empty { color: #999; padding: 8px; text-align: center; font-size: 13px; }
.skill-item {
  padding: 8px;
  margin-bottom: 6px;
  background: rgba(118, 75, 162, 0.05);
  border-radius: 6px;
}
.skill-item strong { font-size: 13px; }
.skill-tags { font-size: 11px; color: #888; margin-left: 6px; }
.capability { margin: 4px 0 0; font-size: 11px; color: #666; }
</style>