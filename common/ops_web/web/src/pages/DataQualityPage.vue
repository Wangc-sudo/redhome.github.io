<script setup>
import { computed, reactive, ref, watch } from 'vue';
import {
  getDataQualityOverview,
  getDataQualityRules,
  createDataQualityRule,
  updateDataQualityRule,
  previewDataQuality,
  getDataQualityIssues,
  resolveDataQualityIssue,
  putDataQualityOverride,
  getDataQualityReruns,
  createDataQualityRerun,
  getDataQualityRerun,
} from '../api.js';
import { hasCapability, isAdmin, me, setFlash } from '../store/me.js';

const DATASETS = [
  { value: 'channel_daily_sales', label: '渠道日销' },
  { value: 'wdt_order_line_fact', label: '旺店通订单行' },
];
const TABS = [
  { value: 'overview', label: '质量总览' },
  { value: 'rules', label: '规则管理' },
  { value: 'issues', label: '异常处理' },
  { value: 'reruns', label: '重跑记录' },
];
const RULE_TYPES = ['required', 'enum', 'range', 'regex', 'unique', 'mapping', 'freshness', 'reconcile', 'schema'];
const COUNT_LABELS = {
  records_read: '读取',
  records_passed: '通过',
  records_quarantined: '隔离',
  records_written: '写入',
  duplicates: '重复',
  unmapped: '未映射',
  issue_count: '异常',
};
const RUN_STATUS_LABELS = {
  pending: '等待调度',
  running: '运行中',
  finished: '已完成',
  failed: '失败',
  rejected: '已拒绝',
};
const ISSUE_STATUS_LABELS = {
  open: '待处理',
  confirmed: '已确认',
  ignored: '已忽略',
  resolved: '已解决',
};

const activeTab = ref('overview');
const loading = reactive({ overview: false, rules: false, issues: false, reruns: false });
const errors = reactive({ overview: '', rules: '', issues: '', reruns: '' });
const canWrite = computed(() => {
  const info = me.value;
  return !!info && info.userid !== 'anon-ops' && (isAdmin() || hasCapability('datafix'));
});

function datasetLabel(value) {
  const found = DATASETS.find((item) => item.value === value);
  return found ? found.label : value;
}
function runStatus(value) {
  return RUN_STATUS_LABELS[value] || value || '暂无运行';
}
function issueStatus(value) {
  return ISSUE_STATUS_LABELS[value] || value || '—';
}
function metricEntries(row) {
  const counts = (row && (row.counts || row)) || {};
  return Object.entries(COUNT_LABELS).map(([key, label]) => ({
    key, label, value: Number(counts[key] || 0),
  }));
}
function formatJson(value) {
  if (value === undefined || value === null || value === '') return '—';
  if (typeof value === 'string') return value;
  try {
    return JSON.stringify(value, null, 2);
  } catch (err) {
    return String(value);
  }
}
function compactJson(value) {
  if (value === undefined || value === null || value === '') return '—';
  if (typeof value === 'string') return value;
  try {
    return JSON.stringify(value);
  } catch (err) {
    return String(value);
  }
}
function shortVersion(value) {
  const text = String(value || '');
  return text.length > 12 ? text.slice(0, 12) + '…' : (text || '—');
}
function validateWindow(start, end) {
  if (!!start !== !!end) throw new Error('业务窗口需同时填写起止日期');
  if (start && end && start > end) throw new Error('业务窗口起始日期不能晚于结束日期');
}
function windowPayload(start, end) {
  validateWindow(start, end);
  return start ? { window_start: start, window_end: end } : {};
}
function windowText(row) {
  if (!row || !row.window_start) return '全部业务窗口';
  return `${row.window_start} 至 ${row.window_end}`;
}
function normalizedDate(value) {
  return value ? String(value).slice(0, 10) : '';
}

/* ------------------------------ 质量总览 ------------------------------ */
const overviewItems = ref([]);
const overviewDatasets = ref(DATASETS.map((item) => item.value));
const overviewRows = computed(() => overviewDatasets.value.map((dataset) => (
  overviewItems.value.find((item) => item.dataset === dataset) || { dataset }
)));

async function loadOverview() {
  loading.overview = true;
  errors.overview = '';
  try {
    const data = await getDataQualityOverview();
    overviewItems.value = (data && data.items) || [];
    if (data && Array.isArray(data.datasets) && data.datasets.length) {
      overviewDatasets.value = data.datasets;
    }
  } catch (err) {
    errors.overview = err.message || '质量总览读取失败';
  } finally {
    loading.overview = false;
  }
}
function openDatasetIssues(dataset) {
  issueFilters.dataset = dataset;
  issueFilters.status = 'open';
  issueFilters.rule_code = '';
  activeTab.value = 'issues';
}

/* ------------------------------ 规则管理 ------------------------------ */
const ruleDataset = ref(DATASETS[0].value);
const rules = ref([]);
const ruleVersion = ref('');
const editingRuleId = ref(null);
const ruleForm = reactive({
  dataset: DATASETS[0].value,
  rule_code: '',
  rule_type: 'required',
  field_name: '',
  paramsText: '{}',
  severity: 'error',
  enabled: true,
  version: 1,
  window_start: '',
  window_end: '',
});
const previewOpen = ref(false);
const previewLoading = ref(false);
const previewSaving = ref(false);
const previewResult = ref(null);
const previewCandidate = ref(null);
const previewRuleId = ref(null);
const previewError = ref('');

