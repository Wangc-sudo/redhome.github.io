<script setup>
/**
 * 任务抽屉（2026-10-16 F2 从 FlowPage.vue 抽出）：播报时间编辑 + 最近运行
 * + 依赖展示 + 收件人覆盖（P3 吃收件人总览页的单线微调）。
 * 写操作编排与旧实现一致：setFlash 直用全局 store；保存时间=关抽屉+全量刷新；
 * 收件人增删改=局部重读（loadRcpt），不动画布。
 */
import { onMounted, ref } from 'vue';
import {
  addRecipientOverride,
  deleteRecipientOverride,
  getRecipientOverride,
  toggleRecipientOverride,
  updatePipelineTimes,
} from '../../../api.js';
import { setFlash } from '../../../store/me.js';

const props = defineProps({
  node: { type: Object, required: true },    // 模块成员行 {service, ...}
  busy: { type: Boolean, default: false },
  reload: { type: Function, required: true },
});
const emit = defineEmits(['close', 'update:busy']);

// -- 播报时间 -------------------------------------------------------------------
const drawerTimes = ref([]);   // [{hour, minute}]

function parseTimes() {
  const zh = props.node.service.schedule_zh || '';
  // 从 schedule_zh（每天 HH:MM / 工作日 HH:MM）解析出时间点列表；解析不出给空
  const times = [];
  for (const m of zh.matchAll(/(\d{1,2}):(\d{2})/g)) {
    times.push({ hour: Number(m[1]), minute: Number(m[2]) });
  }
  drawerTimes.value = times.length ? times : [{ hour: 8, minute: 30 }];
}

function addTimeRow() {
  drawerTimes.value.push({ hour: 12, minute: 0 });
}
function removeTimeRow(index) {
  drawerTimes.value.splice(index, 1);
}

async function onSaveTimes() {
  const times = drawerTimes.value
    .filter((t) => Number.isInteger(t.hour) && Number.isInteger(t.minute));
  if (!times.length) {
    setFlash('err', '至少保留一个播报时间');
    return;
  }
  emit('update:busy', true);
  try {
    await updatePipelineTimes({ service_id: props.node.service.service_id, times });
    setFlash('ok', `已改「${props.node.service.service_label}」播报时间`);
    emit('close');
    await props.reload();
  } catch (err) {
    setFlash('err', `改播报时间失败：${err.message}`);
  } finally {
    emit('update:busy', false);
  }
}

// -- 收件人覆盖 -----------------------------------------------------------------
const RCPT_MODE = { include: '追加', exclude: '移除' };
const RCPT_TYPE = { user_id: '账号', name: '姓名' };
const rcpt = ref({ loading: false, base: [], rows: [], explain: [] });
const rcptForm = ref({ mode: 'include', target_type: 'user_id', target_value: '', note: '' });

async function loadRcpt() {
  rcpt.value.loading = true;
  try {
    const data = await getRecipientOverride(props.node.service.service_id);
    rcpt.value = {
      loading: false,
      base: (data && data.base) || [],
      rows: (data && data.overrides) || [],
      explain: (data && data.explain) || [],
    };
  } catch (err) {
    rcpt.value = { loading: false, base: [], rows: [], explain: [] };
    setFlash('err', `收件人读取失败：${err.message}`);
  }
}

async function onRcptAdd() {
  const value = rcptForm.value.target_value.trim();
  if (!value) return;
  emit('update:busy', true);
  try {
    await addRecipientOverride({
      service_id: props.node.service.service_id,
      mode: rcptForm.value.mode,
      target_type: rcptForm.value.target_type,
      target_value: value,
      note: rcptForm.value.note.trim(),
    });
    setFlash('ok', '收件人覆盖已添加');
    rcptForm.value.target_value = '';
    rcptForm.value.note = '';
    await loadRcpt();
  } catch (err) {
    setFlash('err', `添加失败：${err.message}`);
  } finally {
    emit('update:busy', false);
  }
}

