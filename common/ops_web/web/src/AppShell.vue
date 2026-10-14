<script setup>
import { computed, watch } from 'vue';
import { me, isAdmin, hasCapability, flash, clearFlash } from './store/me.js';

// 顶栏 nav 单一真源：分组与顺序照 app.py `_page()`（日常/权限/系统/记录），
// 加「总览」组；「授权」归「权限」组。导航项按能力裁剪（Overview 恒显示）。
const who = computed(() => (me.value && me.value.name) || '');

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
    <nav>
      <span class="nav-group">
        <span class="nav-label">总览</span>
        <router-link to="/">总览</router-link>
      </span>
      <span class="nav-group">
        <span class="nav-label">日常</span>
        <router-link v-if="hasCapability('roster')" to="/roster">填报名册</router-link>
        <router-link v-if="isAdmin()" to="/recipients">收件人总览</router-link>
        <router-link v-if="hasCapability('calendar')" to="/calendar">工作日历</router-link>
      </span>
      <span class="nav-group">
        <span class="nav-label">权限</span>
        <router-link v-if="hasCapability('org')" to="/org">组织编排</router-link>
        <router-link v-if="hasCapability('org')" to="/flow">闭环画布</router-link>
        <router-link v-if="isAdmin()" to="/grants">授权</router-link>
      </span>
      <span class="nav-group">
        <span class="nav-label">系统</span>
        <router-link v-if="hasCapability('pipelines')" to="/pipelines">定时任务</router-link>
      </span>
      <span class="nav-group">
        <span class="nav-label">记录</span>
        <router-link v-if="hasCapability('audit')" to="/records">操作记录</router-link>
      </span>
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