function resetRuleForm() {
  editingRuleId.value = null;
  Object.assign(ruleForm, {
    dataset: ruleDataset.value,
    rule_code: '',
    rule_type: 'required',
    field_name: '',
    paramsText: '{}',
    severity: 'error',
    enabled: true,
    version: 1,
    window_start: '',
    window_end: '',
  });
}
async function loadRules() {
  loading.rules = true;
  errors.rules = '';
  try {
    const data = await getDataQualityRules(ruleDataset.value);
    rules.value = (data && data.items) || [];
    ruleVersion.value = (data && data.rule_version) || '';
  } catch (err) {
    errors.rules = err.message || '规则读取失败';
    rules.value = [];
  } finally {
    loading.rules = false;
  }
}
function editRule(rule) {
  editingRuleId.value = rule.id;
  Object.assign(ruleForm, {
    dataset: rule.dataset,
    rule_code: rule.rule_code,
    rule_type: rule.rule_type,
    field_name: rule.field_name || '',
    paramsText: JSON.stringify(rule.params || {}, null, 2),
    severity: rule.severity || 'error',
    enabled: !!rule.enabled,
    version: rule.version || 1,
    window_start: '',
    window_end: '',
  });
}
function buildRuleCandidate() {
  let params;
  try {
    params = JSON.parse(ruleForm.paramsText || '{}');
  } catch (err) {
    throw new Error('规则参数必须是合法 JSON');
  }
  if (!params || Array.isArray(params) || typeof params !== 'object') {
    throw new Error('规则参数必须是 JSON object');
  }
  const candidate = {
    dataset: ruleForm.dataset,
    rule_code: ruleForm.rule_code.trim(),
    rule_type: ruleForm.rule_type,
    field_name: ruleForm.field_name.trim() || null,
    params,
    severity: ruleForm.severity,
    enabled: !!ruleForm.enabled,
  };
  if (editingRuleId.value !== null) {
    candidate.id = editingRuleId.value;
    candidate.version = ruleForm.version;
  }
  return candidate;
}
async function previewRule() {
  previewLoading.value = true;
  previewError.value = '';
  errors.rules = '';
  try {
    const candidate = buildRuleCandidate();
    const payload = {
      dataset: candidate.dataset,
      rule: candidate,
      ...windowPayload(ruleForm.window_start, ruleForm.window_end),
    };
    const result = await previewDataQuality(payload);
    previewCandidate.value = candidate;
    previewRuleId.value = editingRuleId.value;
    previewResult.value = result;
    previewOpen.value = true;
  } catch (err) {
    errors.rules = err.message || '预览失败';
  } finally {
    previewLoading.value = false;
  }
}
async function savePreviewedRule() {
  const candidate = previewCandidate.value;
  const result = previewResult.value;
  if (!candidate || !result) return;
  const message = `确认保存规则 ${candidate.rule_code}？\n`
    + `本次预览读取 ${result.records_read || 0} 条，隔离 ${result.records_quarantined || 0} 条，`
    + `发现 ${result.issue_count || 0} 条异常；保存将影响 1 条规则。`;
  if (!window.confirm(message)) return;
  previewSaving.value = true;
  previewError.value = '';
  try {
    let response;
    if (previewRuleId.value !== null) {
      response = await updateDataQualityRule(previewRuleId.value, {
        rule_type: candidate.rule_type,
        field_name: candidate.field_name,
        params: candidate.params,
        severity: candidate.severity,
        enabled: candidate.enabled,
      });
    } else {
      response = await createDataQualityRule({
        dataset: candidate.dataset,
        rule_code: candidate.rule_code,
        rule_type: candidate.rule_type,
        field_name: candidate.field_name,
        params: candidate.params,
        severity: candidate.severity,
        enabled: candidate.enabled,
      });
    }
    previewOpen.value = false;
    previewResult.value = null;
    previewCandidate.value = null;
    resetRuleForm();
    setFlash('ok', `规则已保存（影响 1 条，审计 ${response.audit_id || '已记录'}）`);
    await loadRules();
  } catch (err) {
    previewError.value = err.message || '规则保存失败';
  } finally {
    previewSaving.value = false;
  }
}
watch(ruleDataset, () => {
  resetRuleForm();
  loadRules();
});

/* ------------------------------ 异常处理 ------------------------------ */
const issueFilters = reactive({ dataset: DATASETS[0].value, status: 'open', rule_code: '' });
const issues = ref([]);
const issueCursor = ref('');
const issueNextCursor = ref(null);
const issueHistory = ref([]);
const issueWorkingId = ref(null);
const overrideOpen = ref(false);
const overrideSaving = ref(false);
const overrideError = ref('');
const overrideSource = ref(null);
const overrideForm = reactive({ value: '', reason: '' });

