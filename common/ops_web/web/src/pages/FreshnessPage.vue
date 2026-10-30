<script setup>
import { computed, onMounted, onUnmounted, reactive, ref } from 'vue';
import {
  checkDataFreshness,
  createDataQualityRerun,
  getDataFreshness,
  getDataFreshnessDetail,
  getDataQualityRerun,
  getKnowledgeIndexDetail,
  getKnowledgeIndexes,
  runPipelineOnce,
} from '../api.js';
import { hasCapability, me, setFlash } from '../store/me.js';
import DetailDrawer from '../components/ui/DetailDrawer.vue';
import StatusBadge from '../components/ui/StatusBadge.vue';

const activeView = ref('business');
const items = ref([]);
const deadLetters = ref([]);
const loading = ref(false);
const error = ref('');
const filters = reactive({ status: '', source: '', owner: '', dataset: '' });
const detail = ref(null);
const detailLoading = ref(false);
const detailError = ref('');
const checking = ref(false);
const running = ref(false);
const replaying = ref(false);
const replayPreview = ref(null);
const replayForm = reactive({ window_start: '', window_end: '', reason: '' });
const canCheck = computed(() => (
  me.value && me.value.userid !== 'anon-ops' && hasCapability('pipelines')
));
const sources = computed(() => [...new Set(items.value.map((item) => item.source || item.source_type).filter(Boolean))]);
const owners = computed(() => [...new Set(items.value.map((item) => item.owner).filter(Boolean))]);
const visibleItems = computed(() => {
  const keyword = filters.dataset.trim().toLowerCase();
  if (!keyword) return items.value;
  return items.value.filter((item) => (
    String(item.dataset_key || item.index_key || '').toLowerCase().includes(keyword)
  ));
});

async function load() {
  loading.value = true;
  error.value = '';
  try {
    const data = activeView.value === 'business'
      ? await getDataFreshness({ status: filters.status, source: filters.source, owner: filters.owner })
      : await getKnowledgeIndexes({ status: filters.status, source_type: filters.source });
    items.value = (data && data.items) || [];
    deadLetters.value = activeView.value === 'business' ? ((data && data.dead_letters) || []) : [];
  } catch (err) {
    items.value = [];
    deadLetters.value = [];
    error.value = err.status === 403 ? '无权限查看新鲜度信息' : (err.message || '新鲜度读取失败');
  } finally {
    loading.value = false;
  }
}

async function switchView(view) {
  activeView.value = view;
  Object.assign(filters, { status: '', source: '', owner: '', dataset: '' });
  detail.value = null;
  await load();
}

async function openDataset(datasetKey) {
  detail.value = { type: 'business', key: datasetKey };
  replayPreview.value = null;
  detailLoading.value = true;
  detailError.value = '';
  try {
    detail.value = { type: 'business', key: datasetKey, ...(await getDataFreshnessDetail(datasetKey)) };
  } catch (err) {
    detailError.value = err.message || '详情读取失败';
  } finally {
    detailLoading.value = false;
  }
}

async function openIndex(indexKey) {
  detail.value = { type: 'knowledge', key: indexKey };
  detailLoading.value = true;
  detailError.value = '';
  try {
    detail.value = { type: 'knowledge', key: indexKey, ...(await getKnowledgeIndexDetail(indexKey)) };
  } catch (err) {
    detailError.value = err.message || '详情读取失败';
  } finally {
    detailLoading.value = false;
  }
}

async function checkCurrent() {
  if (!detail.value || detail.value.type !== 'business') return;
  checking.value = true;
  try {
    await checkDataFreshness(detail.value.key);
    setFlash('ok', '检查已完成');
    await Promise.all([openDataset(detail.value.key), load()]);
  } catch (err) {
    setFlash('err', err.message || '检查失败');
  } finally {
    checking.value = false;
  }
}

