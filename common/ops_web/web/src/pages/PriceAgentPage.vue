<script setup>
import { computed, onMounted, reactive, ref } from 'vue';
import {
  getPriceAgentOverview,
  getPriceAgentJobs,
  getPriceAgentJob,
  getPriceAgentResults,
  retryPriceAgentJob,
  recollectPriceAgentJob,
} from '../api.js';

const STATUS_LABELS = {
  queued: '排队中', running: '运行中', waiting_login: '等待登录', completed: '已完成',
  failed: '失败', timed_out: '已超时', login_expired: '登录失效', cancelled: '已取消',
};
const AGENT_LABELS = { online: '在线', idle: '空闲', offline: '离线' };
const SOURCE_LABELS = { cron: '定时', run_once: '手动', retry: '重试', recollect: '商品补采' };
const RETRYABLE = ['failed', 'timed_out', 'login_expired'];

const loading = ref(false);
const acting = ref('');
const error = ref('');
const notice = ref('');
const overview = ref(null);
const jobs = ref([]);
const results = ref([]);
const detail = ref(null);
const jobStatus = ref('');
const resultFilters = reactive({ job_id: '', subject_key: '', item_id: '', sku_id: '' });
const completedCount = computed(() => (overview.value?.job_counts?.completed || 0));

function time(value) {
  if (!value) return '—';
  const text = String(value);
  const parsed = new Date(text.endsWith('Z') ? text : `${text}Z`);
  return Number.isNaN(parsed.getTime()) ? text : parsed.toLocaleString('zh-CN', { hour12: false });
}
function price(value) {
  return value === null || value === undefined || value === '' ? '—' : Number(value).toFixed(2);
}
function size(value) {
  const amount = Number(value || 0);
  if (amount >= 1024 * 1024) return `${(amount / 1024 / 1024).toFixed(1)} MB`;
  if (amount >= 1024) return `${(amount / 1024).toFixed(1)} KB`;
  return `${amount} B`;
}
function statusClass(status) {
  if (status === 'completed' || status === 'online') return 'ok';
  if (['failed', 'timed_out', 'login_expired', 'offline'].includes(status)) return 'bad';
  return 'pending';
}

async function loadOverview() {
  overview.value = await getPriceAgentOverview();
}
async function loadJobs() {
  const data = await getPriceAgentJobs({ status: jobStatus.value, page_size: 50 });
  jobs.value = data.items || [];
}
async function loadResults() {
  const data = await getPriceAgentResults({ ...resultFilters, page_size: 50 });
  results.value = data.items || [];
}
async function loadAll() {
  loading.value = true;
  error.value = '';
  try {
    await Promise.all([loadOverview(), loadJobs(), loadResults()]);
  } catch (err) {
    error.value = err.message || '猫超自动比价数据读取失败';
  } finally {
    loading.value = false;
  }
}
async function openJob(job) {
  error.value = '';
  try {
    detail.value = await getPriceAgentJob(job.job_id);
  } catch (err) {
    error.value = err.message || '任务详情读取失败';
  }
}
async function retry(job) {
  if (!window.confirm(`确认重试任务 ${job.job_id}？将使用原任务配置创建新任务。`)) return;
  acting.value = `retry:${job.job_id}`;
  error.value = '';
  notice.value = '';
  try {
    const result = await retryPriceAgentJob(job.job_id);
    notice.value = `已创建重试任务 ${result.job_id}`;
    await loadAll();
  } catch (err) {
    error.value = err.message || '任务重试失败';
  } finally {
    acting.value = '';
  }
}
async function recollect(row) {
  if (!window.confirm(`确认补采商品 ${row.product || row.item_id}（${row.sku_id}）？`)) return;
  acting.value = `recollect:${row.job_id}:${row.row_uid}`;
  error.value = '';
  notice.value = '';
  try {
    const result = await recollectPriceAgentJob(row.job_id, [{
      subject_key: row.subject_key,
      item_id: row.item_id,
      sku_id: row.sku_id,
    }]);
    notice.value = `已创建商品补采任务 ${result.job_id}`;
    resultFilters.job_id = result.job_id;
    await loadAll();
  } catch (err) {
    error.value = err.message || '商品补采失败';
  } finally {
    acting.value = '';
  }
}
function viewJobResults(jobId) {
  resultFilters.job_id = jobId;
  loadResults().catch((err) => { error.value = err.message || '结果读取失败'; });
}

onMounted(loadAll);
</script>

