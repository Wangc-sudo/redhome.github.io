<script setup>
/**
 * 家族模块节点（2026-10-17 F3 从 FlowPage.vue 抽出）：区域→模块 N:1，
 * 点头部展开成员行（每行：启停开关/运行一次/详情开抽屉）；
 * 紫色输出端口拉线到别的模块＝建立依赖。纯展示+事件上抛。
 */
import { TASK_W } from '../../flow/layout.js';

defineProps({
  node: { type: Object, required: true },    // 模块节点（services 为成员任务）
  expanded: { type: Boolean, default: false },
  busy: { type: Boolean, default: false },
});
const emit = defineEmits([
  'drag', 'toggle', 'dep-wire-start', 'toggle-task', 'run-once', 'open-drawer',
]);
</script>

<template>
  <article
    class="node task-node module-node"
    :class="{ broken: node.brokenCount > 0, expanded }"
    :style="{ left: node.x + 'px', top: node.y + 'px', width: TASK_W + 'px' }"
  >
    <span class="port in module-in" :data-module-in="node.id" title="依赖输入（拉线至此＝本模块依赖来源模块）" />
    <span
      class="port out module-out"
      :class="{ busy }"
      title="依赖输出（拉线到别的模块＝对方依赖本模块）"
      @mousedown="emit('dep-wire-start', $event, node)"
    />
    <div class="node-head module-head" title="拖动移动；点击展开/收起成员" @mousedown="emit('drag', $event, node)">
      <h4>{{ node.label }}</h4>
      <span class="badge" :class="node.brokenCount ? 'warn' : 'ok'">
        {{ node.services.length - node.brokenCount }}/{{ node.services.length }} 正常
      </span>
      <button
        class="btn mini ghost expand-btn"
        @mousedown.stop
        @click.stop="emit('toggle', node)"
      >{{ expanded ? '收起 ⌃' : '展开 ⌄' }}</button>
    </div>
    <div v-if="expanded" class="module-members">
      <div
        v-for="service in node.services"
        :key="service.service_id"
        class="member-row"
        :class="{ disabled: !service.enabled }"
      >
        <div class="member-main">
          <b>{{ service.service_label }}</b>
          <span class="member-sched">
            {{ service.schedule_zh || service.schedule || '—' }}
            <template v-if="service.latest"> · {{ service.latest.status_label }}</template>
          </span>
        </div>
        <div class="member-actions">
          <button
            class="switch"
            :class="{ on: service.enabled }"
            :disabled="busy"
            :title="service.enabled ? '点击停用' : '点击启用'"
            @click="emit('toggle-task', service)"
          ><i /></button>
          <button class="btn mini" :disabled="busy" @click="emit('run-once', service)">运行一次</button>
          <button class="btn mini ghost" @click="emit('open-drawer', service)">详情</button>
        </div>
      </div>
    </div>
  </article>
</template>

<style scoped>
/* 按钮（复制旧 FlowPage scoped 变体，scoped 不外泄） */
.btn {
  padding: 7px 12px;
  border: 1px solid #d1d5db;
  border-radius: 6px;
  background: #fff;
  color: #1f2933;
  font-size: 13px;
  cursor: pointer;
}
.btn:hover {
  background: #f9fafb;
}
.btn:disabled {
  opacity: 0.55;
  cursor: not-allowed;
}
.btn.mini {
  padding: 3px 8px;
  font-size: 12px;
}
.btn.ghost {
  border-color: transparent;
  color: #4b5563;
}

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
.badge {
  padding: 1px 7px;
  border-radius: 10px;
  font-size: 11px;
}
.badge.ok {
  background: #ecfdf5;
  color: #047857;
}
.badge.warn {
  background: #fffbeb;
  color: #b45309;
}

/* 任务节点 */
.task-node {
  padding-bottom: 6px;
}
.task-node.broken {
  border-color: #fbbf24;
}

/* 家族模块节点 */
.module-node {
  border-left: 3px solid #818cf8;
}
.module-node.broken {
  border-left-color: #f59e0b;
}
.module-head h4 {
  flex: 1;
}
.expand-btn {
  flex: 0 0 auto;
}
.module-members {
  border-top: 1px dashed #eef0f3;
  padding: 4px 8px 6px;
}
.member-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  padding: 6px 2px;
  border-bottom: 1px solid #f6f7f9;
}
.member-row:last-child {
  border-bottom: none;
}
.member-row.disabled {
  opacity: 0.6;
}
.member-main {
  display: flex;
  flex-direction: column;
  min-width: 0;
}
.member-main b {
  font-size: 12px;
}
.member-sched {
  font-size: 11px;
  color: #9ca3af;
}
.member-actions {
  display: flex;
  align-items: center;
  gap: 6px;
  flex: 0 0 auto;
}

/* 开关 */
.switch {
  width: 34px;
  height: 18px;
  border: 1px solid #d1d5db;
  border-radius: 9px;
  background: #e5e7eb;
  cursor: pointer;
  position: relative;
  padding: 0;
}
.switch i {
  position: absolute;
  left: 2px;
  top: 2px;
  width: 12px;
  height: 12px;
  border-radius: 50%;
  background: #fff;
  transition: left 0.15s ease;
  box-shadow: 0 1px 2px rgba(0, 0, 0, 0.2);
}
.switch.on {
  background: #10b981;
  border-color: #10b981;
}
.switch.on i {
  left: 18px;
}

/* 端口 */
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
}
.port.module-out {
  background: #8b5cf6;
  cursor: crosshair;
}
.port.module-out.busy {
  opacity: 0.4;
  pointer-events: none;
}
.port.in {
  left: -6px;
  top: 35px;
}
.port.module-in {
  background: #8b5cf6;
  cursor: crosshair;
}
</style>
