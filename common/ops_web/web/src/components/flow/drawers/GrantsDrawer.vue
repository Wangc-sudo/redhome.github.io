<script setup>
/**
 * 授权抽屉（2026-10-16 F2 从 FlowPage.vue 抽出，P4，admin 专属）：
 * 能力×区域授权的新增/删除/列表。区域选项来自画布区域节点（prop 传入）。
 */
import { onMounted, ref } from 'vue';
import { addGrant, deleteGrant, getGrants } from '../../../api.js';
import { setFlash } from '../../../store/me.js';

const props = defineProps({
  regionNodes: { type: Array, default: () => [] },
  busy: { type: Boolean, default: false },
});
const emit = defineEmits(['close', 'update:busy']);

const GRANT_CAPS = [
  { key: 'roster', label: '名册与目标' },
  { key: 'org', label: '组织编排' },
  { key: 'pipelines', label: '定时任务' },
  { key: 'audit', label: '操作记录' },
  { key: 'calendar', label: '播报日历' },
  { key: 'datafix', label: '数据修正' },
];
const grantRows = ref([]);
const grantForm = ref({
  user_id: '', grant_type: 'scope', cap: 'roster', region: '', note: '',
});

async function loadGrants() {
  emit('update:busy', true);
  try {
    const data = await getGrants();
    grantRows.value = (data && data.grants) || [];
  } catch (err) {
    setFlash('err', `授权读取失败：${err.message}`);
  } finally {
    emit('update:busy', false);
  }
}

function grantKeyOf(f) {
  if (f.grant_type === 'admin') return '-';
  if (f.grant_type === 'region') return f.region;
  return f.region ? `${f.cap}:${f.region}` : f.cap;
}

async function onGrantAdd() {
  const f = grantForm.value;
  if (!f.user_id.trim()) {
    setFlash('err', '请填钉钉 userid');
    return;
  }
  if (f.grant_type !== 'admin' && !grantKeyOf(f)) {
    setFlash('err', '请选择能力/区域');
    return;
  }
  emit('update:busy', true);
  try {
    await addGrant({
      user_id: f.user_id.trim(),
      grant_type: f.grant_type,
      grant_key: grantKeyOf(f),
      note: f.note.trim(),
    });
    setFlash('ok', '授权已添加（生效有分钟级延迟）');
    grantForm.value.user_id = '';
    grantForm.value.note = '';
    await loadGrants();
  } catch (err) {
    setFlash('err', `添加失败：${err.message}`);
  } finally {
    emit('update:busy', false);
  }
}

async function onGrantDelete(row) {
  if (!window.confirm(
    `删除授权？\n${row.user_id} · ${row.grant_type_label} · ${row.grant_key}`,
  )) return;
  emit('update:busy', true);
  try {
    await deleteGrant({
      user_id: row.user_id,
      grant_type: row.grant_type,
      grant_key: row.grant_key,
    });
    setFlash('ok', '已删除授权');
    await loadGrants();
  } catch (err) {
    setFlash('err', `删除失败：${err.message}`);
  } finally {
    emit('update:busy', false);
  }
}

onMounted(loadGrants);
</script>

<template>
  <h2>授权</h2>
  <p class="drawer-sub">能力×区域授权（admin 专属；生效有分钟级延迟）</p>
  <div class="drawer-sec">
    <h3>新增授权</h3>
    <div class="roster-add">
      <input v-model="grantForm.user_id" placeholder="钉钉 userid" class="roster-input">
      <select v-model="grantForm.grant_type" class="roster-select">
        <option value="scope">能力</option>
        <option value="region">看板区域</option>
        <option value="admin">管理员</option>
      </select>
    </div>
    <div class="roster-add" v-if="grantForm.grant_type === 'scope'">
      <select v-model="grantForm.cap" class="roster-select">
        <option v-for="c in GRANT_CAPS" :key="c.key" :value="c.key">{{ c.label }}</option>
      </select>
      <select v-model="grantForm.region" class="roster-select">
        <option value="">全局</option>
        <option v-for="n in props.regionNodes" :key="n.key" :value="n.key">{{ n.label }}</option>
      </select>
    </div>
    <div class="roster-add" v-else-if="grantForm.grant_type === 'region'">
      <select v-model="grantForm.region" class="roster-select">
        <option value="">— 选择区域 —</option>
        <option v-for="n in props.regionNodes" :key="n.key" :value="n.key">{{ n.label }}</option>
      </select>
    </div>
    <div class="roster-add">
      <input v-model="grantForm.note" placeholder="备注（可空）" class="roster-input">
      <button class="btn mini primary" :disabled="busy" @click="onGrantAdd">添加</button>
    </div>
  </div>
  <div class="drawer-sec">
    <h3>授权现状（{{ grantRows.length }}）</h3>
    <div v-for="(row, i) in grantRows" :key="i" class="rec-row">
      <span class="rec-target">{{ row.user_id }}</span>
      <span class="rec-action">{{ row.grant_type_label }}</span>
      <span class="rec-source">{{ row.grant_key }}</span>
      <button class="btn mini ghost" :disabled="busy" @click="onGrantDelete(row)">删</button>
      <span class="rec-detail">{{ row.note }} · {{ row.created_at }}</span>
    </div>
    <p v-if="!grantRows.length" class="drawer-line muted">暂无授权记录</p>
  </div>
</template>
