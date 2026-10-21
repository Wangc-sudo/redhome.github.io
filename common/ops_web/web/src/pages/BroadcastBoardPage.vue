<script setup>
/**
 * 播报编排（/broadcast）——任务 × 区域 拉线矩阵（2026-10-21 播报编排大模块）。
 *
 * 读面 GET /api/org/broadcast-board → {links, regions}（注册表为真源，前端即后端）。
 * 行=播报任务类型，列=区域；格=该任务在该区域的播报链接（启用 / 播报时间）。
 * 谁链到任务，谁就收到那个播报。启停 POST /api/pipelines/toggle；
 * 改时间 POST /api/pipelines/update（日级 HH:MM；周/月级 cron 到定时任务页改）。
 * 部门级拉线预留（暂不开放）。
 */
import { computed, onMounted, ref } from 'vue';
import {
  getBroadcastBoard,
  togglePipeline,
  updatePipelineTimes,
} from '../api.js';
import { setFlash } from '../store/me.js';

const links = ref([]);
const regions = ref([]);
const loading = ref(true);
const error = ref('');
const busy = ref(false);

//: 任务类型行轴（保持后端 category 排序：催办在前、播报在后）。
const taskTypes = computed(() => {
  const seen = [];
  for (const l of links.value) {
    if (!seen.includes(l.task_type)) seen.push(l.task_type);
  }
  return seen;
});

//: (task_type, region) → link
const cellMap = computed(() => {
  const m = {};
  for (const l of links.value) m[`${l.task_type}@@${l.region}`] = l;
  return m;
});

function cell(taskType, regionKey) {
  return cellMap.value[`${taskType}@@${regionKey}`] || null;
}

function scheduleText(link) {
  const s = link.schedule || '';
  const p = s.split(' ');
  if (p.length === 5 && p[2] === '*' && p[3] === '*' && p[4] === '*') {
    return `每天 ${p[1].padStart(2, '0')}:${p[0].padStart(2, '0')}`;
  }
  return s || '常驻';
}

async function load() {
  loading.value = true;
  error.value = '';
  try {
    const data = await getBroadcastBoard();
    links.value = (data && data.links) || [];
    regions.value = (data && data.regions) || [];
  } catch (err) {
    error.value = err.message || '加载失败';
  } finally {
    loading.value = false;
  }
}

async function toggleLink(link) {
  busy.value = true;
  try {
    await togglePipeline({ service_id: link.service_id, enabled: !link.enabled });
    await load();
    setFlash('ok', `${link.label} 已${link.enabled ? '停用' : '启用'}`);
  } catch (err) {
    setFlash('err', err.message || '操作失败');
  } finally {
    busy.value = false;
  }
}

//: 改播报时间（分/时；保留 cron 的 日/月/周 段）。
const editCell = ref(null);
const editHour = ref('20');
const editMinute = ref('30');

function openEdit(link) {
  editCell.value = link;
  const p = (link.schedule || '0 20 * * *').split(' ');
  editMinute.value = p[0] || '0';
  editHour.value = p[1] || '20';
}

async function submitEdit() {
  const link = editCell.value;
  if (!link) return;
  busy.value = true;
  try {
    await updatePipelineTimes({
      service_id: link.service_id,
      times: [{
        hour: parseInt(editHour.value, 10),
        minute: parseInt(editMinute.value, 10),
      }],
    });
    editCell.value = null;
    await load();
    setFlash('ok', '已改播报时间');
  } catch (err) {
    setFlash('err', err.message || '保存失败');
  } finally {
    busy.value = false;
  }
}

const hours = Array.from({ length: 24 }, (_, i) => String(i));
const minutes = Array.from({ length: 60 }, (_, i) => String(i));

onMounted(load);
</script>