async function loadIssues(cursor = '') {
  loading.issues = true;
  errors.issues = '';
  try {
    const data = await getDataQualityIssues(issueFilters.dataset, {
      status: issueFilters.status,
      rule_code: issueFilters.rule_code.trim(),
      cursor,
      limit: 50,
    });
    issues.value = (data && data.items) || [];
    issueNextCursor.value = data && data.next_cursor;
    issueCursor.value = cursor;
  } catch (err) {
    errors.issues = err.message || '异常读取失败';
    issues.value = [];
    issueNextCursor.value = null;
  } finally {
    loading.issues = false;
  }
}
function applyIssueFilters() {
  issueHistory.value = [];
  issueCursor.value = '';
  loadIssues('');
}
async function nextIssues() {
  if (!issueNextCursor.value) return;
  issueHistory.value.push(issueCursor.value);
  await loadIssues(String(issueNextCursor.value));
}
async function previousIssues() {
  if (!issueHistory.value.length) return;
  const cursor = issueHistory.value.pop();
  await loadIssues(cursor);
}
async function resolveIssue(issue, status) {
  if (!canWrite.value) return;
  const action = status === 'confirmed' ? '确认' : '忽略';
  const reason = (window.prompt(`请输入${action}异常 ${issue.id} 的原因（必填，进审计）`, '') || '').trim();
  if (!reason) return;
  if (!window.confirm(`确认${action}该异常？将影响 1 条异常，状态改为 ${status}。`)) return;
  issueWorkingId.value = issue.id;
  try {
    const response = await resolveDataQualityIssue(issue.id, { status, reason });
    setFlash('ok', `异常 ${issue.id} 已${action}（状态 ${response.status}，影响 1 条）`);
    await loadIssues(issueCursor.value);
    await loadOverview();
  } catch (err) {
    setFlash('err', err.message || '异常处理失败');
  } finally {
    issueWorkingId.value = null;
  }
}
function openOverride(issue) {
  if (!canWrite.value || !issue.field_name || !issue.source_record_id) return;
  overrideSource.value = issue;
  overrideForm.value = issue.cleaned_value === null || issue.cleaned_value === undefined
    ? '' : String(issue.cleaned_value);
  overrideForm.reason = '';
  overrideError.value = '';
  overrideOpen.value = true;
}
async function submitOverride() {
  const issue = overrideSource.value;
  if (!issue) return;
  const reason = overrideForm.reason.trim();
  if (!reason) {
    overrideError.value = '请填写覆盖原因';
    return;
  }
  if (!window.confirm(
    `确认保存字段覆盖？将影响 1 个字段：${issue.source_record_id} / ${issue.field_name}；状态为 active。`,
  )) return;
  overrideSaving.value = true;
  overrideError.value = '';
  try {
    const response = await putDataQualityOverride({
      dataset: issue.dataset,
      source_record_id: issue.source_record_id,
      field_name: issue.field_name,
      value: overrideForm.value,
      reason,
    });
    overrideOpen.value = false;
    setFlash(
      'ok',
      `字段覆盖已保存（active=${response.active}，影响 1 个字段）；下一次检查生效，当前异常需单独处理`,
    );
  } catch (err) {
    overrideError.value = err.message || '字段覆盖失败';
  } finally {
    overrideSaving.value = false;
  }
}

/* ------------------------------ 重跑记录 ------------------------------ */
const rerunForm = reactive({
  dataset: DATASETS[0].value,
  window_start: '',
  window_end: '',
  rule_version: '',
  reason: '',
});
const reruns = ref([]);
const rerunCursor = ref('');
const rerunNextCursor = ref(null);
const rerunHistory = ref([]);
const rerunSubmitting = ref('');
const rerunDetailLoading = ref('');
const selectedRerun = ref(null);
const readyDryRun = computed(() => reruns.value.find((run) => (
  run.dry_run === true
  && run.status === 'finished'
  && run.dataset === rerunForm.dataset
  && run.rule_version === rerunForm.rule_version
  && normalizedDate(run.window_start) === normalizedDate(rerunForm.window_start)
  && normalizedDate(run.window_end) === normalizedDate(rerunForm.window_end)
)) || null);

