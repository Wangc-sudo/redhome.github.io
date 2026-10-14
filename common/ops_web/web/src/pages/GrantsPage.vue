<script setup>
/**
 * 授权现状（/grants，别名 /audit）——迁移自旧 SSR 页面（保真：app.py 旧 grants_page
 * + audit_page）。写面已停用，本页只读：
 *   GET /api/grants → {grants:[{user_id,grant_type,grant_type_label,grant_key,note,created_at}]}
 *   GET /api/audit  → {rows:[{created_at,actor,action,action_label,user_id,
 *                            grant_type,grant_type_label,grant_key,note}]}
 * ?user= 供 /records 深链而来，用于过滤/高亮某账号。
 */
import { computed, ref, watch } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import { getGrants, getAudit } from '../api.js';

const route = useRoute();
const router = useRouter();

const grants = ref([]);
const audit = ref([]);
const loading = ref(false);
const error = ref('');

const activeUser = computed(() => route.query.user || '');

const visibleGrants = computed(() => {
  const user = activeUser.value;
  return user ? grants.value.filter((g) => g.user_id === user) : grants.value;
});
const visibleAudit = computed(() => {
  const user = activeUser.value;
  return user ? audit.value.filter((r) => r.user_id === user) : audit.value;
});

async function load() {
  loading.value = true;
  error.value = '';
  try {
    const [grantsData, auditData] = await Promise.all([getGrants(), getAudit()]);
    grants.value = (grantsData && grantsData.grants) || [];
    audit.value = (auditData && auditData.rows) || [];
  } catch (err) {
    error.value = err.message;
    grants.value = [];
    audit.value = [];
  } finally {
    loading.value = false;
  }
}

watch(() => route.query.user, load, { immediate: true });

function clearUser() {
  router.replace({ query: {} });
}
</script>

<template>
  <div class="page">
    <h1>授权现状</h1>
    <p class="note">
      授权管理功能已停用，本页为只读视图。如需开通 BI 看板 / 区域数据 /
      名册管理权限，请联系系统管理员。
    </p>

    <p v-if="activeUser" class="user-filter">
      仅显示账号 <strong>{{ activeUser }}</strong> 的授权
      <button type="button" class="btn mini" @click="clearUser">清除</button>
    </p>

    <div v-if="error" class="state error">{{ error }}</div>
    <div v-else-if="loading" class="state">加载中…</div>

    <template v-else>
      <h2>授权现状</h2>
      <table>
        <tr>
          <th>账号</th><th>类型</th><th>对象</th><th>备注</th><th>更新时间</th>
        </tr>
        <tr v-for="(row, i) in visibleGrants" :key="i">
          <td>{{ row.user_id }}</td>
          <td>{{ row.grant_type_label }}</td>
          <td>{{ row.grant_key }}</td>
          <td>{{ row.note }}</td>
          <td>{{ row.created_at }}</td>
        </tr>
        <tr v-if="!visibleGrants.length">
          <td colspan="5" class="hint">暂无授权</td>
        </tr>
      </table>

      <h2>授权审计</h2>
      <table>
        <tr>
          <th>时间</th><th>操作人</th><th>动作</th><th>账号</th>
          <th>类型</th><th>对象</th><th>备注</th>
        </tr>
        <tr v-for="(row, i) in visibleAudit" :key="i">
          <td>{{ row.created_at }}</td>
          <td>{{ row.actor }}</td>
          <td>{{ row.action_label }}</td>
          <td>{{ row.user_id }}</td>
          <td>{{ row.grant_type_label }}</td>
          <td>{{ row.grant_key }}</td>
          <td>{{ row.note }}</td>
        </tr>
        <tr v-if="!visibleAudit.length">
          <td colspan="7" class="hint">暂无审计记录</td>
        </tr>
      </table>
    </template>
  </div>
</template>

<style scoped>
.user-filter {
  margin: 10px 0;
  padding: 6px 10px;
  border-radius: 6px;
  background: #eef4ff;
  font-size: 13px;
  color: #1f3a5f;
}
.user-filter .btn {
  margin-left: 8px;
}
</style>
