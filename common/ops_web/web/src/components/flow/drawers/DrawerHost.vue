<script setup>
/**
 * 抽屉宿主（2026-10-16 F2）：按 drawer.kind 动态分发到具体抽屉组件。
 * 统一渲染壳（aside + 关闭钮 + 遮罩 + 滑入动效）；kind/node 变化时
 * 以 :key 强制重挂载，抽屉内部状态随之重置（等价旧 openXxxDrawer 的初始化）。
 */
import { computed } from 'vue';
import TaskDrawer from './TaskDrawer.vue';
import RegionDrawer from './RegionDrawer.vue';
import CalendarDrawer from './CalendarDrawer.vue';
import RecordsDrawer from './RecordsDrawer.vue';
import GrantsDrawer from './GrantsDrawer.vue';
import DataFixDrawer from './DataFixDrawer.vue';
import GovernanceDrawer from './GovernanceDrawer.vue';
import RelayDrawer from './RelayDrawer.vue';
import './drawer.css';

const props = defineProps({
  drawer: { type: Object, default: null },   // {kind, node?}
  busy: { type: Boolean, default: false },
  month: { type: String, default: '' },      // 目标月份（区域抽屉副标题）
  roster: { type: Object, default: null },   // lastRaw.roster（区域抽屉数据源）
  regionNodes: { type: Array, default: () => [] },  // 授权/修数抽屉的区域选项
  groups: { type: Array, default: () => [] },       // 任务组（治理抽屉数据源）
  reload: { type: Function, required: true },       // () => Promise 静默全量刷新
});

const emit = defineEmits(['close', 'update:busy']);

const KIND_COMPONENT = {
  task: TaskDrawer,
  region: RegionDrawer,
  calendar: CalendarDrawer,
  records: RecordsDrawer,
  grants: GrantsDrawer,
  datafix: DataFixDrawer,
  governance: GovernanceDrawer,
  relay: RelayDrawer,
};

const comp = computed(() =>
  (props.drawer ? KIND_COMPONENT[props.drawer.kind] : null) || null);
const compKey = computed(() => (props.drawer
  ? `${props.drawer.kind}:${props.drawer.node ? props.drawer.node.id : '-'}`
  : ''));
</script>

<template>
  <transition name="flowdrawer">
    <aside v-if="drawer" class="flow-drawer">
      <component
        :is="comp"
        v-if="comp"
        :key="compKey"
        :node="drawer.node"
        :busy="busy"
        :month="month"
        :roster="roster"
        :region-nodes="regionNodes"
        :groups="groups"
        :reload="reload"
        @close="emit('close')"
        @update:busy="emit('update:busy', $event)"
      />
      <button class="drawer-close" @click="emit('close')">✕</button>
    </aside>
  </transition>
  <div v-if="drawer" class="flow-drawer-mask" @click="emit('close')" />
</template>
