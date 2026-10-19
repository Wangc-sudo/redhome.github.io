<script setup>
/**
 * 区域抽屉（2026-10-16 F2 从 FlowPage.vue 抽出，P2：名册+月目标编辑）。
 * 数据源=父级 lastRaw.roster（prop）；写操作后 await props.reload() 触发全量
 * 静默刷新，roster prop 引用变化 → watch 重新派生本地可编辑副本（并重置
 * 补录输入，等价旧 openRegionDrawer 的再打开语义）。
 */
import { ref, watch } from 'vue';
import {
  addRosterEntry,
  deleteRosterEntry,
  saveRegionTargets,
  setRegionRelay,
  toggleRosterEntry,
} from '../../../api.js';
import { setFlash } from '../../../store/me.js';

const props = defineProps({
  node: { type: Object, required: true },    // 区域节点
  roster: { type: Object, default: null },   // {roster: [...], targets: {...}}
  month: { type: String, default: '' },
  busy: { type: Boolean, default: false },
  reload: { type: Function, required: true },
});
const emit = defineEmits(['close', 'update:busy']);

const drawerRoster = ref([]);      // 该区域名册行（可编辑副本）
const drawerTargets = ref({});     // {person_name: 目标万元}
const drawerNewName = ref('');
const drawerNewRole = ref('owner');

function derive() {
  const roster = props.roster || {};
  drawerRoster.value = ((roster.roster) || [])
    .filter((row) => row.scope === props.node.key)
    .map((row) => ({ ...row }));
  drawerTargets.value = { ...((roster.targets || {})[props.node.key] || {}) };
  drawerNewName.value = '';
  drawerNewRole.value = 'owner';
}
watch(() => props.roster, derive, { immediate: true });

async function onToggleRosterRow(row) {
  emit('update:busy', true);
  try {
    await toggleRosterEntry({ id: row.id, enabled: !Number(row.enabled) });
    setFlash('ok', `已${Number(row.enabled) ? '停用' : '启用'}「${row.person_name}」`);
    await props.reload(['roster']);   // F4：名册/月目标只影响名册域
  } catch (err) {
    setFlash('err', `名册操作失败：${err.message}`);
  } finally {
    emit('update:busy', false);
  }
}

async function onDeleteRosterRow(row) {
  if (!window.confirm(`确认从名册删除「${row.person_name}」？（留审计）`)) return;
  emit('update:busy', true);
  try {
    await deleteRosterEntry({ id: row.id });
    setFlash('ok', `已删除名册项「${row.person_name}」`);
    await props.reload(['roster']);   // F4：名册/月目标只影响名册域
  } catch (err) {
    setFlash('err', `删除失败：${err.message}`);
  } finally {
    emit('update:busy', false);
  }
}

async function onAddRosterRow() {
  const name = drawerNewName.value.trim();
  if (!name) {
    setFlash('err', '请填姓名');
    return;
  }
  emit('update:busy', true);
  try {
    await addRosterEntry({
      scope: props.node.key,
      entity_type: 'person',
      entity_key: props.node.label,
      person_name: name,
      role: drawerNewRole.value,
      note: '',
    });
    setFlash('ok', `已把「${name}」补录到 ${props.node.label} 名册`);
    drawerNewName.value = '';
    await props.reload(['roster']);   // F4：名册/月目标只影响名册域
  } catch (err) {
    setFlash('err', `补录失败：${err.message}`);
  } finally {
    emit('update:busy', false);
  }
}

async function onToggleRelay() {
  // 播报改投测试区（2026-10-19）：开启后该区域群消息+DING 全部改投
  // 测试区群（带 [测试→原区域] 前缀），网关 30s 内生效，无需重启。
  const target = !props.node.relayTo;
  if (target && !window.confirm(
    `确认把「${props.node.label}」的全部播报改投测试区？`
    + '（群消息+DING 都改投，真群不再收到；随时可关）')) return;
  emit('update:busy', true);
  try {
    await setRegionRelay({ region_key: props.node.key, relay: target });
    setFlash('ok', target
      ? `「${props.node.label}」播报已改投测试区`
      : `「${props.node.label}」已恢复投真群`);
    await props.reload(['overview']);   // relay_to 挂在组织树域
  } catch (err) {
    setFlash('err', `改投开关失败：${err.message}`);
  } finally {
    emit('update:busy', false);
  }
}

async function onSaveTargets() {
  const rows = Object.entries(drawerTargets.value)
    .map(([person_name, value]) => ({
      person_name,
      monthly_target: Number(value) || 0,
    }));
  emit('update:busy', true);
  try {
    await saveRegionTargets({ scope: props.node.key, rows });
    setFlash('ok', `已保存 ${props.node.label} 月目标（${rows.length} 人）`);
    await props.reload(['roster']);   // F4：名册/月目标只影响名册域
  } catch (err) {
    setFlash('err', `保存目标失败：${err.message}`);
  } finally {
    emit('update:busy', false);
  }
}
</script>

<template>
  <h2>{{ node.label }}</h2>
  <p class="drawer-sub">
    {{ node.key }} · 成员 {{ node.memberCount }} ·
    下游任务 {{ node.taskCount }} · 目标月份 {{ month }}
  </p>
  <div class="drawer-sec">
    <h3>播报改投测试区</h3>
    <div class="roster-row">
      <span class="roster-name">群消息 + DING 全部改投测试区群</span>
      <button
        class="switch"
        :class="{ on: !!node.relayTo }"
        :disabled="busy"
        @click="onToggleRelay"
      ><i /></button>
    </div>
    <p class="drawer-line muted">
      {{ node.relayTo
        ? '已改投：消息带 [测试→' + node.label + '] 前缀发到测试区群'
        : '关闭中：播报投本区域群' }}
      （网关 30 秒内生效，幂等键不变不影响当日去重）
    </p>
  </div>
  <div class="drawer-sec">
    <h3>名册（{{ drawerRoster.length }}）</h3>
    <div v-for="row in drawerRoster" :key="row.id" class="roster-row">
      <span class="roster-name" :class="{ off: !Number(row.enabled) }">
        {{ row.person_name }}
      </span>
      <span class="roster-role">{{ row.role === 'deputy' ? '代填' : '负责' }}</span>
      <button
        class="switch"
        :class="{ on: Number(row.enabled) }"
        :disabled="busy"
        @click="onToggleRosterRow(row)"
      ><i /></button>
      <button
        class="btn mini ghost"
        :disabled="busy"
        @click="onDeleteRosterRow(row)"
      >删</button>
    </div>
    <div class="roster-add">
      <input v-model="drawerNewName" placeholder="姓名" class="roster-input">
      <select v-model="drawerNewRole" class="roster-select">
        <option value="owner">负责人</option>
        <option value="deputy">代填人</option>
      </select>
      <button class="btn mini" :disabled="busy" @click="onAddRosterRow">补录</button>
    </div>
  </div>
  <div class="drawer-sec">
    <h3>月目标（万元）</h3>
    <div
      v-for="row in drawerRoster.filter((r) => Number(r.enabled))"
      :key="'t' + row.id"
      class="target-row"
    >
      <span class="roster-name">{{ row.person_name }}</span>
      <input
        v-model="drawerTargets[row.person_name]"
        type="number"
        min="0"
        step="0.1"
        class="target-input"
      >
    </div>
    <button class="btn mini primary" :disabled="busy" @click="onSaveTargets">
      保存月目标
    </button>
    <p class="drawer-line muted">
      保存即写当月快照真源（dim_report_target）；发布快照/月份切换暂留名册页。
    </p>
  </div>
</template>