async function onRcptToggle(row) {
  emit('update:busy', true);
  try {
    await toggleRecipientOverride({ id: row.id, enabled: !Number(row.enabled) });
    await loadRcpt();
  } catch (err) {
    setFlash('err', `操作失败：${err.message}`);
  } finally {
    emit('update:busy', false);
  }
}

async function onRcptDelete(row) {
  if (!window.confirm(`删除覆盖「${RCPT_MODE[row.mode]} ${row.target_value}」？`)) return;
  emit('update:busy', true);
  try {
    await deleteRecipientOverride({ id: row.id });
    await loadRcpt();
  } catch (err) {
    setFlash('err', `删除失败：${err.message}`);
  } finally {
    emit('update:busy', false);
  }
}

onMounted(() => {
  parseTimes();
  loadRcpt();
});
</script>

<template>
  <h2>{{ node.service.service_label }}</h2>
  <p class="drawer-sub">{{ node.service.service_id }}</p>
  <div class="drawer-sec">
    <h3>播报时间</h3>
    <div
      v-for="(t, index) in drawerTimes"
      :key="index"
      class="time-row"
    >
      <input v-model.number="t.hour" type="number" min="0" max="23"> 时
      <input v-model.number="t.minute" type="number" min="0" max="59"> 分
      <button
        class="btn mini ghost"
        :disabled="drawerTimes.length <= 1"
        @click="removeTimeRow(index)"
      >删</button>
    </div>
    <button class="btn mini" @click="addTimeRow">+ 加一个时点</button>
    <button class="btn mini primary" :disabled="busy" @click="onSaveTimes">保存时间</button>
  </div>
  <div class="drawer-sec">
    <h3>最近运行</h3>
    <p v-if="node.service.latest" class="drawer-line">
      {{ node.service.latest.status_label }} ·
      {{ node.service.latest.created_at }} ·
      {{ node.service.latest.requested_by }}
    </p>
    <p v-else class="drawer-line muted">无运行记录</p>
    <router-link class="drawer-link" to="/pipelines/history" @click="emit('close')">
      完整执行流水 →
    </router-link>
  </div>
  <div class="drawer-sec" v-if="node.service.depends_on.length">
    <h3>依赖</h3>
    <p class="drawer-line">{{ node.service.depends_on.join('、') }}</p>
  </div>
  <div class="drawer-sec">
    <h3>收件人覆盖</h3>
    <p class="drawer-line muted" v-if="rcpt.loading">读取中…</p>
    <template v-else>
      <p class="drawer-line muted">
        基名单 {{ rcpt.base.length }} 人（{{ rcpt.base.join('、') || '—' }}）
      </p>
      <div v-for="o in rcpt.rows" :key="o.id" class="roster-row">
        <span class="rcpt-mode" :class="o.mode">{{ RCPT_MODE[o.mode] || o.mode }}</span>
        <span class="roster-name">{{ o.target_value }}</span>
        <span class="roster-role">{{ RCPT_TYPE[o.target_type] || o.target_type }}</span>
        <button
          class="switch"
          :class="{ on: Number(o.enabled) }"
          :disabled="busy"
          @click="onRcptToggle(o)"
        ><i /></button>
        <button
          class="btn mini ghost"
          :disabled="busy"
          @click="onRcptDelete(o)"
        >删</button>
      </div>
      <p v-if="!rcpt.rows.length" class="drawer-line muted">
        暂无覆盖（按名册口径发送）。
      </p>
      <div class="roster-add">
        <select v-model="rcptForm.mode" class="roster-select">
          <option value="include">追加</option>
          <option value="exclude">移除</option>
        </select>
        <select v-model="rcptForm.target_type" class="roster-select">
          <option value="user_id">账号</option>
          <option value="name">姓名</option>
        </select>
        <input v-model="rcptForm.target_value" placeholder="账号或姓名" class="roster-input">
        <button
          class="btn mini"
          :disabled="busy"
          @click="onRcptAdd"
        >添加</button>
      </div>
    </template>
  </div>
</template>