async function runCurrent(retry = false) {
  const serviceId = detail.value?.type === 'business'
    ? (retry ? detail.value.actions?.retry_service_id : detail.value.actions?.run_service_id)
    : detail.value?.actions?.rebuild_service_id;
  if (!serviceId) return;
  const label = detail.value.type === 'knowledge' ? '重建指定知识索引' : (retry ? '重试失败任务' : '立即运行');
  if (!window.confirm(`确认${label}？任务将进入调度队列，不会立即显示成功。`)) return;
  running.value = true;
  try {
    const result = await runPipelineOnce({ service_id: serviceId });
    setFlash('ok', `已提交调度请求 #${result.request_id}，请稍后刷新运行流水`);
  } catch (err) {
    setFlash('err', err.message || '调度请求提交失败');
  } finally {
    running.value = false;
  }
}

async function waitRerun(runId) {
  for (let attempt = 0; attempt < 40; attempt += 1) {
    const run = await getDataQualityRerun(runId);
    if (run.status !== 'pending' && run.status !== 'running') return run;
    await new Promise((resolve) => setTimeout(resolve, 1500));
  }
  throw new Error('dry-run 仍在处理，请稍后在数据质量页查看');
}

async function previewReplay() {
  if (!replayForm.window_start || !replayForm.window_end) {
    setFlash('err', '请先填写重放窗口');
    return;
  }
  replaying.value = true;
  try {
    const created = await createDataQualityRerun({
      dataset: detail.value.key,
      window_start: replayForm.window_start,
      window_end: replayForm.window_end,
      reason: replayForm.reason || '新鲜度详情 dry-run',
      dry_run: true,
    });
    const result = await waitRerun(created.run_id);
    if (result.status !== 'finished') throw new Error('dry-run 失败，请在数据质量页查看原因');
    replayPreview.value = result;
  } catch (err) {
    replayPreview.value = null;
    setFlash('err', err.message || '重放预检失败');
  } finally {
    replaying.value = false;
  }
}

async function replayCurrent() {
  if (!replayPreview.value) {
    setFlash('err', '必须先完成 dry-run 并查看影响范围');
    return;
  }
  if (!window.confirm('确认按已预检窗口正式重放？该操作会进入受控重放队列。')) return;
  replaying.value = true;
  try {
    const result = await createDataQualityRerun({
      dataset: detail.value.key,
      window_start: replayForm.window_start,
      window_end: replayForm.window_end,
      reason: replayForm.reason || '新鲜度详情受控重放',
      dry_run: false,
    });
    setFlash('ok', `重放请求 ${result.run_id} 已提交，请在数据质量页跟踪结果`);
    replayPreview.value = null;
  } catch (err) {
    setFlash('err', err.message || '重放请求提交失败');
  } finally {
    replaying.value = false;
  }
}

async function recoverDeadLetter(serviceId) {
  if (!window.confirm(`确认将 ${serviceId} 提交人工恢复运行？成功后死信才会自动解除。`)) return;
  running.value = true;
  try {
    const result = await runPipelineOnce({ service_id: serviceId });
    setFlash('ok', `已提交恢复请求 #${result.request_id}，请稍后刷新运行结果`);
  } catch (err) {
    setFlash('err', err.message || '恢复请求提交失败');
  } finally {
    running.value = false;
  }
}

function formatTime(value) {
  return value ? String(value).replace('T', ' ').slice(0, 19) : '—';
}
function formatLag(value) {
  if (value === null || value === undefined) return '—';
  if (value < 60) return `${value} 分钟`;
  return `${Math.floor(value / 60)} 小时 ${value % 60} 分`;
}
function formatJson(value) {
  if (value === null || value === undefined || value === '') return '—';
  if (typeof value === 'string') return value;
  return JSON.stringify(value, null, 2);
}

let timer;
onMounted(() => {
  load();
  timer = setInterval(load, 60000);
});
onUnmounted(() => clearInterval(timer));
</script>