async function loadRerunRuleVersion() {
  try {
    const data = await getDataQualityRules(rerunForm.dataset);
    if (data && data.rule_version) rerunForm.rule_version = data.rule_version;
  } catch (err) {
    errors.reruns = err.message || '当前规则版本读取失败';
  }
}
async function loadReruns(cursor = '') {
  loading.reruns = true;
  errors.reruns = '';
  try {
    const data = await getDataQualityReruns({ dataset: rerunForm.dataset, cursor, limit: 50 });
    reruns.value = (data && data.items) || [];
    rerunNextCursor.value = data && data.next_cursor;
    rerunCursor.value = cursor;
  } catch (err) {
    errors.reruns = err.message || '重跑记录读取失败';
    reruns.value = [];
    rerunNextCursor.value = null;
  } finally {
    loading.reruns = false;
  }
}
function applyRerunDataset() {
  rerunHistory.value = [];
  rerunCursor.value = '';
  rerunForm.rule_version = '';
  selectedRerun.value = null;
  loadRerunRuleVersion();
  loadReruns('');
}
async function nextReruns() {
  if (!rerunNextCursor.value) return;
  rerunHistory.value.push(rerunCursor.value);
  await loadReruns(String(rerunNextCursor.value));
}
async function previousReruns() {
  if (!rerunHistory.value.length) return;
  const cursor = rerunHistory.value.pop();
  await loadReruns(cursor);
}
async function refreshReruns() {
  rerunHistory.value = [];
  await Promise.all([loadRerunRuleVersion(), loadReruns('')]);
}
async function submitRerun(dryRun) {
  if (!canWrite.value) return;
  let payload;
  try {
    if (!rerunForm.window_start || !rerunForm.window_end) {
      throw new Error('重跑必须填写完整的业务窗口');
    }
    payload = {
      dataset: rerunForm.dataset,
      ...windowPayload(rerunForm.window_start, rerunForm.window_end),
      rule_version: rerunForm.rule_version || undefined,
      dry_run: dryRun,
      reason: rerunForm.reason.trim(),
    };
  } catch (err) {
    setFlash('err', err.message);
    return;
  }
  if (!dryRun && !readyDryRun.value) {
    setFlash('err', '正式重跑前，需先有相同数据集、业务窗口和规则版本的已完成 dry-run；请刷新记录确认状态');
    return;
  }
  let confirmation = `确认提交${dryRun ? ' dry-run' : '正式'}重跑请求？\n`
    + `数据集：${datasetLabel(payload.dataset)}；窗口：${payload.window_start ? `${payload.window_start} 至 ${payload.window_end}` : '全部'}。\n`
    + '请求只会入队为 pending，不代表已经执行完成。';
  if (!dryRun) {
    const counts = readyDryRun.value.counts || readyDryRun.value;
    confirmation = `已完成 dry-run：${readyDryRun.value.run_id}，读取 ${counts.records_read || 0} 条，`
      + `隔离 ${counts.records_quarantined || 0} 条，异常 ${counts.issue_count || 0} 条。\n` + confirmation;
  }
  if (!window.confirm(confirmation)) return;
  rerunSubmitting.value = dryRun ? 'dry' : 'formal';
  try {
    const response = await createDataQualityRerun(payload);
    rerunForm.rule_version = response.rule_version || rerunForm.rule_version;
    setFlash(
      'ok',
      `重跑请求 ${response.run_id} 已入队（mode=${response.mode}，status=${response.status}，dry_run=${response.dry_run}）；请等待调度，并非已完成`,
    );
    rerunHistory.value = [];
    await loadReruns('');
  } catch (err) {
    setFlash('err', err.message || '重跑请求提交失败');
  } finally {
    rerunSubmitting.value = '';
  }
}
async function viewRerun(run) {
  rerunDetailLoading.value = run.run_id;
  errors.reruns = '';
  try {
    selectedRerun.value = await getDataQualityRerun(run.run_id);
  } catch (err) {
    errors.reruns = err.message || '重跑详情读取失败';
  } finally {
    rerunDetailLoading.value = '';
  }
}

watch(activeTab, (tab) => {
  if (tab === 'overview') loadOverview();
  if (tab === 'rules') loadRules();
  if (tab === 'issues') applyIssueFilters();
  if (tab === 'reruns') {
    loadRerunRuleVersion();
    loadReruns('');
  }
}, { immediate: true });
</script>

