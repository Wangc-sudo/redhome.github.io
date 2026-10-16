<script setup>
/**
 * 全局节点列（2026-10-17 F3 从 FlowPage.vue 抽出）：
 * 播报日历 / 操作记录 / 授权 / 数据修正——点开对应抽屉。纯展示+事件上抛。
 */
import { GLOBAL_H, GLOBAL_W } from '../../flow/layout.js';

defineProps({
  nodes: { type: Array, default: () => [] },
});
const emit = defineEmits(['drag', 'open']);
</script>

<template>
  <article
    v-for="node in nodes"
    :key="node.id"
    class="node global-node"
    :style="{ left: node.x + 'px', top: node.y + 'px', width: GLOBAL_W + 'px', height: GLOBAL_H + 'px' }"
  >
    <div class="node-head" title="拖动移动节点" @mousedown="emit('drag', $event, node)">
      <h4>{{ node.label }}</h4>
    </div>
    <button class="global-open" @click="emit('open', node)">
      {{ node.desc }} →
    </button>
  </article>
</template>

<style scoped>
.node {
  position: absolute;
  border: 1px solid #e2e5ea;
  border-radius: 10px;
  background: #fff;
  box-shadow: 0 1px 3px rgba(16, 24, 40, 0.08);
}
.node-head {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 10px 4px;
  cursor: move;
  user-select: none;
}
.node-head h4 {
  margin: 0;
  font-size: 13px;
  font-weight: 600;
}
.global-node {
  padding-bottom: 4px;
  border-style: dashed;
  background: #fdfdff;
}
.global-open {
  display: block;
  margin: 0 10px;
  padding: 2px 0 6px;
  border: none;
  background: none;
  color: #2563eb;
  font-size: 12px;
  cursor: pointer;
  text-align: left;
}
</style>
