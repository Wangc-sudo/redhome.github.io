<script setup>
/**
 * 定时任务审计流水页 · Vue 迁移自旧 SSR /pipelines/audit。
 *
 * 读面 GET /api/pipelines/audit → rows[{created_at, actor, action, action_label,
 * service_id, service_name, detail}]；仅记录注册表变更（开关/新增）。
 */
import { onMounted, ref } from 'vue';
import { getPipelinesAudit } from '../api.js';

const loading = ref(true);
const loadError = ref('');
const rows = ref([]);

async function load() {
  loading.value = true;
  loadError.value = '';
  try {
    const data = await getPipelinesAudit();
    rows.value = (data && data.rows) || [];
  } catch (err) {
    loadError.value = err.message || '读取失败，请稍后重试';
    rows.value = [];
  } finally {
    loading.value = false;
  }
}

onMounted(load);
</script>

<template>
  <div class="page">
    <div class="head">
      <div class="head-main">
        <h1>定时任务审计流水</h1>
      </div>
      <div class="head-actions">
        <router-link class="btn" to="/pipelines">返回定时任务</router-link>
      </div>
    </div>

    <div v-if="loading" class="state">加载中…</div>
    <div v-else-if="loadError" class="state error">
      {{ loadError }}
      <div><button class="btn" @click="load()">重试</button></div>
    </div>

    <template v-else>
      <table>
        <thead>
          <tr>
            <th>时间</th><th>操作人</th><th>动作</th><th>服务标识</th><th>详情</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="(row, idx) in rows" :key="idx">
            <td>{{ row.created_at }}</td>
            <td>{{ row.actor }}</td>
            <td>{{ row.action_label }}</td>
            <td>
              <span>{{ row.service_name }}</span>
              <span v-if="row.service_name !== row.service_id" class="hint">（{{ row.service_id }}）</span>
            </td>
            <td>{{ row.detail }}</td>
          </tr>
        </tbody>
      </table>
      <p v-if="!rows.length" class="empty">暂无审计记录。</p>

      <p class="hint">
        本表只记注册表变更（开关/新增）；每次实际运行（定时与「立即运行一次」）见
        <router-link to="/pipelines/history">执行流水</router-link> 页。
        其他审计：<router-link to="/roster">填报名册</router-link>页底=名册变更流水、
        <router-link to="/grants">授权审计</router-link>=成员授权变更、
        <router-link to="/calendar">播报日历</router-link>页底=日历改判流水。
      </p>
    </template>
  </div>
</template>

<style scoped>
.page {
  max-width: 1080px;
}
</style>
