<script setup>
import { computed, onMounted, ref, watch } from 'vue';
import { useRoute } from 'vue-router';
import { getBusinessDataFreshness } from '../api.js';
import StatusBadge from '../components/ui/StatusBadge.vue';

const route = useRoute();
const items = ref([]);
const loading = ref(false);
const error = ref('');
const businessLine = computed(() => route.params.businessLine);
const labels = {
  daily_report_offline: '线下日报', channel_daily_sales: '渠道日销',
  wdt_order_line_fact: '订单数据', wdt_stockout_line_fact: '出库数据',
  wdt_refund_line_fact: '退款数据', wdt_inventory_sku_daily: '库存数据',
  ecommerce_monthly: '电商月报', restaurant_monthly: '餐饮月报',
  platform_bill_monthly: '平台账单',
};
const abnormalCount = computed(() => items.value.filter((item) => item.status !== 'fresh').length);

async function load() {
  loading.value = true;
  error.value = '';
  try {
    const data = await getBusinessDataFreshness(businessLine.value);
    items.value = (data && data.items) || [];
  } catch (err) {
    items.value = [];
    error.value = err.status === 403 ? '你暂无该业务线的数据状态查看权限' : (err.message || '数据到位情况读取失败');
  } finally {
    loading.value = false;
  }
}

function label(item) {
  return labels[item.dataset_key] || item.dataset_key;
}

watch(businessLine, load);
onMounted(load);
</script>

<template>
  <div class="page">
    <div class="head">
      <div class="head-main"><h1>数据到位情况</h1><p class="note">查看 {{ businessLine }} 业务数据的应到时间、负责人和异常提示。</p></div>
      <button class="btn" :disabled="loading" @click="load">{{ loading ? '刷新中…' : '刷新' }}</button>
    </div>
    <p v-if="!loading && !error && abnormalCount" class="business-alert">有 {{ abnormalCount }} 类数据需要关注，请联系对应负责人处理。</p>
    <div v-if="loading && !items.length" class="state">正在检查数据到位情况…</div>
    <div v-else-if="error" class="state error">{{ error }}</div>
    <div v-else-if="!items.length" class="state">暂无数据到位记录</div>
    <div v-else class="status-list">
      <article v-for="item in items" :key="item.dataset_key" class="card status-row">
        <div><strong>{{ label(item) }}</strong><p>负责人：{{ item.owner }}</p></div>
        <div><span>最新业务日期</span><strong>{{ item.latest_business_date || '尚未确认' }}</strong></div>
        <div><span>应到时间</span><strong>{{ item.expected_by }}（宽限 {{ item.max_lag_minutes }} 分钟）</strong></div>
        <StatusBadge :status="item.status" :labels="{ fresh: '已到位', degraded: '部分异常', stale: '未按时到位', blocked: '暂不可用', unknown: '待确认' }" />
      </article>
    </div>
  </div>
</template>

<style scoped>
.business-alert { padding: 10px 12px; color: #9a3412; background: #fff7ed; border: 1px solid #fed7aa; border-radius: 8px; }
.status-list { display: grid; gap: 10px; margin-top: 16px; }
.status-row { display: grid; grid-template-columns: minmax(180px, 1.4fr) 1fr 1.2fr auto; align-items: center; gap: 16px; }
.status-row p, .status-row span { margin: 4px 0 0; color: #6b7280; font-size: 12px; }
.status-row div > strong { display: block; margin-top: 4px; }
@media (max-width: 760px) { .status-row { grid-template-columns: 1fr 1fr; } }
</style>
