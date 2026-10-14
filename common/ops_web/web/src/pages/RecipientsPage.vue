<script setup>
/**
 * 收件人总览（旧 SSR /recipients 迁移，C 阶段；只读，无写面）。
 *
 * 数据源：GET /api/recipients → {status, rules_html, sections:[{scope,label,html}],
 * context_error}。rules_html 与各段 html 均为后端生成的 HTML 片段（v-html 渲染）。
 */
import { onMounted, ref } from 'vue';
import { getRecipients } from '../api.js';

const data = ref(null);
const loading = ref(true);
const error = ref('');

async function load() {
  loading.value = true;
  error.value = '';
  try {
    data.value = await getRecipients();
  } catch (e) {
    error.value = (e && e.message) || '加载失败';
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
        <h1>收件人总览（谁会被催办）</h1>
      </div>
    </div>

    <div v-if="loading" class="state">加载中…</div>
    <div v-else-if="error" class="state error">{{ error }}</div>
    <template v-else-if="data">
      <div class="flash err" v-if="data.context_error">
        名单读取失败（上下文加载异常）：以下为可展示的口径，请稍后重试。
      </div>
      <div class="rules" v-html="data.rules_html"></div>
      <p class="hint">
        本页只读、为展示口径：改名单请到「填报名册」，或到定时任务页对应任务的「收件人…」抽屉做单条微调。
      </p>
      <section v-for="sec in data.sections" :key="sec.scope">
        <h2>{{ sec.label }}</h2>
        <div v-html="sec.html"></div>
      </section>
    </template>
  </div>
</template>

<style scoped>
.rules {
  margin: 8px 0;
}
</style>
