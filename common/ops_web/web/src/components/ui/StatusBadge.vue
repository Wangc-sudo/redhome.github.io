<script setup>
import { computed } from 'vue';

const props = defineProps({
  status: { type: String, default: 'unknown' },
  labels: { type: Object, default: () => ({}) },
});
const defaultLabels = {
  fresh: '正常', degraded: '部分异常', stale: '已超期', blocked: '已阻塞', unknown: '状态未知',
};
const label = computed(() => props.labels[props.status] || defaultLabels[props.status] || props.status);
</script>

<template>
  <span class="status-badge" :class="`status-${status}`">{{ label }}</span>
</template>

<style scoped>
.status-badge {
  display: inline-flex;
  align-items: center;
  border-radius: 999px;
  padding: 2px 8px;
  font-size: 12px;
  white-space: nowrap;
  border: 1px solid #d1d5db;
  background: #f3f4f6;
  color: #4b5563;
}
.status-fresh { color: #047857; background: #ecfdf5; border-color: #a7f3d0; }
.status-degraded { color: #a16207; background: #fffbeb; border-color: #fde68a; }
.status-stale { color: #c2410c; background: #fff7ed; border-color: #fed7aa; }
.status-blocked { color: #b91c1c; background: #fef2f2; border-color: #fecaca; }
.status-unknown { color: #6b7280; background: #f9fafb; border-color: #d1d5db; }
</style>
