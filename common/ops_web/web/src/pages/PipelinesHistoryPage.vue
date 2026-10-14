<script setup>
/**
 * 定时任务执行流水页 · Vue 迁移自旧 SSR /pipelines/history。
 *
 * 读面 GET /api/pipelines/history?service_id=；支持 ?service_id= 过滤（写入路由
 * query 后重新拉取）。调度器每次实际点火（定时与「立即运行一次」同路径）落一行。
 */
import { ref, watch } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import { getPipelinesHistory } from '../api.js';

const route = useRoute();
const router = useRouter();

const loading = ref(true);
const loadError = ref('');
const rows = ref([]);
const filterInput = ref('');

async function load(serviceId) {
  loading.value = true;
  loadError.value = '';
  try {
    const data = await getPipelinesHistory(serviceId || undefined);
    rows.value = (data && data.rows) || [];
  } catch (err) {
    loadError.value = err.message || '读取失败，请稍后重试';
    rows.value = [];
  } finally {
    loading.value = false;
  }
}

function applyFilter() {
  const value = filterInput.value.trim();
  router.push({ path: '/pipelines/history', query: value ? { service_id: value } : {} });
}

watch(
  () => route.query.service_id,
  (value) => {
    filterInput.value = typeof value === 'string' ? value : '';
    load(filterInput.value);
  },
  { immediate: true },
);
</script>

<template>
  <div class="page">
    <div class="head">
      <div class="head-main">
        <h1>定时任务执行流水</h1>
      </div>
      <div class="head-actions">
        <router-link class="btn" to="/pipelines">返回定时任务</router-link>
      </div>
    </div>

    <form class="inline" @submit.prevent="applyFilter">
      <input v-model="filterInput" placeholder="按服务标识过滤（可空）">
      <button type="submit">过滤</button>
    </form>

    <div v-if="loading" class="state">加载中…</div>
    <div v-else-if="loadError" class="state error">
      {{ loadError }}
      <div><button class="btn" @click="load(filterInput.trim())">重试</button></div>
    </div>

    <template v-else>
      <table>
        <thead>
          <tr>
            <th>开始时间</th><th>服务标识</th><th>触发</th><th>状态</th>
            <th>退出码</th><th>结束时间</th><th>备注</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="(row, idx) in rows" :key="idx">
            <td>{{ row.started_at }}</td>
            <td>
              <span>{{ row.service_name }}</span>
              <span v-if="row.service_name !== row.service_id" class="hint">（{{ row.service_id }}）</span>
            </td>
            <td>{{ row.trigger_label }}</td>
            <td>{{ row.status_label }}</td>
            <td>{{ row.exit_code === null || row.exit_code === undefined ? '—' : row.exit_code }}</td>
            <td>{{ row.finished_at }}</td>
            <td>{{ row.note }}</td>
          </tr>
        </tbody>
      </table>
      <p v-if="!rows.length" class="empty">暂无执行流水。</p>

      <p class="hint">
        调度器每次实际点火（定时与「立即运行一次」同路径）落一行；锁被占用/运行异常
        记为失败且无退出码。依赖未满足的暂缓与非法 cron 不是运行，只走调度器日志。
        时间为北京时间。
      </p>
    </template>
  </div>
</template>

<style scoped>
.page {
  max-width: 1080px;
}
</style>
