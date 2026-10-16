<script setup>
import { computed } from 'vue';
import { hasCapability, isAdmin, me } from '../store/me.js';

const who = computed(() => (me.value && me.value.name) || '');

// 入口卡片：按能力裁剪（顺序照 AppShell 顶栏「日常 / 权限 / 系统 / 记录」）。
const entries = computed(() => {
  const cards = [];
  if (hasCapability('org')) {
    cards.push({
      to: '/org',
      title: '组织编排',
      desc: '核对在职成员归属：拖拽把成员归入区域/部门，登记区域目录，管理消费侧覆盖（不改钉钉组织）。',
    });
  }
  if (hasCapability('roster')) {
    cards.push({
      to: '/roster',
      title: '填报名册',
      desc: '维护区域名册项与个人月目标，发布月度目标快照，供催办与报表取数。',
    });
  }
  if (hasCapability('calendar')) {
    cards.push({
      to: '/calendar',
      title: '播报日历',
      desc: '默认每天播报；逐日人工裁决播报/休息，修正催办节奏。',
    });
  }
  if (hasCapability('pipelines')) {
    cards.push({
      to: '/pipelines',
      title: '定时任务',
      desc: '查看并启停六源播报任务，调整播报时间、依赖与收件人覆盖。',
    });
  }
  if (hasCapability('audit')) {
    cards.push({
      to: '/records',
      title: '操作记录',
      desc: '六源聚合审计读面：按来源/操作人/动作/时间追溯每一次变更。',
    });
  }
  if (isAdmin()) {
    cards.push({
      to: '/recipients',
      title: '收件人总览',
      desc: '查看各服务线基名单、覆盖与归因，核对播报是否送达正确的人。',
    });
    cards.push({
      to: '/grants',
      title: '授权',
      desc: '分配能力×区域授权，按区域一键开通，查看授权现状与审计。',
    });
  }
  return cards;
});

// 待办/提示：纯静态引导文案 + 深链（无新 API）。
const hints = computed(() => [
  {
    text: '去组织编排核对新成员归属，把未归类池里的成员拖入对应区域/部门。',
    to: '/org',
    label: '组织编排',
    show: hasCapability('org'),
  },
  {
    text: '去名册录入本月区域目标，并发布月度目标快照。',
    to: '/roster',
    label: '填报名册',
    show: hasCapability('roster'),
  },
  {
    text: '检查播报日历：确认本月人工裁决的休息日，避免催办节奏错位。',
    to: '/calendar',
    label: '播报日历',
    show: hasCapability('calendar'),
  },
  {
    text: '巡检定时任务：确认关键播报任务处于启用状态、播报时间正确。',
    to: '/pipelines',
    label: '定时任务',
    show: hasCapability('pipelines'),
  },
  {
    text: '复核操作记录：按来源/操作人回溯近期变更，确认无异常写入。',
    to: '/records',
    label: '操作记录',
    show: hasCapability('audit'),
  },
]);
</script>

<template>
  <div class="page">
    <div class="head">
      <div class="head-main">
        <h1>总览</h1>
        <p class="note">
          按你的角色与区域授权展示可用入口<template v-if="who">（{{ who }}）</template>，
          点卡片进入对应工作台。
        </p>
      </div>
    </div>

    <div v-if="!entries.length" class="empty">
      当前账号没有任何已授权的工作台，请联系管理员开通能力×区域授权。
    </div>

    <div v-else class="cards">
      <router-link
        v-for="card in entries"
        :key="card.to"
        class="card entry"
        :to="card.to"
      >
        <h2 class="entry-title">{{ card.title }}</h2>
        <p class="entry-desc">{{ card.desc }}</p>
        <span class="entry-go">进入 →</span>
      </router-link>
    </div>

    <section class="todo">
      <h2 class="todo-title">待办 / 提示</h2>
      <ul class="todo-list">
        <template v-for="hint in hints" :key="hint.to">
          <li v-if="hint.show" class="todo-item">
            <span class="todo-text">{{ hint.text }}</span>
            <router-link class="btn mini" :to="hint.to">{{ hint.label }}</router-link>
          </li>
        </template>
      </ul>
    </section>
  </div>
</template>

<style scoped>
.page {
  max-width: 1080px;
  margin: 0 auto;
}

.cards {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(260px, 1fr));
  gap: 14px;
  margin-top: 16px;
}

/* 入口卡片 */
.entry {
  display: flex;
  flex-direction: column;
  gap: 6px;
  text-decoration: none;
  color: inherit;
  transition: border-color 0.15s ease, box-shadow 0.15s ease;
}
.entry:hover {
  border-color: #93c5fd;
  box-shadow: 0 4px 14px rgba(37, 99, 235, 0.1);
}
.entry-title {
  margin: 0;
  font-size: 15px;
  color: #1f2933;
}
.entry-desc {
  margin: 0;
  color: #6b7280;
  font-size: 12px;
  line-height: 1.6;
}
.entry-go {
  margin-top: auto;
  padding-top: 4px;
  color: #2563eb;
  font-size: 12px;
}

/* 待办 / 提示 */
.todo {
  margin-top: 28px;
}
.todo-title {
  margin: 0 0 10px;
  font-size: 15px;
}
.todo-list {
  margin: 0;
  padding: 12px;
  list-style: none;
  border: 1px solid #e5e7eb;
  border-radius: 10px;
  background: #fff;
  display: grid;
  gap: 10px;
}
.todo-item {
  display: flex;
  align-items: center;
  gap: 12px;
}
.todo-text {
  flex: 1;
  color: #1f2933;
  font-size: 13px;
  line-height: 1.5;
}
.todo-item .btn {
  text-decoration: none;
  white-space: nowrap;
}
</style>
