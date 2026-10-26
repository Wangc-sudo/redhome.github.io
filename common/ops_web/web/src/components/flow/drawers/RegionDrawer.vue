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
  getRegionCatalog,
  saveRegionBroadcast,
  saveRegionDepts,
  saveRegionTargets,
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

// -- 播报配置与归属映射（2026-10-26 权威源迁 DB 的页面入口）-------------------
const broadcast = ref(null);      // 该区域播报配置可编辑副本
const deptIdsText = ref('');      // 归属映射（逗号分隔部门 ID）
const catalogLoaded = ref(false);

function deriveCatalog(catalog) {
  const region = ((catalog && catalog.regions) || [])
    .find((row) => row.region_key === props.node.key);
  broadcast.value = region && region.broadcast
    ? { ...region.broadcast } : null;
  const ids = ((catalog && catalog.region_depts) || {})[props.node.key] || [];
  deptIdsText.value = ids.join(', ');
  catalogLoaded.value = true;
}

async function loadCatalog() {
  try {
    deriveCatalog(await getRegionCatalog());
  } catch (err) {
    setFlash('err', `目录读取失败：${err.message}`);
  }
}
watch(() => props.node && props.node.key, () => {
  catalogLoaded.value = false;
  loadCatalog();
}, { immediate: true });

async function onSaveBroadcast() {
  if (!broadcast.value) return;
  const fields = {};
  for (const key of ['robot_code', 'open_conversation_id', 'table_url',
                     'leaderboard_url']) {
    const value = (broadcast.value[key] || '').trim();
    fields[key] = value || null;   // 空串=清空该列
  }
  for (const key of ['remind_hour', 'check_hour']) {
    const value = broadcast.value[key];
    fields[key] = value === null || value === '' ? null : Number(value);
  }
  for (const key of ['broadcast_exclude', 'dept_order']) {
    const raw = broadcast.value[key];
    const list = Array.isArray(raw) ? raw
      : String(raw || '').split(/[,，]/).map((s) => s.trim()).filter(Boolean);
    fields[key] = list.length ? list : null;
  }
  emit('update:busy', true);
  try {
    await saveRegionBroadcast({ region_key: props.node.key, fields });
    setFlash('ok', `已保存 ${props.node.label} 播报配置`);
    await props.reload();
  } catch (err) {
    setFlash('err', `保存播报配置失败：${err.message}`);
  } finally {
    emit('update:busy', false);
  }
}

async function onSaveDepts() {
  const ids = deptIdsText.value.split(/[,，\s]+/)
    .map((s) => s.trim()).filter(Boolean).map(Number);
  if (ids.some((d) => !Number.isInteger(d) || d <= 0)) {
    setFlash('err', '部门 ID 必须是正整数（逗号分隔）');
    return;
  }
  emit('update:busy', true);
  try {
    const result = await saveRegionDepts({
      region_key: props.node.key, dept_ids: ids,
    });
    setFlash('ok', `已保存归属映射（+${result.added.length} / -${result.removed.length}）`);
    await props.reload();
  } catch (err) {
    setFlash('err', `保存归属映射失败：${err.message}`);
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

  <div v-if="broadcast" class="drawer-sec">
    <h3>播报配置</h3>
    <div class="target-row">
      <span class="roster-name">群 conversationId</span>
      <input v-model="broadcast.open_conversation_id" class="roster-input">
    </div>
    <div class="target-row">
      <span class="roster-name">机器人 robotCode</span>
      <input v-model="broadcast.robot_code" class="roster-input">
    </div>
    <div class="target-row">
      <span class="roster-name">日报表 URL</span>
      <input v-model="broadcast.table_url" class="roster-input">
    </div>
    <div class="target-row">
      <span class="roster-name">榜单页</span>
      <input v-model="broadcast.leaderboard_url" class="roster-input">
    </div>
    <div class="target-row">
      <span class="roster-name">提醒时点</span>
      <input v-model="broadcast.remind_hour" type="number" min="0" max="23"
             class="target-input">
      <span class="roster-name">催办时点</span>
      <input v-model="broadcast.check_hour" type="number" min="0" max="23"
             class="target-input">
    </div>
    <div class="target-row">
      <span class="roster-name">榜单排除</span>
      <input v-model="broadcast.broadcast_exclude" class="roster-input"
             placeholder="逗号分隔人名">
    </div>
    <div class="target-row">
      <span class="roster-name">榜单分桶</span>
      <input v-model="broadcast.dept_order" class="roster-input"
             placeholder="逗号分隔，如：杭中, 滨萧, 余杭">
    </div>
    <button class="btn mini primary" :disabled="busy" @click="onSaveBroadcast">
      保存播报配置
    </button>
    <p class="drawer-line muted">
      保存即写 dim_org_region（播报读序 DB 优先）；空字段=清空该列。
    </p>
  </div>

  <div v-if="catalogLoaded" class="drawer-sec">
    <h3>归属部门（采集范围）</h3>
    <div class="target-row">
      <input v-model="deptIdsText" class="roster-input"
             placeholder="钉钉部门 ID，逗号分隔">
    </div>
    <button class="btn mini primary" :disabled="busy" @click="onSaveDepts">
      保存归属映射
    </button>
    <p class="drawer-line muted">
      保存即写 dim_org_region_dept（采集范围 DB 优先）；清空=回落 seed
      兜底。该区域的子部门自动纳入，无需逐层登记。
    </p>
  </div>
</template>