<template>
  <div class="page freshness-page">
    <div class="head">
      <div class="head-main">
        <h1>数据新鲜度</h1>
        <p class="note">状态由后端契约检查计算；未知和未检查不会被当作正常。</p>
      </div>
      <div class="head-actions">
        <button class="btn" :disabled="loading" @click="load">{{ loading ? '刷新中…' : '刷新' }}</button>
      </div>
    </div>

    <div class="view-tabs" role="tablist">
      <button :class="{ active: activeView === 'business' }" @click="switchView('business')">业务数据</button>
      <button :class="{ active: activeView === 'knowledge' }" @click="switchView('knowledge')">知识索引</button>
    </div>

    <form class="filters" @submit.prevent="load">
      <select v-model="filters.status" aria-label="按状态筛选" @change="load">
        <option value="">全部状态</option>
        <option value="stale">已超期</option><option value="blocked">已阻塞</option>
        <option value="degraded">部分异常</option><option value="unknown">状态未知</option>
        <option value="fresh">正常</option>
      </select>
      <select v-model="filters.source" aria-label="按来源筛选" @change="load">
        <option value="">全部来源</option>
        <option v-for="source in sources" :key="source" :value="source">{{ source }}</option>
      </select>
      <select v-if="activeView === 'business'" v-model="filters.owner" aria-label="按负责人筛选" @change="load">
        <option value="">全部负责人</option>
        <option v-for="owner in owners" :key="owner" :value="owner">{{ owner }}</option>
      </select>
      <input v-model="filters.dataset" class="search" placeholder="筛选数据集或索引" aria-label="筛选数据集或索引">
      <span class="count">{{ visibleItems.length }} 条</span>
    </form>

    <div v-if="loading && !items.length" class="state">新鲜度加载中…</div>
    <div v-else-if="error" class="state error">{{ error }}<button class="btn mini" @click="load">重试</button></div>
    <div v-else-if="!visibleItems.length" class="state">暂无符合筛选条件的数据</div>

    <div v-else class="table-wrap">
      <table v-if="activeView === 'business'">
        <thead><tr><th>数据集</th><th>负责人 / 来源</th><th>状态</th><th>最新业务日期</th><th>最后采集</th><th>最后加工 / 发布</th><th>滞后</th><th>最近 run_id</th></tr></thead>
        <tbody>
          <tr v-for="item in visibleItems" :key="item.dataset_key">
            <td><button class="link" @click="openDataset(item.dataset_key)">{{ item.dataset_key }}</button></td>
            <td>{{ item.owner }}<br><span class="muted">{{ item.source }}</span></td>
            <td><StatusBadge :status="item.status" /><div v-if="item.error" class="muted">{{ item.error }}</div></td>
            <td>{{ item.latest_business_date || '—' }}</td>
            <td>{{ formatTime(item.last_collected_at) }}</td>
            <td>{{ formatTime(item.last_processed_at) }}<br><span class="muted">{{ formatTime(item.last_published_at) }}</span></td>
            <td>{{ formatLag(item.lag_minutes) }}</td>
            <td class="mono">{{ item.last_run_id || '—' }}</td>
          </tr>
        </tbody>
      </table>
      <table v-else>
        <thead><tr><th>索引</th><th>来源</th><th>状态</th><th>源 / 索引 / 待处理 / 失败</th><th>最后源快照</th><th>最后成功</th><th>index_version</th></tr></thead>
        <tbody>
          <tr v-for="item in visibleItems" :key="item.index_key">
            <td><button class="link" @click="openIndex(item.index_key)">{{ item.index_key }}</button></td>
            <td>{{ item.source_type }}<br><span class="muted">{{ item.source_id || '—' }}</span></td>
            <td><StatusBadge :status="item.status" /></td>
            <td>{{ item.source_count }} / {{ item.indexed_count }} / {{ item.pending_count }} / {{ item.failed_count }}</td>
            <td>{{ formatTime(item.last_source_snapshot_at) }}</td>
            <td>{{ formatTime(item.last_success_at) }}</td>
            <td class="mono">{{ item.index_version || '—' }}</td>
          </tr>
        </tbody>
      </table>
    </div>

    <section v-if="activeView === 'business' && deadLetters.length" class="card dead-letters">
      <h2>待恢复死信（{{ deadLetters.length }}）</h2>
      <p class="note">自动重试已停止；人工恢复走现有调度审计，运行成功后自动解除。</p>
      <table>
        <thead><tr><th>服务</th><th>run_id</th><th>尝试 / 退出码</th><th>原因</th><th>进入时间</th><th>操作</th></tr></thead>
        <tbody>
          <tr v-for="letter in deadLetters" :key="`${letter.service_id}:${letter.run_id}`">
            <td>{{ letter.service_id }}</td><td class="mono">{{ letter.run_id }}</td>
            <td>{{ letter.attempts }} / {{ letter.exit_code }}</td><td>{{ letter.reason }}</td>
            <td>{{ formatTime(letter.created_at) }}</td>
            <td><button v-if="canCheck" class="btn mini" :disabled="running" @click="recoverDeadLetter(letter.service_id)">恢复运行</button></td>
          </tr>
        </tbody>
      </table>
    </section>

    <DetailDrawer
      v-if="detail"
      :title="detail.key"
      :loading="detailLoading"
      :error="detailError"
      @close="detail = null"
    >
      <template v-if="detail.type === 'business' && detail.item">
        <div class="detail-actions">
          <StatusBadge :status="detail.item.status" />
          <button v-if="canCheck" class="btn primary" :disabled="checking" @click="checkCurrent">{{ checking ? '检查中…' : '立即检查' }}</button>
          <button v-if="canCheck && detail.actions?.run_service_id" class="btn" :disabled="running" @click="runCurrent(false)">立即运行</button>
          <button v-if="canCheck && detail.actions?.retry_service_id && detail.item.status !== 'fresh'" class="btn" :disabled="running" @click="runCurrent(true)">重试失败任务</button>
        </div>
        <section v-if="detail.actions?.replay" class="card">
          <h2>按业务窗口重放</h2>
          <div class="replay-form">
            <input v-model="replayForm.window_start" type="date" aria-label="重放开始日期">
            <input v-model="replayForm.window_end" type="date" aria-label="重放结束日期">
            <input v-model="replayForm.reason" maxlength="500" placeholder="重放原因" aria-label="重放原因">
            <button v-if="canCheck" class="btn" :disabled="replaying" @click="previewReplay">dry-run 预检</button>
          </div>
          <template v-if="replayPreview">
            <pre>{{ formatJson(replayPreview.diff || replayPreview.counts) }}</pre>
            <button v-if="canCheck" class="btn primary" :disabled="replaying" @click="replayCurrent">确认正式重放</button>
          </template>
        </section>
        <section class="card">
          <h2>契约与 SLA</h2>
          <dl class="detail-grid">
            <dt>负责人</dt><dd>{{ detail.contract.owner }}</dd><dt>来源</dt><dd>{{ detail.contract.source }}</dd>
            <dt>调度</dt><dd>{{ detail.contract.schedule }}</dd><dt>应到 / 宽限</dt><dd>{{ detail.contract.expected_by }} + {{ detail.contract.max_lag_minutes }} 分钟</dd>
            <dt>业务主键</dt><dd>{{ (detail.contract.business_key_columns || []).join(', ') || '—' }}</dd>
            <dt>应到范围</dt><dd>{{ detail.contract.expected_scope || '—' }}</dd>
            <dt>DQ 规则</dt><dd>{{ (detail.contract.dq_rules || []).join(', ') || '—' }}</dd>
            <dt>恢复策略</dt><dd>重试 {{ detail.contract.auto_retry ? '开' : '关' }} / 补跑 {{ detail.contract.auto_catchup ? '开' : '关' }} / 重放 {{ detail.contract.auto_replay ? '开' : '关' }}</dd>
          </dl>
        </section>
        <section class="card">
          <h2>阶段检查与差异</h2>
          <pre>{{ formatJson(detail.item.check_details) }}</pre>
          <pre>{{ formatJson(detail.item.source_target_diff) }}</pre>
          <p v-if="detail.item.error" class="error-text">{{ detail.item.error }}</p>
        </section>
        <section class="card">
          <h2>下游影响</h2>
          <p>{{ (detail.contract.downstream_consumers || []).join('、') || '无已登记下游' }}</p>
        </section>
        <section class="card">
          <h2>最近运行</h2>
          <div v-if="!detail.recent_runs || !detail.recent_runs.length" class="empty">暂无运行记录</div>
          <table v-else><thead><tr><th>run_id</th><th>状态</th><th>开始 / 完成</th><th>读取 / 写入</th></tr></thead>
            <tbody><tr v-for="run in detail.recent_runs" :key="run.sync_run_id"><td class="mono">{{ run.sync_run_id }}</td><td>{{ run.status }}<br><span class="muted">{{ run.failure_code || '' }}</span></td><td>{{ formatTime(run.started_at) }}<br>{{ formatTime(run.finished_at) }}</td><td>{{ run.records_read }} / {{ run.raw_records_written }}</td></tr></tbody>
          </table>
        </section>
      </template>
      <template v-else-if="detail.item">
        <section class="card">
          <div class="detail-actions">
            <StatusBadge :status="detail.item.status" /><span>{{ detail.item.index_version || '无生效版本' }}</span>
            <button v-if="canCheck && detail.actions?.rebuild_service_id" class="btn" :disabled="running" @click="runCurrent(false)">重建指定索引</button>
          </div>
          <dl class="detail-grid">
            <dt>source_id</dt><dd>{{ detail.item.source_id || '—' }}</dd><dt>source_version</dt><dd>{{ detail.item.source_version || '—' }}</dd>
            <dt>content_hash</dt><dd class="mono">{{ detail.item.content_hash || '—' }}</dd><dt>chunker_version</dt><dd>{{ detail.item.chunker_version || '—' }}</dd>
            <dt>模型 / 维度</dt><dd>{{ detail.item.embedding_model || '—' }} / {{ detail.item.dim || '—' }}</dd><dt>尝试次数</dt><dd>{{ detail.item.attempts }}</dd>
            <dt>最后快照</dt><dd>{{ formatTime(detail.item.last_source_snapshot_at) }}</dd><dt>最后成功</dt><dd>{{ formatTime(detail.item.last_success_at) }}</dd>
          </dl>
          <p v-if="detail.item.last_error" class="error-text">{{ detail.item.last_error }}</p>
          <p class="note">自动删除计划：{{ detail.item.delete_plan_count || 0 }} 条待处理；当前仅展示计划，不提供生产一键删除。</p>
        </section>
      </template>
    </DetailDrawer>
  </div>