<template>
  <section class="price-agent-page">
    <header class="page-head">
      <div>
        <h1>猫超-自动比价功能</h1>
        <p>可见 Chrome 自动采集、价格结果、商品补采和任务治理；群告警已关闭。</p>
      </div>
      <button class="btn" :disabled="loading" @click="loadAll">{{ loading ? '刷新中…' : '刷新' }}</button>
    </header>

    <div v-if="error" class="message error">{{ error }}</div>
    <div v-if="notice" class="message notice">{{ notice }}</div>

    <div class="metrics">
      <div class="metric"><span>Agent</span><strong>{{ overview?.agents?.[0] ? AGENT_LABELS[overview.agents[0].status] : '离线' }}</strong></div>
      <div class="metric"><span>活跃任务</span><strong>{{ overview?.active_job?.job_id || '无' }}</strong></div>
      <div class="metric"><span>累计完成</span><strong>{{ completedCount }}</strong></div>
      <div class="metric"><span>价格结果</span><strong>{{ overview?.snapshots?.amount || 0 }}</strong></div>
      <div class="metric"><span>附件</span><strong>{{ overview?.artifacts?.amount || 0 }}</strong></div>
    </div>

    <section class="panel">
      <div class="panel-title"><h2>采集端状态</h2><span>每 5 秒轮询，超过 180 秒判定离线</span></div>
      <div class="table-wrap"><table>
        <thead><tr><th>Agent</th><th>状态</th><th>最后心跳</th><th>最近任务</th></tr></thead>
        <tbody>
          <tr v-for="agent in overview?.agents || []" :key="agent.agent_id">
            <td>{{ agent.agent_id }}</td><td><span class="badge" :class="statusClass(agent.status)">{{ AGENT_LABELS[agent.status] }}</span></td>
            <td>{{ time(agent.last_seen_at) }}</td><td class="mono">{{ agent.last_job_id || '—' }}</td>
          </tr>
          <tr v-if="!overview?.agents?.length"><td colspan="4" class="empty">暂无 agent 心跳，请先启动 Windows 采集端。</td></tr>
        </tbody>
      </table></div>
    </section>

    <section class="panel">
      <div class="panel-title"><h2>任务历史</h2><label>状态 <select v-model="jobStatus" @change="loadJobs"><option value="">全部</option><option v-for="(label, key) in STATUS_LABELS" :key="key" :value="key">{{ label }}</option></select></label></div>
      <div class="table-wrap"><table>
        <thead><tr><th>任务</th><th>状态</th><th>来源</th><th>结果</th><th>错误</th><th>创建时间</th><th>操作</th></tr></thead>
        <tbody>
          <tr v-for="job in jobs" :key="job.job_id">
            <td><button class="link mono" @click="openJob(job)">{{ job.job_id.slice(0, 8) }}</button><small>{{ job.agent_id || '未分配' }}</small></td>
            <td><span class="badge" :class="statusClass(job.status)">{{ STATUS_LABELS[job.status] || job.status }}</span></td>
            <td>{{ SOURCE_LABELS[job.source] || job.source }}</td><td>{{ job.row_count }} 行</td>
            <td>{{ job.error_code || '—' }}</td><td>{{ time(job.created_at) }}</td>
            <td class="actions"><button class="btn small" @click="viewJobResults(job.job_id)">结果</button><button v-if="RETRYABLE.includes(job.status)" class="btn small" :disabled="!!acting" @click="retry(job)">重试</button></td>
          </tr>
          <tr v-if="!jobs.length"><td colspan="7" class="empty">没有符合条件的任务。</td></tr>
        </tbody>
      </table></div>
    </section>

    <section class="panel">
      <div class="panel-title"><h2>价格结果</h2><span>选择带 SkuID 的结果可创建商品级补采</span></div>
      <div class="filters">
        <input v-model.trim="resultFilters.job_id" placeholder="Job ID" @keyup.enter="loadResults">
        <input v-model.trim="resultFilters.subject_key" placeholder="主体" @keyup.enter="loadResults">
        <input v-model.trim="resultFilters.item_id" placeholder="ItemID" @keyup.enter="loadResults">
        <input v-model.trim="resultFilters.sku_id" placeholder="SkuID" @keyup.enter="loadResults">
        <button class="btn" @click="loadResults">查询</button>
      </div>
      <div class="table-wrap"><table>
        <thead><tr><th>商品 / 规格</th><th>主体</th><th>当前价</th><th>建议价</th><th>市场价</th><th>价差</th><th>采集时间</th><th>操作</th></tr></thead>
        <tbody>
          <tr v-for="row in results" :key="row.row_uid + row.job_id">
            <td><strong>{{ row.product || '未命名商品' }}</strong><small>{{ row.product_spec || '—' }} · {{ row.item_id || '无 ItemID' }} / {{ row.sku_id || '无 SkuID' }}</small></td>
            <td>{{ row.subject_key }}</td><td>{{ price(row.current_price) }}</td><td>{{ price(row.suggested_price) }}</td>
            <td>{{ price(row.market_price) }}<small>{{ row.market_source || '—' }}</small></td><td>{{ price(row.price_gap) }}</td>
            <td>{{ time(row.captured_at) }}</td><td><button class="btn small" :disabled="!row.item_id || !row.sku_id || !!acting" @click="recollect(row)">补采</button></td>
          </tr>
          <tr v-if="!results.length"><td colspan="8" class="empty">没有符合条件的价格结果。</td></tr>
        </tbody>
      </table></div>
    </section>

    <section v-if="detail" class="panel detail">
      <div class="panel-title"><h2>任务详情</h2><button class="link" @click="detail = null">关闭</button></div>
      <div class="detail-grid">
        <span>Job</span><strong class="mono">{{ detail.job.job_id }}</strong><span>父任务</span><strong class="mono">{{ detail.job.parent_job_id || '—' }}</strong>
        <span>采集范围</span><strong>{{ detail.job.selected_skus.length ? `${detail.job.selected_skus.length} 个 SKU` : `${detail.job.subjects.length} 个主体` }}</strong>
        <span>错误</span><strong>{{ detail.job.error_detail || detail.job.error_code || '—' }}</strong>
      </div>
      <h3>附件</h3>
      <ul class="artifacts"><li v-for="item in detail.artifacts" :key="item.id"><span>{{ item.artifact_type }}</span><code>{{ item.object_key }}</code><span>{{ size(item.size_bytes) }}</span></li><li v-if="!detail.artifacts.length" class="empty">暂无附件</li></ul>
    </section>
  </section>
