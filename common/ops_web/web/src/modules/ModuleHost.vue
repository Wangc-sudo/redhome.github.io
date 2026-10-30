<script setup>
import { computed } from 'vue';
import { useRoute } from 'vue-router';
import { getModule } from './registry.js';
import { resolveComponent } from './componentMap.js';

const route = useRoute();
const current = computed(() => getModule(route.meta.workspace, route.params.moduleKey));
const page = computed(() => (current.value ? resolveComponent(current.value.component) : null));
</script>

<template>
  <component :is="page" v-if="page" :key="route.fullPath" />
  <div v-else class="state error">模块不存在或已停用</div>
</template>
