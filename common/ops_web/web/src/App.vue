<script setup>
import { onMounted } from 'vue';
import AppShell from './AppShell.vue';
import './styles.css';
import { loadMe, me } from './store/me.js';

// 启动拉 me（router 守卫通常已拉，这里仅兜底）；失败 401/403 → 跳登录。
onMounted(() => {
  if (!me.value) {
    loadMe().catch((err) => {
      if (err && (err.status === 401 || err.status === 403)) {
        window.location.assign('/auth/entry');
      }
    });
  }
});
</script>

<template>
  <AppShell><router-view /></AppShell>
</template>