<template>
  <div class="page data-quality-page">
    <div class="head">
      <div class="head-main">
        <h1>数据质量</h1>
        <p class="note">规则预览只读计算，不写控制表；重跑仅创建等待调度请求，入队不等于执行完成。</p>
      </div>
      <span class="access-badge" :class="{ writable: canWrite }">{{ canWrite ? '可管理' : '只读' }}</span>
    </div>

    <div class="quality-tabs" role="tablist" aria-label="数据质量功能">
      <button
        v-for="tab in TABS" :key="tab.value" type="button" role="tab"
        :aria-selected="activeTab === tab.value" :class="{ active: activeTab === tab.value }"
        @click="activeTab = tab.value"
      >{{ tab.label }}</button>
    </div>

    <section v-if="activeTab === 'overview'" class="tab-panel">
      <div class="section-head">
        <h2>质量总览</h2>
        <button class="btn" :disabled="loading.overview" @click="loadOverview">刷新</button>
      </div>
      <div v-if="errors.overview" class="state error">{{ errors.overview }}</div>
      <div v-else-if="loading.overview" class="state">加载中…</div>
      <div v-else class="overview-grid">
        <article v-for="row in overviewRows" :key="row.dataset" class="card dataset-card">
          <div class="dataset-title">
            <div><h3>{{ datasetLabel(row.dataset) }}</h3><code>{{ row.dataset }}</code></div>
            <span class="status-pill" :class="row.status">{{ runStatus(row.status) }}</span>
          </div>
          <template v-if="row.run_id">
            <p class="hint">最近批次 {{ row.run_id }} · {{ row.mode }} · {{ windowText(row) }}</p>
            <p class="hint">规则版本 <code :title="row.rule_version">{{ shortVersion(row.rule_version) }}</code>
              · 告警 {{ row.alert_status || '—' }}</p>
            <div class="metrics">
              <div v-for="metric in metricEntries(row)" :key="metric.key">
                <strong>{{ metric.value }}</strong><span>{{ metric.label }}</span>
              </div>
            </div>
            <p class="issue-summary">
              待处理 {{ (row.issues && row.issues.open) || 0 }} ·
              已确认 {{ (row.issues && row.issues.confirmed) || 0 }} ·
              已忽略 {{ (row.issues && row.issues.ignored) || 0 }} ·
              已解决 {{ (row.issues && row.issues.resolved) || 0 }}
            </p>
          </template>
          <p v-else class="empty">该数据集暂无质量检查或重跑批次</p>
          <button class="btn mini" @click="openDatasetIssues(row.dataset)">处理异常</button>
        </article>
      </div>
      <p class="hint">总览展示数据层提供的最近批次与异常状态计数，不推算未提供的“最近成功批次”或 T-1 状态。</p>
    </section>

    <section v-if="activeTab === 'rules'" class="tab-panel">
      <div class="section-head">
        <h2>规则管理</h2>
        <label>数据集
          <select v-model="ruleDataset">
            <option v-for="dataset in DATASETS" :key="dataset.value" :value="dataset.value">{{ dataset.label }}</option>
          </select>
        </label>
      </div>
      <p class="hint">当前启用规则版本：<code class="version">{{ ruleVersion || '—' }}</code></p>
      <div v-if="errors.rules" class="flash err">{{ errors.rules }}</div>

      <form v-if="canWrite" class="card rule-editor" @submit.prevent="previewRule">
        <div class="section-head">
          <h3>{{ editingRuleId === null ? '新增规则' : `编辑规则 #${editingRuleId}` }}</h3>
          <button v-if="editingRuleId !== null" type="button" class="btn mini" @click="resetRuleForm">取消编辑</button>
        </div>
        <div class="form-grid">
          <label class="field"><span class="field-label">数据集</span>
            <select v-model="ruleForm.dataset" disabled>
              <option v-for="dataset in DATASETS" :key="dataset.value" :value="dataset.value">{{ dataset.label }}</option>
            </select>
          </label>
          <label class="field"><span class="field-label">规则编号</span>
            <input v-model="ruleForm.rule_code" :disabled="editingRuleId !== null" pattern="[A-Z][A-Z0-9_]{2,63}" required>
          </label>
          <label class="field"><span class="field-label">规则类型</span>
            <select v-model="ruleForm.rule_type"><option v-for="kind in RULE_TYPES" :key="kind">{{ kind }}</option></select>
          </label>
          <label class="field"><span class="field-label">字段名（按类型必填）</span>
            <input v-model="ruleForm.field_name" pattern="[a-z][a-z0-9_]*">
          </label>
          <label class="field"><span class="field-label">严重度</span>
            <select v-model="ruleForm.severity"><option>critical</option><option>error</option><option>warning</option></select>
          </label>
          <label class="check-field"><input v-model="ruleForm.enabled" type="checkbox"> 启用规则</label>
          <label class="field"><span class="field-label">预览窗口开始（可空）</span><input v-model="ruleForm.window_start" type="date"></label>
          <label class="field"><span class="field-label">预览窗口结束（可空）</span><input v-model="ruleForm.window_end" type="date"></label>
          <label class="field params-field"><span class="field-label">规则参数（JSON object）</span>
            <textarea v-model="ruleForm.paramsText" rows="4" spellcheck="false" required></textarea>
          </label>
        </div>
        <div class="form-actions">
          <span class="hint">保存前必须运行真实预览；预览结果在抽屉中确认。</span>
          <button class="btn primary" :disabled="previewLoading">{{ previewLoading ? '预览中…' : '预览拟保存规则' }}</button>
        </div>
      </form>
      <p v-else class="hint">当前身份仅可查看规则；写操作需要管理员或 datafix 能力。</p>

      <div class="table-wrap">
        <table>
          <thead><tr><th>ID</th><th>规则编号</th><th>类型</th><th>字段</th><th>参数</th><th>严重度</th><th>版本</th><th>状态</th><th v-if="canWrite">操作</th></tr></thead>
          <tbody>
            <tr v-for="rule in rules" :key="rule.id">
              <td>{{ rule.id }}</td><td><code>{{ rule.rule_code }}</code></td><td>{{ rule.rule_type }}</td>
              <td>{{ rule.field_name || '—' }}</td><td class="json-cell">{{ compactJson(rule.params) }}</td>
              <td>{{ rule.severity }}</td><td>{{ rule.version }}</td><td>{{ rule.enabled ? '启用' : '停用' }}</td>
              <td v-if="canWrite"><button class="btn mini" @click="editRule(rule)">编辑并预览</button></td>
            </tr>
            <tr v-if="!loading.rules && !rules.length"><td :colspan="canWrite ? 9 : 8" class="empty">暂无规则</td></tr>
          </tbody>
        </table>
      </div>
    </section>

    <section v-if="activeTab === 'issues'" class="tab-panel">
      <div class="section-head"><h2>异常处理</h2><span class="count">每页 50 条</span></div>
      <form class="inline" @submit.prevent="applyIssueFilters">
        <select v-model="issueFilters.dataset" @change="applyIssueFilters">
          <option v-for="dataset in DATASETS" :key="dataset.value" :value="dataset.value">{{ dataset.label }}</option>
        </select>
        <select v-model="issueFilters.status" @change="applyIssueFilters">
          <option value="">全部状态</option><option value="open">待处理</option><option value="confirmed">已确认</option>
          <option value="ignored">已忽略</option><option value="resolved">已解决</option>
        </select>
        <input v-model="issueFilters.rule_code" placeholder="规则编号（可空）">
        <button type="submit" class="btn">筛选</button>
      </form>
      <div v-if="errors.issues" class="state error">{{ errors.issues }}</div>
      <div v-else-if="loading.issues" class="state">加载中…</div>
      <div v-else class="table-wrap">
        <table>
          <thead><tr><th>ID</th><th>规则 / 原因</th><th>来源记录</th><th>字段</th><th>原值</th><th>清洗值</th><th>严重度</th><th>状态</th><th v-if="canWrite">操作</th></tr></thead>
          <tbody>
            <tr v-for="issue in issues" :key="issue.id">
              <td>{{ issue.id }}</td>
              <td><code>{{ issue.rule_code }}</code><br><span class="hint">{{ issue.reason }}</span></td>
              <td class="break-cell">{{ issue.source_record_id || '—' }}<br><span class="hint">{{ issue.business_key || '' }}</span></td>
              <td>{{ issue.field_name || '—' }}</td><td class="break-cell">{{ compactJson(issue.original_value) }}</td>
              <td class="break-cell">{{ compactJson(issue.cleaned_value) }}</td><td>{{ issue.severity }}</td>
              <td>{{ issueStatus(issue.status) }}</td>
              <td v-if="canWrite" class="actions-cell">
                <template v-if="issue.status === 'open'">
                  <button class="btn mini" :disabled="issueWorkingId === issue.id" @click="resolveIssue(issue, 'confirmed')">确认</button>
                  <button class="btn mini" :disabled="issueWorkingId === issue.id" @click="resolveIssue(issue, 'ignored')">忽略</button>
                </template>
                <button v-if="issue.field_name && issue.source_record_id" class="btn mini" @click="openOverride(issue)">字段覆盖</button>
              </td>
            </tr>
            <tr v-if="!issues.length"><td :colspan="canWrite ? 9 : 8" class="empty">暂无符合条件的异常</td></tr>
          </tbody>
        </table>
      </div>
      <div class="pager">
        <button class="btn" :disabled="!issueHistory.length || loading.issues" @click="previousIssues">上一页</button>
        <button class="btn" :disabled="!issueNextCursor || loading.issues" @click="nextIssues">下一页</button>
      </div>
      <p class="hint">字段覆盖仅影响后续质量检查，不会直接 UPDATE raw 或业务事实表，也不会自动关闭当前异常。</p>
    </section>

    <section v-if="activeTab === 'reruns'" class="tab-panel">
      <div class="section-head">
        <h2>重跑记录</h2>
        <button class="btn" :disabled="loading.reruns" @click="refreshReruns">刷新状态</button>
      </div>
      <form v-if="canWrite" class="card rerun-editor" @submit.prevent="submitRerun(true)">
        <h3>申请重跑</h3>
        <div class="form-grid rerun-grid">
          <label class="field"><span class="field-label">数据集</span>
            <select v-model="rerunForm.dataset" @change="applyRerunDataset">
              <option v-for="dataset in DATASETS" :key="dataset.value" :value="dataset.value">{{ dataset.label }}</option>
            </select>
          </label>
          <label class="field"><span class="field-label">业务窗口开始（必填）</span><input v-model="rerunForm.window_start" type="date" required></label>
          <label class="field"><span class="field-label">业务窗口结束（必填）</span><input v-model="rerunForm.window_end" type="date" required></label>
          <label class="field version-field"><span class="field-label">当前启用规则版本</span>
            <input v-model="rerunForm.rule_version" placeholder="加载后自动填入；留空由后端取当前版本" readonly>
          </label>
          <label class="field reason-field"><span class="field-label">申请原因（可空，进审计）</span><input v-model="rerunForm.reason" maxlength="500"></label>
        </div>
        <div class="dry-run-state" :class="{ ready: readyDryRun }">
          <template v-if="readyDryRun">
            已匹配完成的 dry-run <code>{{ readyDryRun.run_id }}</code>：
            读取 {{ (readyDryRun.counts && readyDryRun.counts.records_read) || 0 }}，
            隔离 {{ (readyDryRun.counts && readyDryRun.counts.records_quarantined) || 0 }}，
            异常 {{ (readyDryRun.counts && readyDryRun.counts.issue_count) || 0 }}。
          </template>
          <template v-else>正式提交前，需要相同数据集、窗口和规则版本的 dry-run 状态为“已完成”；pending 不算完成。</template>
        </div>
        <div class="form-actions">
          <span class="hint">先 dry-run，再正式提交；两者都只创建 pending 请求。</span>
          <button type="submit" class="btn" :disabled="!!rerunSubmitting">{{ rerunSubmitting === 'dry' ? '提交中…' : '1. 提交 dry-run' }}</button>
          <button type="button" class="btn primary" :disabled="!!rerunSubmitting || !readyDryRun" @click="submitRerun(false)">
            {{ rerunSubmitting === 'formal' ? '提交中…' : '2. 正式重跑' }}
          </button>
        </div>
      </form>
      <p v-else class="hint">当前身份仅可查看重跑记录；申请重跑需要管理员或 datafix 能力。</p>
      <div v-if="errors.reruns" class="flash err">{{ errors.reruns }}</div>

      <div class="section-head">
        <h3>{{ datasetLabel(rerunForm.dataset) }}的请求</h3>
        <label>切换记录数据集
          <select v-model="rerunForm.dataset" @change="applyRerunDataset">
            <option v-for="dataset in DATASETS" :key="dataset.value" :value="dataset.value">{{ dataset.label }}</option>
          </select>
        </label>
      </div>
      <div v-if="loading.reruns" class="state">加载中…</div>
      <div v-else class="table-wrap">
        <table>
          <thead><tr><th>请求 / 时间</th><th>状态</th><th>dry_run</th><th>业务窗口</th><th>规则版本</th><th>计数</th><th>申请人</th><th>详情</th></tr></thead>
          <tbody>
            <tr v-for="run in reruns" :key="run.run_id">
              <td><code>{{ run.run_id }}</code><br><span class="hint">{{ run.created_at || '—' }}</span></td>
              <td><span class="status-pill" :class="run.status">{{ runStatus(run.status) }}</span></td>
              <td>{{ run.dry_run ? '是' : '否' }}</td><td>{{ windowText(run) }}</td>
              <td><code :title="run.rule_version">{{ shortVersion(run.rule_version) }}</code></td>
              <td>读取 {{ (run.counts && run.counts.records_read) || 0 }} / 异常 {{ (run.counts && run.counts.issue_count) || 0 }}</td>
              <td>{{ run.requested_by || '—' }}</td>
              <td><button class="btn mini" :disabled="rerunDetailLoading === run.run_id" @click="viewRerun(run)">查看</button></td>
            </tr>
            <tr v-if="!reruns.length"><td colspan="8" class="empty">暂无重跑请求</td></tr>
          </tbody>
        </table>
      </div>
      <div class="pager">
        <button class="btn" :disabled="!rerunHistory.length || loading.reruns" @click="previousReruns">上一页</button>
        <button class="btn" :disabled="!rerunNextCursor || loading.reruns" @click="nextReruns">下一页</button>
      </div>
      <div v-if="selectedRerun" class="card rerun-detail">
        <div class="section-head"><h3>请求详情 · {{ selectedRerun.run_id }}</h3><button class="btn mini" @click="selectedRerun = null">关闭</button></div>
        <p>状态 <strong>{{ runStatus(selectedRerun.status) }}</strong> · dry_run={{ selectedRerun.dry_run }} · {{ windowText(selectedRerun) }}</p>
        <div class="metrics"><div v-for="metric in metricEntries(selectedRerun)" :key="metric.key"><strong>{{ metric.value }}</strong><span>{{ metric.label }}</span></div></div>
        <h4>差异</h4><pre>{{ formatJson(selectedRerun.diff) }}</pre>
        <p v-if="selectedRerun.failure_code" class="flash err">失败代码：{{ selectedRerun.failure_code }}</p>
      </div>
    </section>

    <div v-if="previewOpen" class="drawer-mask" @click.self="previewOpen = false">
      <aside class="drawer quality-drawer" aria-label="规则预览结果">
        <h2 class="drawer-title">规则预览 · {{ previewCandidate && previewCandidate.rule_code }}</h2>
        <p class="drawer-note">真实 Runner(mode=preview, persist=false) 计算；未写任何 dq_* 控制表。预览计算的 finished 不代表创建了运行记录。</p>
        <div v-if="previewError" class="flash err">{{ previewError }}</div>
        <template v-if="previewResult">
          <div class="metrics"><div v-for="metric in metricEntries(previewResult)" :key="metric.key"><strong>{{ metric.value }}</strong><span>{{ metric.label }}</span></div></div>
          <p>告警状态：<strong>{{ previewResult.alert_status }}</strong> · 批次 {{ previewResult.batch_count }} · SQL {{ previewResult.sql_count }}</p>
          <h3>异常样本（{{ (previewResult.samples || []).length }}）</h3>
          <div class="table-wrap">
            <table>
              <thead><tr><th>来源记录</th><th>规则</th><th>字段</th><th>原因</th><th>原值</th><th>清洗值</th></tr></thead>
              <tbody>
                <tr v-for="(sample, index) in previewResult.samples || []" :key="index">
                  <td class="break-cell">{{ sample.source_record_id || '—' }}</td><td>{{ sample.rule_code }}</td><td>{{ sample.field_name || '—' }}</td>
                  <td>{{ sample.reason }}</td><td class="break-cell">{{ compactJson(sample.original_value) }}</td><td class="break-cell">{{ compactJson(sample.cleaned_value) }}</td>
                </tr>
                <tr v-if="!(previewResult.samples || []).length"><td colspan="6" class="empty">未发现异常样本</td></tr>
              </tbody>
            </table>
          </div>
        </template>
        <div class="drawer-actions">
          <button class="btn" :disabled="previewSaving" @click="previewOpen = false">返回修改</button>
          <button v-if="canWrite" class="btn primary" :disabled="previewSaving || !previewResult" @click="savePreviewedRule">
            {{ previewSaving ? '保存中…' : '确认保存规则' }}
          </button>
        </div>
      </aside>
    </div>

    <div v-if="overrideOpen && overrideSource" class="modal-mask" @click.self="overrideOpen = false">
      <form class="modal override-modal" @submit.prevent="submitOverride">
        <h2 class="modal-title">字段覆盖 · 异常 {{ overrideSource.id }}</h2>
        <p class="hint">{{ overrideSource.dataset }} / {{ overrideSource.source_record_id }} / {{ overrideSource.field_name }}</p>
        <label class="field"><span class="field-label">覆盖值</span><textarea v-model="overrideForm.value" rows="4"></textarea></label>
        <label class="field"><span class="field-label">覆盖原因（必填，进审计）</span><textarea v-model="overrideForm.reason" rows="3" maxlength="500" required></textarea></label>
        <div v-if="overrideError" class="flash err">{{ overrideError }}</div>
        <p class="hint">只保存质量覆盖值，下一次检查生效；不直接修改 raw 或业务事实表。</p>
        <div class="modal-actions">
          <button type="button" class="btn" :disabled="overrideSaving" @click="overrideOpen = false">取消</button>
          <button type="submit" class="btn primary" :disabled="overrideSaving">{{ overrideSaving ? '保存中…' : '确认覆盖 1 个字段' }}</button>
        </div>
      </form>
    </div>
  </div>