</template>

<style scoped>
.view-tabs { display: flex; gap: 4px; margin: 20px 0 12px; border-bottom: 1px solid #dbe1e8; }
.view-tabs button { border: 0; border-bottom: 2px solid transparent; background: none; color: #6b7280; }
.view-tabs button.active { color: #1d4ed8; border-bottom-color: #2563eb; }
.filters { display: flex; align-items: center; flex-wrap: wrap; gap: 8px; margin-bottom: 12px; }
.table-wrap { overflow-x: auto; }
.link { padding: 0; border: 0; background: none; color: #1d4ed8; text-align: left; }
.mono { font-family: ui-monospace, SFMono-Regular, Consolas, monospace; font-size: 12px; word-break: break-all; }
.detail-actions { display: flex; align-items: center; flex-wrap: wrap; gap: 12px; margin-bottom: 12px; }
.replay-form { display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 12px; }
.card { margin-bottom: 12px; }
.card h2 { margin: 0 0 12px; }
.detail-grid { display: grid; grid-template-columns: 110px minmax(0, 1fr); gap: 8px; margin: 0; }
.detail-grid dt { color: #6b7280; }
.detail-grid dd { margin: 0; word-break: break-word; }
pre { padding: 10px; overflow-x: auto; background: #f1f5f9; white-space: pre-wrap; word-break: break-word; }
.error-text { color: #b91c1c; }
@media (max-width: 700px) { .detail-grid { grid-template-columns: 90px minmax(0, 1fr); } }
</style>
