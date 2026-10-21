<script setup>
import { computed, ref, watch } from 'vue';
import { me, isAdmin, hasCapability, hasAnyCapability, flash, clearFlash } from './store/me.js';

// 2026-10-16 P4：画布即应用——导航只留「闭环画布」；旧页面入口收进
// admin 可见的「更多」折叠组（过渡期回退通道，P4 观察期满后删路由）。
const who = computed(() => (me.value && me.value.name) || '');
const showLegacy = ref(false);

let flashTimer = null;
watch(flash, (value) => {
  if (flashTimer) clearTimeout(flashTimer);
  if (value) {
    flashTimer = setTimeout(() => {
      clearFlash();
    }, 4000);
  }
});
</script>

<template>
  <header>
    <strong>ops-web</strong>
    <!-- 2026-10-16 P4 画布即应用：导航只留画布（各页功能已收进画布节点/抽屉；
         旧路由保留可达——深链与外部引用不炸），admin 多一个「更多页面」入口。 -->
    <nav>
      <router-link v-if="hasCapability('org')" to="/flow">闭环画布</router-link>
      <router-link v-if="hasCapability('pipelines')" to="/broadcast">播报编排</router-link>
      <span v-if="showLegacy" class="nav-group legacy">
        <router-link v-if="hasCapability('roster')" to="/roster">名册</router-link>
        <router-link v-if="hasAnyCapability('org')" to="/org">组织</router-link>
        <router-link v-if="hasCapability('pipelines')" to="/pipelines">任务</router-link>
        <router-link v-if="hasCapability('calendar')" to="/calendar">日历</router-link>
        <router-link v-if="isAdmin()" to="/recipients">收件人</router-link>
        <router-link v-if="isAdmin()" to="/grants">授权</router-link>
        <router-link v-if="hasCapability('audit')" to="/records">记录</router-link>
      </span>
      <button
        v-if="isAdmin()"
        class="legacy-toggle"
        :title="showLegacy ? '收起旧页面入口' : '展开旧页面入口（过渡期）'"
        @click="showLegacy = !showLegacy"
      >{{ showLegacy ? '收起 ⌃' : '更多 ⌄' }}</button>
    </nav>
    <a href="/auth/logout">退出</a>
    <span class="who">{{ who }}</span>
  </header>

  <main>
    <transition name="flash">
      <div v-if="flash" class="flash" :class="flash.type">{{ flash.text }}</div>
    </transition>
    <slot />
  </main>
</template>

<style scoped>
.legacy-toggle {
  margin-left: 8px;
  padding: 2px 8px;
  border: 1px solid rgba(255, 255, 255, 0.35);
  border-radius: 6px;
  background: none;
  color: inherit;
  font-size: 12px;
  cursor: pointer;
}
.nav-group.legacy {
  display: inline-flex;
  gap: 10px;
  margin-left: 6px;
  padding-left: 10px;
  border-left: 1px solid rgba(255, 255, 255, 0.25);
}
</style>