</template>

<style scoped>
.data-quality-page { display: grid; gap: 14px; }
.access-badge { padding: 4px 10px; border-radius: 999px; background: #e5e7eb; color: #4b5563; font-size: 12px; }
.access-badge.writable { background: #dcfce7; color: #047857; }
.quality-tabs { display: flex; gap: 4px; border-bottom: 1px solid #d1d5db; overflow-x: auto; }
.quality-tabs button { border: 0; border-bottom: 2px solid transparent; background: transparent; color: #6b7280; padding: 10px 16px; white-space: nowrap; }
.quality-tabs button.active { border-bottom-color: #2563eb; color: #1d4ed8; font-weight: 600; }
.tab-panel { display: grid; gap: 12px; min-width: 0; }
.section-head { display: flex; align-items: center; justify-content: space-between; gap: 12px; flex-wrap: wrap; }
.section-head h2, .section-head h3, .dataset-title h3, .rule-editor h3, .rerun-editor h3 { margin: 0; }
.section-head label { display: flex; align-items: center; gap: 8px; font-size: 13px; }
.overview-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); gap: 14px; }
.dataset-card { display: grid; gap: 12px; }
.dataset-title { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; }
.dataset-title code { color: #6b7280; font-size: 11px; }
.status-pill { display: inline-block; padding: 2px 7px; border-radius: 999px; background: #e5e7eb; color: #4b5563; font-size: 12px; white-space: nowrap; }
.status-pill.finished { background: #dcfce7; color: #047857; }
.status-pill.failed, .status-pill.rejected { background: #fee2e2; color: #b91c1c; }
.status-pill.pending, .status-pill.running { background: #fef3c7; color: #92400e; }
.metrics { display: grid; grid-template-columns: repeat(7, minmax(60px, 1fr)); gap: 6px; }
.metrics > div { display: grid; gap: 2px; padding: 8px 4px; border-radius: 6px; background: #f3f4f6; text-align: center; }
.metrics strong { font-size: 17px; }
.metrics span { color: #6b7280; font-size: 11px; }
.issue-summary { margin: 0; color: #4b5563; font-size: 12px; }
.version { overflow-wrap: anywhere; }
.rule-editor, .rerun-editor { display: grid; gap: 14px; }
.form-grid { display: grid; grid-template-columns: repeat(4, minmax(150px, 1fr)); gap: 12px; }
.form-grid input, .form-grid select, textarea { width: 100%; border: 1px solid #d1d5db; border-radius: 6px; background: #fff; color: #1f2933; }
textarea { padding: 8px; font: inherit; resize: vertical; }
.params-field { grid-column: span 2; }
.params-field textarea { font-family: ui-monospace, SFMono-Regular, Consolas, monospace; font-size: 12px; }
.check-field { display: flex; align-items: center; gap: 6px; padding-top: 18px; }
.check-field input { width: auto; }
.form-actions { display: flex; align-items: center; justify-content: flex-end; gap: 10px; flex-wrap: wrap; }
.form-actions .hint { margin-right: auto; }
.table-wrap { width: 100%; overflow-x: auto; }
.json-cell { max-width: 280px; overflow-wrap: anywhere; }
.break-cell { max-width: 220px; overflow-wrap: anywhere; }
.actions-cell { min-width: 180px; }
.actions-cell .btn { margin: 2px; }
.pager { display: flex; justify-content: flex-end; gap: 8px; }
.quality-drawer { width: min(760px, 96vw); }
.quality-drawer .metrics { margin: 14px 0; }
.override-modal { display: grid; gap: 12px; width: min(520px, 92vw); }
.override-modal .modal-actions { margin-top: 4px; }
.rerun-grid { grid-template-columns: repeat(3, minmax(170px, 1fr)); }
.version-field { grid-column: span 2; }
.reason-field { grid-column: span 1; }
.dry-run-state { padding: 10px; border: 1px solid #fde68a; border-radius: 6px; background: #fffbeb; color: #92400e; font-size: 12px; }
.dry-run-state.ready { border-color: #a7f3d0; background: #ecfdf5; color: #047857; }
.rerun-detail pre { max-height: 320px; overflow: auto; padding: 10px; border-radius: 6px; background: #f3f4f6; white-space: pre-wrap; overflow-wrap: anywhere; }
@media (max-width: 900px) {
  .form-grid, .rerun-grid { grid-template-columns: repeat(2, minmax(140px, 1fr)); }
  .metrics { grid-template-columns: repeat(4, minmax(60px, 1fr)); }
}
@media (max-width: 560px) {
  .form-grid, .rerun-grid { grid-template-columns: 1fr; }
  .params-field, .version-field, .reason-field { grid-column: span 1; }
  .metrics { grid-template-columns: repeat(2, minmax(60px, 1fr)); }
}
</style>