<template>
  <div class="page">
    <div class="head">
      <h1>播报编排</h1>
      <p class="note">
        任务 × 区域 拉线板：格子 = 该任务在该区域的播报链接（启用 / 播报时间）。
        谁链到任务，谁就收到那个播报。部门级拉线预留（暂不开放）。
      </p>
    </div>

    <div v-if="loading" class="state">加载中…</div>
    <div v-else-if="error" class="state error">
      <p>加载失败：{{ error }}</p>
      <button class="btn" @click="load()">重试</button>
    </div>

    <div v-else class="board-wrap">
      <table class="board">
        <thead>
          <tr>
            <th class="task-col">播报任务</th>
            <th v-for="r in regions" :key="r.key">{{ r.label }}</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="t in taskTypes" :key="t">
            <td class="task-col">{{ t }}</td>
            <td v-for="r in regions" :key="r.key" class="cell">
              <div
                v-if="cell(t, r.key)"
                class="link"
                :class="{ off: !cell(t, r.key).enabled }"
              >
                <div class="link-top">
                  <span class="dot" :class="{ on: cell(t, r.key).enabled }" />
                  <span class="sched">{{ scheduleText(cell(t, r.key)) }}</span>
                </div>
                <div class="ops">
                  <button
                    class="btn mini"
                    :disabled="busy"
                    @click="toggleLink(cell(t, r.key))"
                  >
                    {{ cell(t, r.key).enabled ? '停用' : '启用' }}
                  </button>
                  <button
                    class="btn mini ghost"
                    :disabled="busy"
                    @click="openEdit(cell(t, r.key))"
                  >
                    改时间
                  </button>
                </div>
              </div>
              <span v-else class="dash">—</span>
            </td>
          </tr>
        </tbody>
      </table>
      <p v-if="!taskTypes.length" class="empty">暂无播报任务。</p>
    </div>

    <!-- 改播报时间 -->
    <div v-if="editCell" class="modal-mask" @click.self="editCell = null">
      <div class="modal">
        <h3 class="modal-title">改播报时间 — {{ editCell.label }}</h3>
        <div class="time-form">
          <select v-model="editHour" class="input">
            <option v-for="h in hours" :key="h" :value="h">
              {{ h.padStart(2, '0') }} 时
            </option>
          </select>
          <select v-model="editMinute" class="input">
            <option v-for="m in minutes" :key="m" :value="m">
              {{ m.padStart(2, '0') }} 分
            </option>
          </select>
        </div>
        <div class="modal-ops">
          <button class="btn primary" :disabled="busy" @click="submitEdit">
            确定
          </button>
          <button class="btn" @click="editCell = null">取消</button>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.page {
  padding: 18px;
}
.head h1 {
  margin: 0 0 4px;
  font-size: 20px;
}
.note {
  color: #6b7280;
  font-size: 13px;
  margin: 0;
}
.state {
  padding: 40px;
  text-align: center;
  color: #6b7280;
}
.state.error {
  color: #b91c1c;
}
.board-wrap {
  overflow-x: auto;
  margin-top: 14px;
}
.board {
  border-collapse: collapse;
  width: 100%;
  font-size: 13px;
}
.board th,
.board td {
  border: 1px solid #e5e7eb;
  padding: 8px;
  vertical-align: top;
  text-align: center;
}
.board th {
  background: #f9fafb;
  font-weight: 600;
}
.task-col {
  text-align: left;
  font-weight: 600;
  white-space: nowrap;
  background: #f9fafb;
}
.cell {
  min-width: 120px;
}
.link {
  display: flex;
  flex-direction: column;
  gap: 6px;
  align-items: center;
}
.link.off {
  opacity: 0.5;
}
.link-top {
  display: flex;
  align-items: center;
  gap: 6px;
}
.dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: #d1d5db;
}
.dot.on {
  background: #16a34a;
}
.sched {
  font-size: 12px;
  color: #374151;
}
.ops {
  display: flex;
  gap: 4px;
}
.dash {
  color: #d1d5db;
}
.empty {
  padding: 30px;
  text-align: center;
  color: #6b7280;
}
.btn {
  padding: 6px 12px;
  border: 1px solid #d0d7de;
  border-radius: 6px;
  background: #fff;
  cursor: pointer;
  font-size: 13px;
}
.btn.mini {
  padding: 3px 8px;
  font-size: 12px;
}
.btn.primary {
  background: #2563eb;
  color: #fff;
  border-color: #2563eb;
}
.btn.ghost {
  background: transparent;
}
.btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}
.modal-mask {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.4);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 50;
}
.modal {
  background: #fff;
  border-radius: 10px;
  padding: 20px;
  min-width: 320px;
}
.modal-title {
  margin: 0 0 14px;
  font-size: 16px;
}
.time-form {
  display: flex;
  gap: 8px;
  margin-bottom: 16px;
}
.input {
  padding: 6px 8px;
  border: 1px solid #d0d7de;
  border-radius: 6px;
}
.modal-ops {
  display: flex;
  gap: 8px;
  justify-content: flex-end;
}
</style>
