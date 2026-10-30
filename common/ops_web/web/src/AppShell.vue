<script setup>
import { computed, ref, watch } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import { me, flash, clearFlash } from './store/me.js';
import {
  availableBusinessLines,
  getModule,
  modulePath,
  preferredBusinessLine,
  visibleModules,
} from './modules/registry.js';

const route = useRoute();
const router = useRouter();
const who = computed(() => (me.value && me.value.name) || '');
const businessModules = computed(() => visibleModules('business'));
const adminModules = computed(() => visibleModules('admin'));
const workspace = computed(() => route.meta.workspace || null);
const currentLine = computed(() => route.params.businessLine || preferredBusinessLine());
const businessLines = computed(() => {
  const keys = new Set();
  for (const item of businessModules.value) {
    for (const key of availableBusinessLines(item)) keys.add(key);
  }
  return [...keys];
});
const navigation = computed(() => {
  if (!workspace.value) return [];
  const items = workspace.value === 'business'
    ? visibleModules('business', currentLine.value)
    : adminModules.value;
  const groups = new Map();
  for (const item of items) {
    if (!groups.has(item.nav_group)) groups.set(item.nav_group, []);
    groups.get(item.nav_group).push(item);
  }
  return [...groups.entries()].map(([title, modules]) => ({ title, modules }));
});

function workspacePath(target) {
  const items = target === 'business' ? businessModules.value : adminModules.value;
  for (const item of items) {
    const path = modulePath(item, target === 'business' ? currentLine.value : null);
    if (path) return path;
  }
  return '/';
}

async function changeBusinessLine(event) {
  const line = event.target.value;
  const current = getModule('business', route.params.moduleKey);
  let destination = current && visibleModules('business', line).includes(current)
    ? modulePath(current, line)
    : null;
  if (!destination) {
    const first = visibleModules('business', line)[0];
    destination = first ? modulePath(first, line) : null;
  }
  if (destination) await router.push(destination);
}

let flashTimer = null;
watch(flash, (value) => {
  if (flashTimer) clearTimeout(flashTimer);
  if (value) flashTimer = setTimeout(clearFlash, 4000);
});
</script>

<template>
  <header class="app-header">
    <strong>ops-web</strong>
    <nav class="workspaces" aria-label="工作区">
      <router-link v-if="businessModules.length" :to="workspacePath('business')">业务工作台</router-link>
      <router-link v-if="adminModules.length" :to="workspacePath('admin')">系统后台</router-link>
    </nav>
    <label v-if="workspace === 'business' && businessLines.length" class="line-picker">
      <span>业务线</span>
      <select :value="currentLine" @change="changeBusinessLine">
        <option v-for="line in businessLines" :key="line" :value="line">{{ line }}</option>
      </select>
    </label>
    <a class="logout" href="/auth/logout">退出</a>
    <span class="who">{{ who }}</span>
  </header>

  <nav v-if="navigation.length" class="module-nav" aria-label="模块导航">
    <section v-for="group in navigation" :key="group.title">
      <span class="nav-label">{{ group.title }}</span>
      <router-link
        v-for="item in group.modules"
        :key="item.module_key"
        :to="modulePath(item, workspace === 'business' ? currentLine : null)"
      >{{ item.title }}</router-link>
    </section>
  </nav>

  <main>
    <transition name="flash">
      <div v-if="flash" class="flash" :class="flash.type">{{ flash.text }}</div>
    </transition>
    <slot />
  </main>
</template>

<style scoped>
.workspaces a {
  padding: 5px 10px;
  border-radius: 6px;
}
.workspaces a.router-link-active {
  background: rgba(255, 255, 255, 0.14);
}
.line-picker {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  color: #cfe0f5;
}
.line-picker select {
  max-width: 150px;
}
.logout {
  margin-left: auto;
}
.module-nav {
  display: flex;
  gap: 24px;
  flex-wrap: wrap;
  padding: 10px 24px;
  background: #fff;
  border-bottom: 1px solid #e3e6ea;
}
.module-nav section {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}
.module-nav a {
  color: #4b5563;
  font-size: 13px;
  text-decoration: none;
  padding: 4px 0;
}
.module-nav a.router-link-active {
  color: #1d4ed8;
  font-weight: 600;
}
@media (max-width: 760px) {
  .app-header { padding: 10px 14px; gap: 12px; }
  .module-nav { padding: 10px 14px; gap: 12px; }
  .who { display: none; }
}
</style>
