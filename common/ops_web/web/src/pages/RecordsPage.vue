<script setup>
/**
 * 操作记录（/records）——迁移自旧 SSR 页面（保真：app.py 旧 records_page + _PAGE_JS）。
 *
 * 六源聚合审计读面 GET /api/records?source&actor&action&from&to（时间倒序，最多 200）。
 * 筛选变更即同步 ?query 并重拉；每行「记录回源深链」跳回各自写入面（spec §4.6）。
 */
import { ref, watch } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import { getRecords } from '../api.js';

const route = useRoute();
const router = useRouter();

//: 六源集合（顺序即下拉顺序）——与后端 _RECORD_SOURCES 一致，作兜底。
const DEFAULT_SOURCES = ['授权', '定时任务', '填报名册', '工作日历', '收件人覆盖', '定时运行'];

const records = ref([]);
const sources = ref(DEFAULT_SOURCES.slice());
const loading = ref(false);
const error = ref('');

const source = ref(route.query.source || '');
const actor = ref(route.query.actor || '');
const action = ref(route.query.action || '');
const from = ref(route.query.from || '');
const to = ref(route.query.to || '');

function syncFromQuery() {
  source.value = route.query.source || '';
  actor.value = route.query.actor || '';
  action.value = route.query.action || '';
  from.value = route.query.from || '';
  to.value = route.query.to || '';
}

async function load() {
  loading.value = true;
  error.value = '';
  try {
    const data = await getRecords({
      source: route.query.source || '',
      actor: route.query.actor || '',
      action: route.query.action || '',
      from: route.query.from || '',
      to: route.query.to || '',
    });
    records.value = (data && data.records) || [];
    if (data && data.sources && data.sources.length) sources.value = data.sources;
  } catch (err) {
    error.value = err.message;
    records.value = [];
  } finally {
    loading.value = false;
  }
}

watch(
  () => route.query,
  () => {
    syncFromQuery();
    load();
  },
  { immediate: true },
);

/** 把当前筛选同步进 ?query（空值略去）；触发 watch → 重拉。 */
function apply() {
  const query = {};
  if (source.value) query.source = source.value;
  if (actor.value) query.actor = actor.value;
  if (action.value) query.action = action.value;
  if (from.value) query.from = from.value;
  if (to.value) query.to = to.value;
  router.replace({ query });
}

function reset() {
  source.value = '';
  actor.value = '';
  action.value = '';
  from.value = '';
  to.value = '';
  router.replace({ query: {} });
}

/**
 * 每行「回源深链」（spec §4.6）：把该记录跳回其写入面。
 * 逐源取可辨识字段（target 首词 / grant 主体）拼路由。
 */
function deepLink(row) {
  const src = row.source || '';
  const target = (row.target || '').trim();
  const first = target.split(/\s+/)[0] || '';
  if (src === '授权') {
    // 记录行的 user_id（若后端下发）优先；否则退化为 grant_key（旧 handler 口径）。
    const user = row.user_id || '';
    return user ? { path: '/grants', query: { user } } : { path: '/grants' };
  }
  if (src === '填报名册') {
    return first ? { path: '/roster', query: { region: first } } : { path: '/roster' };
  }
  if (src === '工作日历') {
    const ym = target.slice(0, 7);
    return /^\d{4}-\d{2}$/.test(ym)
      ? { path: '/calendar', query: { month: ym } }
      : { path: '/calendar' };
  }
  if (src === '定时任务' || src === '定时运行' || src === '收件人覆盖') {
    return first ? { path: '/pipelines', query: { service: first } } : { path: '/pipelines' };
  }
  return null;
}
</script>

<template>
  <div class="page">
    <h1>操作记录</h1>

    <form class="inline" @submit.prevent="apply">
      <select v-model="source" @change="apply">
        <option value="">全部来源</option>
        <option v-for="s in sources" :key="s" :value="s">{{ s }}</option>
      </select>
      <input v-model="actor" placeholder="操作人（可空）" @change="apply">
      <input v-model="action" placeholder="动作（可空）" @change="apply">
      <input v-model="from" type="date" title="起始日期" @change="apply">
      <input v-model="to" type="date" title="结束日期" @change="apply">
      <button type="submit">筛选</button>
      <button type="button" class="btn ghost" @click="reset">重置</button>
    </form>

    <div v-if="error" class="state error">{{ error }}</div>
    <div v-else-if="loading" class="state">加载中…</div>

    <table v-else>
      <tr>
        <th>时间</th><th>来源</th><th>操作人</th><th>动作</th>
        <th>对象</th><th>详情</th><th>回源</th>
      </tr>
      <tr v-for="(row, i) in records" :key="i">
        <td>{{ row.created_at }}</td>
        <td>{{ row.source }}</td>
        <td>{{ row.actor }}</td>
        <td>{{ row.action }}</td>
        <td>{{ row.target }}</td>
        <td>{{ row.detail }}</td>
        <td>
          <router-link v-if="deepLink(row)" :to="deepLink(row)">去处理</router-link>
          <span v-else class="muted">—</span>
        </td>
      </tr>
      <tr v-if="!records.length">
        <td colspan="7" class="hint">暂无记录</td>
      </tr>
    </table>

    <p class="hint">
      汇总授权 / 定时任务变更 / 填报名册 / 工作日历 / 收件人覆盖五类人工操作，
      加定时运行流水；按时间倒序，最多 200 条。时间为北京时间。
    </p>
  </div>
</template>

<style scoped>
.state {
  margin-top: 16px;
}
</style>
