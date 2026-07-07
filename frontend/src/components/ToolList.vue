<template>
  <div class="tool-list">
    <h4>可用工具</h4>
    <div v-if="tools.length === 0" class="empty">加载中...</div>
    <div v-for="tool in tools" :key="tool.name" class="tool-item">
      <span class="tool-icon">⚙️</span>
      <div class="tool-info">
        <strong>{{ tool.name }}</strong>
        <p>{{ tool.description }}</p>
      </div>
    </div>
  </div>
</template>

<script>
export default {
  name: 'ToolList',
  props: { tools: { type: Array, default: () => [] } },
}
</script>

<style scoped>
.tool-list h4 { margin: 0 0 8px 0; font-size: 14px; color: #555; }
.empty { color: #999; padding: 8px; text-align: center; font-size: 13px; }
.tool-item {
  display: flex;
  gap: 8px;
  padding: 8px;
  margin-bottom: 6px;
  background: rgba(102, 126, 234, 0.05);
  border-radius: 6px;
}
.tool-icon { font-size: 20px; }
.tool-info strong { font-size: 13px; display: block; }
.tool-info p { margin: 2px 0 0; font-size: 11px; color: #666; }
</style>