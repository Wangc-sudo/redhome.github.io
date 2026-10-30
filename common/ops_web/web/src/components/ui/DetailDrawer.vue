<script setup>
defineProps({
  title: { type: String, required: true },
  loading: { type: Boolean, default: false },
  error: { type: String, default: '' },
});
const emit = defineEmits(['close']);
</script>

<template>
  <div class="drawer-mask" @click.self="emit('close')">
    <aside class="detail-drawer" role="dialog" aria-modal="true" :aria-label="title">
      <div class="drawer-head">
        <h2>{{ title }}</h2>
        <button class="btn ghost" aria-label="关闭详情" @click="emit('close')">关闭</button>
      </div>
      <div v-if="loading" class="state">详情加载中…</div>
      <div v-else-if="error" class="state error">{{ error }}</div>
      <div v-else class="drawer-body"><slot /></div>
    </aside>
  </div>
</template>

<style scoped>
.drawer-mask {
  position: fixed;
  inset: 0;
  z-index: 30;
  background: rgba(15, 23, 42, 0.35);
}
.detail-drawer {
  position: absolute;
  top: 0;
  right: 0;
  width: min(680px, 94vw);
  height: 100%;
  overflow-y: auto;
  background: #f8fafc;
  box-shadow: -8px 0 24px rgba(15, 23, 42, 0.15);
}
.drawer-head {
  position: sticky;
  top: 0;
  z-index: 1;
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 16px 20px;
  background: #fff;
  border-bottom: 1px solid #e5e7eb;
}
.drawer-head h2 { margin: 0; }
.drawer-body { padding: 16px 20px 32px; }
</style>
