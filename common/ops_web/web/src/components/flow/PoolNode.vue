<script setup>
/**
 * 未归类池节点（2026-10-17 F3 从 FlowPage.vue 抽出）：
 * 成员输出端口：拉线到区域/部门端口＝归口。纯展示+事件上抛。
 */
import { POOL_W } from '../../flow/layout.js';

defineProps({
  node: { type: Object, required: true },      // {id, x, y}
  members: { type: Array, default: () => [] },
  busy: { type: Boolean, default: false },
});
const emit = defineEmits(['drag', 'wire-start']);
</script>

<template>
  <article
    class="node pool-node"
    :style="{ left: node.x + 'px', top: node.y + 'px', width: POOL_W + 'px' }"
  >
    <div class="node-head" title="拖动移动节点" @mousedown="emit('drag', $event, node)">
      <h3>未归类池</h3>
      <span class="badge plain">{{ members.length }}</span>
    </div>
    <p class="pool-hint">从成员右侧端口拉线到区域节点＝归口</p>
    <div
      v-for="member in members"
      :key="member.user_id"
      class="pool-row"
    >
      <span class="pool-name">{{ member.name || member.user_id }}</span>
      <span class="pool-dept">{{ member.dept_name || '' }}</span>
      <span
        class="port out member-port"
        :class="{ busy }"
        title="拉线到区域/部门端口＝归口"
        @mousedown="emit('wire-start', $event, member)"
      />
    </div>
    <p v-if="!members.length" class="pool-hint">全员已归口</p>
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
.node-head h3 {
  margin: 0;
  font-size: 15px;
}
.badge {
  padding: 1px 7px;
  border-radius: 10px;
  font-size: 11px;
}
.badge.plain {
  background: #f3f4f6;
  color: #6b7280;
}
.pool-node {
  padding-bottom: 8px;
}
.pool-hint {
  margin: 0;
  padding: 0 10px 6px;
  color: #9ca3af;
  font-size: 11px;
}
.pool-row {
  position: relative;
  display: flex;
  align-items: center;
  gap: 6px;
  height: 30px;
  padding: 0 10px;
  font-size: 12px;
}
.pool-name {
  font-weight: 600;
}
.pool-dept {
  color: #9ca3af;
  font-size: 11px;
}
.port {
  position: absolute;
  width: 10px;
  height: 10px;
  border-radius: 50%;
  border: 2px solid #fff;
  box-shadow: 0 0 0 1px #cbd2da;
}
.port.out {
  right: -6px;
  top: 50%;
  margin-top: -5px;
  background: #10b981;
}
.pool-row .port.out {
  top: 10px;
  margin-top: 0;
}
.member-port {
  right: -6px;
  cursor: crosshair;
}
.member-port.busy {
  opacity: 0.4;
  pointer-events: none;
}
</style>