</template>

<style scoped>
.price-agent-page { padding: 20px; max-width: 1600px; margin: 0 auto; color: #172033; }
.page-head, .panel-title { display: flex; align-items: center; justify-content: space-between; gap: 16px; }
h1 { margin: 0; font-size: 24px; } h2 { margin: 0; font-size: 17px; } h3 { font-size: 15px; }
.page-head p, .panel-title span { margin: 6px 0 0; color: #697386; font-size: 13px; }
.metrics { display: grid; grid-template-columns: repeat(5, minmax(120px, 1fr)); gap: 12px; margin: 18px 0; }
.metric, .panel { background: #fff; border: 1px solid #e3e8ef; border-radius: 12px; }
.metric { padding: 14px; min-width: 0; } .metric span { display: block; color: #697386; font-size: 12px; }
.metric strong { display: block; margin-top: 8px; font-size: 20px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.panel { padding: 16px; margin-top: 14px; } .panel-title { margin-bottom: 12px; }
.table-wrap { overflow-x: auto; } table { width: 100%; border-collapse: collapse; font-size: 13px; }
th, td { padding: 10px 8px; border-bottom: 1px solid #edf0f4; text-align: left; vertical-align: top; }
th { color: #667085; font-weight: 600; background: #f8fafc; } td small { display: block; margin-top: 4px; color: #7a8494; }
.btn { border: 1px solid #cbd5e1; border-radius: 7px; background: #fff; color: #26364d; padding: 7px 12px; cursor: pointer; }
.btn:hover { border-color: #3b5bff; color: #3b5bff; } .btn:disabled { opacity: .5; cursor: not-allowed; }
.btn.small { padding: 4px 8px; font-size: 12px; } .link { border: 0; background: none; color: #3b5bff; cursor: pointer; padding: 0; }
.actions { white-space: nowrap; } .actions button + button { margin-left: 5px; }
.badge { display: inline-flex; border-radius: 999px; padding: 2px 8px; font-size: 12px; background: #f1f5f9; }
.badge.ok { color: #047857; background: #ecfdf5; } .badge.bad { color: #b42318; background: #fef2f2; } .badge.pending { color: #a16207; background: #fffbeb; }
.filters { display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 12px; }
.filters input, select { border: 1px solid #d8dee8; border-radius: 7px; padding: 7px 9px; background: #fff; }
.filters input { min-width: 140px; } .mono { font-family: ui-monospace, SFMono-Regular, Consolas, monospace; }
.empty { text-align: center; color: #8490a1; padding: 22px; } .message { padding: 10px 12px; border-radius: 8px; margin: 10px 0; }
.message.error { color: #b42318; background: #fef2f2; } .message.notice { color: #047857; background: #ecfdf5; }
.detail-grid { display: grid; grid-template-columns: 100px 1fr 100px 1fr; gap: 8px 12px; font-size: 13px; }
.detail-grid > span { color: #697386; } .artifacts { list-style: none; padding: 0; }
.artifacts li { display: grid; grid-template-columns: 130px 1fr 90px; gap: 10px; padding: 8px 0; border-bottom: 1px solid #edf0f4; }
.artifacts code { overflow-wrap: anywhere; }
@media (max-width: 900px) { .metrics { grid-template-columns: repeat(2, 1fr); } .detail-grid { grid-template-columns: 90px 1fr; } }
</style>
