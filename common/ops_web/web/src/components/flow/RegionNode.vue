<script setup>
/**
 * 区域节点（2026-10-17 F3 从 FlowPage.vue 抽出）：聚合=圆形、业务=矩形。
 * 部门槽（点击部门行弹下拉选人归口/调部门，2026-10-26）+ 任务方块
 * （指标格同款，点击折叠/展开该区域任务模块）。纯展示+事件上抛。
 * 2026-10-26 用户裁决：成员展开行退役（「毫无作用」）——成员明细与
 * 搬迁去 /org 组织编排页，画布不再内嵌。
 */
import { fmt } from '../../flow/graph.js';
import { AGG_SIZE, REGION_W } from '../../flow/layout.js';

defineProps({
  node: { type: Object, required: true },
  anchorHit: { type: Boolean, default: false },
  tasksCollapsed: { type: Boolean, default: false },
  busy: { type: Boolean, default: false },
});
const emit = defineEmits(['drag', 'wire-start', 'toggle-tasks',
  'open-drawer', 'dept-assign']);
</script>

<template>
  <article
    :id="`flow-${node.key}`"
    class="node region-node"
    :class="{ 'anchor-hit': anchorHit, 'agg-node': node.isAggregate }"
    :style="node.isAggregate
      ? { left: node.x + 'px', top: node.y + 'px', width: AGG_SIZE + 'px', height: AGG_SIZE + 'px' }
      : { left: node.x + 'px', top: node.y + 'px', width: REGION_W + 'px' }"
  >
    <span v-if="!node.isAggregate" class="port in-region" :data-region-in="node.key" title="成员归口输入" />
    <span v-else class="port in-region agg-in" title="汇聚输入（组成桶汇入）" />
    <!-- 聚合节点：圆形内容居中 -->
    <template v-if="node.isAggregate">
      <div class="agg-body" @mousedown="emit('drag', $event, node)">
        <h3>{{ node.label }}</h3>
        <span class="badge agg">{{ node.badge.text }}</span>
        <div class="agg-metrics">
          <b>{{ node.rosterCount }}</b><span>名册</span>
          <b>{{ fmt(node.targetSum) }}</b><span>Σ 万元</span>
          <b v-if="node.completionRate != null">
            {{ (node.completionRate * 100).toFixed(1) }}%
          </b><span v-if="node.completionRate != null">完成率</span>
        </div>
        <button class="btn mini ghost" @mousedown.stop @click="emit('open-drawer', node)">详情</button>
      </div>
    </template>
    <template v-else>
      <div class="node-head" title="拖动移动节点" @mousedown="emit('drag', $event, node)">
        <h3>{{ node.label }}</h3>
        <span class="badge" :class="node.badge.cls">{{ node.badge.text }}</span>
      </div>
      <div class="metrics" v-if="!node.isPlatform">
        <span class="metric"><b>{{ node.rosterCount }}</b><span>名册</span></span>
        <span class="metric"><b>{{ node.targetCount }}</b><span>目标人</span></span>
        <span class="metric"><b>{{ fmt(node.targetSum) }}</b><span>Σ 万元</span></span>
        <!-- 完成率（2026-10-26 用户裁决「播报回显完成百分比添加到面板」）：
             本月累计/月目标，与播报回执同口径；目标 0/无 →「—」；
             hq 职能底不报数不显示 -->
        <span class="metric" v-if="node.group !== 'hq'">
          <b>{{ node.completionRate == null ? '—' : (node.completionRate * 100).toFixed(1) + '%' }}</b>
          <span>完成</span>
        </span>
        <span class="metric"><b>{{ node.memberCount }}</b><span>成员</span></span>
      </div>
      <!-- 部门槽（2026-10-26：点击部门行弹下拉列表选人归口，替代拉线圆点） -->
      <div v-if="node.depts && node.depts.length" class="dept-slot">
        <div
          v-for="dept in node.depts"
          :key="dept.dept_id || dept.dept_name"
          class="dept-row clickable"
          :title="`点击选择要归入「${dept.dept_name}」的成员`"
          @click.stop="emit('dept-assign', node, dept, $event)"
        >
          <span class="dept-name">{{ dept.dept_name || '（无 ID 部门）' }}</span>
          <span class="dept-count">{{ dept.memberCount }}</span>
          <span class="dept-caret">▾</span>
        </div>
      </div>
      <div class="node-foot">
        <!-- 任务方块（2026-10-26 用户裁决「把任务做成方块」）：与指标格
             同款视觉，点击=折叠/展开该区域任务模块 -->
        <button
          v-if="node.taskCount"
          class="metric task-metric"
          :title="tasksCollapsed ? '展开该区域的任务模块' : '折叠该区域的任务模块（组内连线收起）'"
          @mousedown.stop
          @click="emit('toggle-tasks', node.key)"
        >
          <b>{{ node.taskCount }}</b><span>任务 {{ tasksCollapsed ? '▸' : '▾' }}</span>
        </button>
        <span v-else class="task-count">下游任务 0</span>
        <button class="btn mini ghost" @click="emit('open-drawer', node)">详情</button>
      </div>
    </template>
    <span class="port out" title="驱动下游任务" />
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
.node.anchor-hit {
  border-color: #2563eb;
  box-shadow: 0 0 0 3px rgba(37, 99, 235, 0.18);
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
.node-foot {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 2px 10px 8px;
  font-size: 11px;
  color: #9ca3af;
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
.badge.plain {
  background: #f3f4f6;
  color: #6b7280;
}
.badge.agg {
  background: #e0e7ff;
  color: #4338ca;
}
.metrics {
  display: flex;
  gap: 6px;
  padding: 4px 10px;
}
.metric {
  display: flex;
  flex-direction: column;
  align-items: center;
  min-width: 46px;   /* 2026-10-26 完成率格加入后五格并排收窄 */
  padding: 3px 4px;
  border: 1px solid #eef0f3;
  border-radius: 6px;
  background: #f9fafb;
  font-size: 11px;
}
.metric b {
  font-size: 14px;
}
.metric span {
  color: #6b7280;
}

/* 聚合节点（圆形）：与矩形业务节点视觉分层 */
.node.agg-node {
  border-radius: 50%;
  border: 2px solid #6366f1;
  background: radial-gradient(circle at 35% 30%, #eef2ff, #e0e7ff);
  box-shadow: 0 2px 8px rgba(99, 102, 241, 0.18);
}
.agg-body {
  height: 100%;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 4px;
  cursor: move;
  user-select: none;
  text-align: center;
}
.agg-body h3 {
  margin: 0;
  font-size: 16px;
}
.agg-metrics {
  display: flex;
  flex-direction: column;
  font-size: 11px;
  color: #4b5563;
}
.agg-metrics b {
  font-size: 15px;
  color: #1f2933;
}

/* 部门槽（输入端口） */
.dept-slot {
  margin: 2px 10px 4px;
  padding: 4px 0;
  border-top: 1px dashed #eef0f3;
}
.dept-row {
  position: relative;
  display: flex;
  align-items: center;
  gap: 8px;
  height: 26px;
  padding-left: 14px;
  font-size: 12px;
}
.dept-row.clickable {
  cursor: pointer;
  border-radius: 4px;
}
.dept-row.clickable:hover {
  background: #f1f5ff;
}
.dept-row.clickable:hover .dept-caret {
  color: #2563eb;
}
.dept-name {
  color: #374151;
}
.dept-count {
  margin-left: auto;
  color: #9ca3af;
  font-size: 11px;
}
.dept-caret {
  color: #d1d5db;
  font-size: 10px;
}

/* 2026-10-26：成员展开行（member-slot/member-row-wire/member-port）
   随「毫无作用」裁决退役——成员明细与搬迁去 /org 组织编排页 */

/* 任务方块（foot 行，与指标格同款视觉） */
.task-metric {
  cursor: pointer;
}
.task-metric:hover {
  border-color: #c7d2fe;
  background: #eef2ff;
}
.task-metric span {
  color: #6b7280;
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
  background: #10b981;
}
.port.in-region {
  left: -6px;
  top: 50%;
  margin-top: -5px;
  background: #2563eb;
  cursor: crosshair;
}
.port.agg-in {
  background: #6366f1;
}
/* 2026-10-26：.port.in-dept（部门拉线圆点）随点击下拉归口上线退役 */
</style>
