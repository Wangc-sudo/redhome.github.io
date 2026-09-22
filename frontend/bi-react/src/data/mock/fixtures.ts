/**
 * Mock 后端（VITE_MOCK=1 生效）
 * ---------------------------------------------------------------------------
 * 用途：后端未启动也能开发/演示；同时给单测提供稳定的 payload → CubeSchema 样本。
 *
 * 这里的 payload **严格用后端真实响应结构**（2026-09-15 抓自 http://127.0.0.1:18080），
 * 再统一经 adapter/cardToCube.ts 归一成 CubeSchema —— 保证 mock 与真实链路同构，
 * 不会出现「mock 能跑、真机翻车」。
 *
 * cube 字段名（target/done/shortfall/severity…）沿用 CubeSchema.md §4 与后端真实下发，
 * 缺口/告警口径一律由后端算好，前端只透传渲染。
 */

import { BiWebError } from '../errors';
import type { CardPayload, ChartKind, DashboardDefinition, DashboardSummary, FilterDef } from '../types';

const OPTIONS: Record<string, string[]> = {
  regions: ['杭州', '绍兴'],
  channels: ['直播', '猫超', '拼多多', '天猫', '京东', '即时零售', '私域'],
  months: ['2026-09', '2026-08'],
  brands: ['习酒', '金钻'],
  sku_channels: ['天猫', '京东', '拼多多'],
  // 占位页筛选项：数据源未接入 → 候选为空数组，FilterBar 渲染禁用态（T6 裁决）
  warehouses: [],
};

interface CardSpec {
  card: string;
  title: string;
  chart: ChartKind;
  span: number;
  params?: string[];
}

interface MockDashboard {
  title: string;
  refresh_seconds: number;
  filters?: FilterDef[];
  cards: CardSpec[];
  payloads: Record<string, CardPayload>;
}

/* 注：工作日字段（total/elapsed/remaining_workdays）由后端 dim_calendar 下发，
 * mock 里只在 kpi_shortfall / anomaly_top 行内按真机结构带 elapsed/remaining 两项
 * （真机就没有 total；这两张卡的 required_daily 也是后端给的）。前端零推算。 */

/* ------------------------------------------------------------- l1-cockpit */

const l1: MockDashboard = {
  title: '首页驾驶舱',
  refresh_seconds: 86400,
  filters: [],
  cards: [
    { card: 'kpi_offline_mtd', title: '线下本月累计销售', chart: 'scalar', span: 4 },
    { card: 'kpi_channel_mtd', title: '电商渠道本月累计销售', chart: 'scalar', span: 4 },
    { card: 'kpi_annual_progress', title: '年度目标达成进度', chart: 'scalar', span: 4 },
    { card: 'trend_region_daily', title: '区域日销趋势', chart: 'line', span: 8, params: ['region', 'month'] },
    { card: 'bar_channel_mtd', title: '渠道本月排行', chart: 'bar', span: 4, params: ['month'] },
    { card: 'table_channel_mtd', title: '渠道本月对比', chart: 'table', span: 12, params: ['month'] },
    { card: 'pie_sku_mtd', title: '电商 SKU 本月销售占比', chart: 'pie', span: 6 },
    // 派生口径卡（CubeSchema §2）：缺口/所需日均/四级告警**后端已算**，前端只渲染
    { card: 'anomaly_top', title: '今日跟进（缺口 TOP10）', chart: 'table', span: 6, params: ['month'] },
    { card: 'kpi_shortfall', title: '目标缺口与告警', chart: 'table', span: 6, params: ['region', 'month'] },
    { card: 'kpi_offline_dod', title: '线下日环比', chart: 'scalar', span: 3 },
    { card: 'kpi_channel_dod', title: '电商渠道日环比', chart: 'scalar', span: 3 },
  ],
  payloads: {
    kpi_offline_mtd: { chart: 'scalar', value: 4534023.0, unit: '元' },
    kpi_channel_mtd: { chart: 'scalar', value: 21762753.02, target: 760210000.0, rate: 0.028627291169545255, unit: '元' },
    kpi_annual_progress: { chart: 'scalar', value: 21762753.02, target: 760210000.0, rate: 0.028627291169545255, unit: '元' },
    trend_region_daily: {
      chart: 'line',
      dates: ['09-01', '09-02', '09-03', '09-04', '09-05', '09-07', '09-08', '09-09', '09-10', '09-11', '09-12', '09-14'],
      series: [
        {
          name: '杭州',
          data: [194820.0, 97965.0, 79004.0, 247094.0, 309572.0, 252754.0, 457012.0, 547976.0, 315482.0, 243923.0, 631544.0, 0.0],
        },
        {
          name: '绍兴',
          data: [38564.0, 10000.0, 31150.0, 16466.0, 120825.0, 117688.0, 36150.0, 65111.0, 133147.0, 451812.0, 121744.0, 14220.0],
        },
      ],
      unit: '元',
    },
    bar_channel_mtd: {
      chart: 'bar',
      categories: ['直播', '猫超', '拼多多', '天猫', '京东', '即时零售', '私域'],
      values: [5619927.19, 4640041.08, 2728317.16, 1407564.97, 1042892.17, 951590.0, 67469.0],
      unit: '元',
    },
    table_channel_mtd: {
      chart: 'table',
      columns: [
        { key: 'channel', title: '渠道' },
        { key: 'sales', title: '本月销售额', format: 'wan' },
        { key: 'promo', title: '推广费', format: 'wan' },
        { key: 'roi', title: 'ROI', format: 'ratio' },
        { key: 'stores', title: '店铺数', format: 'int' },
      ],
      rows: [
        { channel: '直播', sales: 5619927.19, promo: 538256.98, roi: 10.440974104971199, stores: 5 },
        { channel: '猫超', sales: 4640041.08, promo: 42414.84, roi: 109.39664230726794, stores: 0 },
        { channel: '拼多多', sales: 2728317.16, promo: 13280.77, roi: 205.43365783761033, stores: 10 },
        { channel: '天猫', sales: 1407564.97, promo: 35375.5, roi: 39.7892600811296, stores: 6 },
        { channel: '京东', sales: 1042892.17, promo: 21626.08, roi: 48.22381911099931, stores: 5 },
        { channel: '即时零售', sales: 951590.0, promo: null, roi: null, stores: 4 },
        { channel: '私域', sales: 67469.0, promo: null, roi: null, stores: 1 },
      ],
    },
    pie_sku_mtd: {
      chart: 'pie',
      items: [
        { name: '53°1.5l*2习酒古韵繁体珍藏级（2021产）', value: 2561174.7843 },
        { name: '53°500ml*6习酒窖藏1988（2025版2026产）', value: 1957076.0357 },
        { name: '53°500ml*6习酒君品2024版（2026产）', value: 1887906.0809 },
        { name: '其他', value: 7716633.1495 },
      ],
      unit: '元',
    },
    // 与后端 queries.run_kpi_shortfall / run_anomaly_top 同结构（severity 由后端算）
    kpi_shortfall: {
      chart: 'table',
      unit: '元',
      month: '2026-09',
      // 后端默认 grain=person（真机抓取），列里带「部门」
      grain: 'person',
      as_of: '2026-09-15',
      severity_domain: ['p0', 'p1', 'p2', 'ok'],
      columns: [
        { key: 'name', title: '姓名' },
        { key: 'dept', title: '部门' },
        { key: 'target', title: '月目标', format: 'wan' },
        { key: 'done', title: '已完成', format: 'wan' },
        { key: 'shortfall', title: '缺口', format: 'wan' },
        { key: 'rate', title: '完成率', format: 'percent' },
        { key: 'required_daily', title: '所需日均', format: 'wan' },
        { key: 'severity', title: '告警', format: 'severity' },
      ],
      rows: [
        {
          name: '李树军', dept: '线下运营中心', target: 1030000.0, done: 60454.0, shortfall: 969546.0,
          rate: 0.058693203883495144, required_daily: 42916.666666666664, severity: 'p1',
          remaining_workdays: 11, elapsed_workdays: 13, has_fact: true,
        },
        {
          name: '卢炳华', dept: '滨萧', target: 1019000.0, done: 225845.0, shortfall: 793155.0,
          rate: 0.22163395485770362, required_daily: 42458.333333333336, severity: 'p1',
          remaining_workdays: 11, elapsed_workdays: 13, has_fact: true,
        },
        {
          // 无目标 → 缺口不可算（CubeSchema §5 显式化）
          name: '陈杰', dept: '杭中', target: null, done: 620000.0, shortfall: null, rate: null,
          required_daily: null, severity: 'ok', remaining_workdays: 11, elapsed_workdays: 13, has_fact: true,
        },
        {
          // 演示「挂零」诊断角标（非真机抓取，形状按 golden row_cases.row_no_fact_with_target_is_also_p0）：
          // has_fact=false = 本月截至今日无销单行；裁决 #3：后端仍判 p0，字段只作诊断。
          // 前端原样透传 p0 chip + 数值，额外渲染中性「挂零」角标，角标不参与判定。
          name: '演示·挂零', dept: '绍兴一部', target: 240000.0, done: 0.0, shortfall: 240000.0,
          rate: 0.0, required_daily: 10000.0, severity: 'p0',
          remaining_workdays: 11, elapsed_workdays: 13, has_fact: false,
        },
      ],
    },
    anomaly_top: {
      chart: 'table',
      unit: '元',
      month: '2026-09',
      grain: 'person',
      as_of: '2026-09-15',
      severity_domain: ['p0', 'p1', 'p2', 'ok'],
      limit: 10,
      columns: [
        { key: 'rank', title: '名次' },
        { key: 'name', title: '姓名' },
        { key: 'dept', title: '部门' },
        { key: 'target', title: '月目标', format: 'wan' },
        { key: 'done', title: '已完成', format: 'wan' },
        { key: 'shortfall', title: '缺口', format: 'wan' },
        { key: 'rate', title: '完成率', format: 'percent' },
        { key: 'required_daily', title: '所需日均', format: 'wan' },
        { key: 'severity', title: '告警', format: 'severity' },
      ],
      rows: [
        {
          rank: 1, name: '李树军', dept: '线下运营中心', target: 1030000.0, done: 60454.0,
          shortfall: 969546.0, rate: 0.058693203883495144, required_daily: 42916.666666666664,
          severity: 'p1', remaining_workdays: 11, elapsed_workdays: 13, has_fact: true,
        },
        {
          rank: 2, name: '卢炳华', dept: '滨萧', target: 1019000.0, done: 225845.0,
          shortfall: 793155.0, rate: 0.22163395485770362, required_daily: 42458.333333333336,
          severity: 'p1', remaining_workdays: 11, elapsed_workdays: 13, has_fact: true,
        },
      ],
    },
    kpi_offline_dod: {
      chart: 'scalar',
      value: 14220.0,
      date: '2026-09-14',
      prev: null,
      delta_pct: null,
      trend7: [
        { date: '2026-09-08', value: 493162.0 },
        { date: '2026-09-09', value: 613087.0 },
        { date: '2026-09-10', value: 448629.0 },
        { date: '2026-09-11', value: 695735.0 },
        { date: '2026-09-12', value: 753288.0 },
        { date: '2026-09-13', value: null },
        { date: '2026-09-14', value: 14220.0 },
      ],
      unit: '元',
    },
    kpi_channel_dod: { chart: 'scalar', value: 0.0, date: '2026-09-14', prev: 1321470.0, delta_pct: -100, unit: '元' },
  },
};

/* -------------------------------------------------------------- l2-region */

const regionFilters: FilterDef[] = [
  { param: 'region', source: 'regions', label: '区域' },
  { param: 'month', source: 'months', label: '月份' },
];

const l2Region: MockDashboard = {
  title: '区域下钻',
  refresh_seconds: 86400,
  filters: regionFilters,
  cards: [
    { card: 'kpi_region_mtd', title: '区域本月累计销售', chart: 'scalar', span: 4, params: ['region', 'month'] },
    { card: 'trend_region_daily', title: '区域日销趋势', chart: 'line', span: 8, params: ['region', 'month'] },
    { card: 'bar_department_mtd', title: '部门本月排行', chart: 'bar', span: 12, params: ['region', 'month'] },
  ],
  payloads: {
    kpi_region_mtd: { chart: 'scalar', value: 26298400.0, target: 32000000.0, rate: 0.821825, unit: '元' },
    trend_region_daily: l1.payloads.trend_region_daily,
    bar_department_mtd: {
      chart: 'bar',
      categories: ['杭州一部', '杭州二部', '绍兴一部', '绍兴二部', '绍兴三部'],
      values: [10430000.0, 8215000.0, 4120000.0, 2354000.0, 1189400.0],
      unit: '元',
    },
  },
};

/* ------------------------------------------------------------- l2-channel */

const channelFilters: FilterDef[] = [{ param: 'month', source: 'months', label: '月份' }];

const l2Channel: MockDashboard = {
  title: '渠道明细',
  refresh_seconds: 86400,
  filters: channelFilters,
  cards: [
    { card: 'kpi_channel_mtd', title: '电商渠道本月累计销售', chart: 'scalar', span: 4, params: ['month'] },
    { card: 'bar_channel_mtd', title: '渠道本月排行', chart: 'bar', span: 8, params: ['month'] },
    { card: 'table_channel_mtd', title: '渠道本月对比', chart: 'table', span: 12, params: ['month'] },
  ],
  payloads: {
    kpi_channel_mtd: l1.payloads.kpi_channel_mtd,
    bar_channel_mtd: l1.payloads.bar_channel_mtd,
    table_channel_mtd: l1.payloads.table_channel_mtd,
  },
};

/* ------------------------------------------------------------- l2-product */

const l2Product: MockDashboard = {
  title: '商品动销',
  refresh_seconds: 86400,
  filters: channelFilters,
  cards: [
    { card: 'pie_sku_mtd', title: '电商 SKU 本月销售占比', chart: 'pie', span: 6 },
    { card: 'table_sku_mtd', title: 'SKU 动销明细', chart: 'table', span: 12 },
  ],
  payloads: {
    pie_sku_mtd: l1.payloads.pie_sku_mtd,
    table_sku_mtd: {
      chart: 'table',
      columns: [
        { key: 'sku', title: 'SKU', format: 'text' },
        { key: 'sales', title: '本月销售额', format: 'wan' },
        { key: 'qty', title: '销量', format: 'int' },
        { key: 'stores', title: '动销店铺', format: 'int' },
      ],
      rows: [
        { sku: '53°1.5l*2习酒古韵繁体珍藏级（2021产）', sales: 2561174.78, qty: 1280, stores: 6 },
        { sku: '53°500ml*6习酒窖藏1988（2025版2026产）', sales: 1957076.04, qty: 2310, stores: 9 },
        { sku: '53°500ml*6习酒君品2024版（2026产）', sales: 1887906.08, qty: 980, stores: 4 },
        { sku: '53°500ml*2*3习酒金钻（双瓶礼盒）', sales: 898175.72, qty: 1502, stores: 7 },
        { sku: '53°500ml*6金钻习酒新版', sales: 733168.61, qty: 1104, stores: 3 },
      ],
    },
  },
};

/* -------------------------------------------------------------- l2-people */

// 人员榜：卡 id / 列结构**与后端注册表一致**（common/bi_web/cards.py +
// queries.run_table_people_leaderboard），避免出现「mock 能跑、真机翻车」。
// 注意：这张卡后端不下发 severity 与 shortfall，前端也不自造 —— 告警只出现在
// kpi_shortfall / anomaly_top（后端 derived.py 产出，原样透传）。
const l2People: MockDashboard = {
  title: '人员榜',
  refresh_seconds: 86400,
  filters: regionFilters,
  cards: [
    {
      card: 'table_people_leaderboard',
      title: '人员销售榜',
      chart: 'table',
      span: 12,
      params: ['region', 'month'],
    },
  ],
  payloads: {
    table_people_leaderboard: {
      chart: 'table',
      columns: [
        { key: 'rank', title: '排名' },
        { key: 'name', title: '姓名' },
        { key: 'dept', title: '部门', format: 'text' },
        { key: 'completed', title: '完成额', format: 'wan' },
        { key: 'target', title: '月目标', format: 'wan' },
        { key: 'rate', title: '达成率', format: 'percent' },
        { key: 'unfilled', title: '未完成缺口' },
      ],
      rows: [
        { rank: 1, name: '张伟', dept: '杭州一部', completed: 0, target: 800000, rate: 0.0, unfilled: 13 },
        { rank: 2, name: '李娜', dept: '杭州一部', completed: 120000, target: 800000, rate: 0.15, unfilled: 11 },
        { rank: 3, name: '王强', dept: '杭州二部', completed: 540000, target: 600000, rate: 0.9, unfilled: 2 },
        { rank: 4, name: '赵敏', dept: '绍兴一部', completed: 500000, target: 500000, rate: 1.0, unfilled: 0 },
        { rank: 5, name: '陈杰', dept: '绍兴二部', completed: 620000, target: 400000, rate: 1.55, unfilled: 0 },
        // 无事实行：completed 为 null → 「—」（§5 数据缺陷显式化，不静默补 0）
        { rank: 6, name: '周涛', dept: '绍兴三部', completed: null, target: 300000, rate: null, unfilled: 13 },
      ],
    },
  },
};

/* ----------------------------------------------------- l2-fund-safety（资金安全） */
/* V1 新增真卡页（⑩）。行为对齐后端 seed：全页 refresh_seconds=86400（T+1）。
 * 下方行为演示结构（非真机抓取），severity 由后端算好、前端只透传。 */
const l2FundSafety: MockDashboard = {
  title: '资金安全',
  refresh_seconds: 86400,
  filters: [],
  cards: [
    { card: 'fin_overdue_ar', title: '应收超期明细', chart: 'table', span: 12 },
  ],
  payloads: {
    fin_overdue_ar: {
      chart: 'table',
      unit: '元',
      as_of: '2026-09-15',
      severity_domain: ['p0', 'p1', 'p2', 'ok'],
      columns: [
        { key: 'name', title: '客户/主体' },
        { key: 'amount', title: '应收金额', format: 'wan' },
        { key: 'overdue_days', title: '超期天数', format: 'int' },
        { key: 'severity', title: '告警', format: 'severity' },
      ],
      rows: [
        // 演示结构（非真机）：超期 >60 天为 p0（默认阈值，待财务确认）
        { name: '演示·渠道A', amount: 120000.0, overdue_days: 75, severity: 'p0', has_fact: true },
        { name: '演示·渠道B', amount: 80000.0, overdue_days: 30, severity: 'p2', has_fact: true },
      ],
    },
  },
};

/* --------------------------------------- l2-ecom / l2-dining / l2-hall（月报真卡页） */

const l2Ecom: MockDashboard = {
  title: '电商月报',
  refresh_seconds: 86400,
  filters: [],
  cards: [{ card: 'ecom_monthly', title: '电商月报汇总', chart: 'table', span: 12 }],
  payloads: {
    ecom_monthly: {
      chart: 'table',
      unit: '元',
      month: '2026-09',
      columns: [
        { key: 'channel', title: '渠道' },
        { key: 'sales', title: '销售额', format: 'wan' },
        { key: 'refund', title: '退款金额', format: 'wan' },
        { key: 'net', title: '净销售额', format: 'wan' },
      ],
      rows: [
        { channel: '天猫', sales: 1407564.97, refund: 56300.0, net: 1351264.97 },
        { channel: '京东', sales: 1042892.17, refund: 31200.0, net: 1011692.17 },
      ],
    },
  },
};

const l2Dining: MockDashboard = {
  title: '餐厅月报',
  refresh_seconds: 86400,
  filters: [],
  cards: [{ card: 'dining_monthly', title: '餐厅月报汇总', chart: 'table', span: 12 }],
  payloads: {
    dining_monthly: {
      chart: 'table',
      unit: '元',
      month: '2026-09',
      columns: [
        { key: 'item', title: '项目' },
        { key: 'amount', title: '金额', format: 'wan' },
        { key: 'covers', title: '接待人次', format: 'int' },
      ],
      rows: [
        { item: '堂食收入', amount: 386000.0, covers: 2140 },
        { item: '宴请收入', amount: 152000.0, covers: 460 },
      ],
    },
  },
};

const l2Hall: MockDashboard = {
  title: '体验馆月报',
  refresh_seconds: 86400,
  filters: [],
  cards: [{ card: 'hall_monthly', title: '体验馆月报汇总', chart: 'table', span: 12 }],
  payloads: {
    hall_monthly: {
      chart: 'table',
      unit: '元',
      month: '2026-09',
      columns: [
        { key: 'item', title: '项目' },
        { key: 'amount', title: '金额', format: 'wan' },
        { key: 'visits', title: '到访人次', format: 'int' },
      ],
      rows: [
        { item: '品鉴销售', amount: 268000.0, visits: 320 },
        { item: '团购转化', amount: 195000.0, visits: 85 },
      ],
    },
  },
};

/* ------------------------------------- 占位页（0 占位：结构一次到位 + has_fact=false） */
/* 契约形态：{"chart":"table","columns":[...],"rows":[],"has_fact":false,"unit":"元"}。
 * 挂零 = p0「应接入未接入」；前端列头照常渲染 + 空态 + 「待接入」角标。 */

const l2Inventory: MockDashboard = {
  title: '库存与库龄（待接入）',
  refresh_seconds: 86400,
  filters: [],
  cards: [{ card: 'inventory_age', title: '库存与库龄分布', chart: 'table', span: 12 }],
  payloads: {
    inventory_age: {
      chart: 'table',
      unit: '元',
      has_fact: false,
      columns: [
        { key: 'sku', title: 'SKU' },
        { key: 'warehouse', title: '仓库' },
        { key: 'qty', title: '库存量', format: 'int' },
        { key: 'amount', title: '库存金额', format: 'wan' },
        // 库龄分桶（0-90/91-180/181-365/>365，非效期口径）
        { key: 'age_0_90', title: '库龄0-90天', format: 'int' },
        { key: 'age_91_180', title: '库龄91-180天', format: 'int' },
        { key: 'age_181_365', title: '库龄181-365天', format: 'int' },
        { key: 'age_over_365', title: '库龄>365天', format: 'int' },
      ],
      rows: [],
    },
  },
};

const l2Warehouse: MockDashboard = {
  title: '仓储运作（待接入）',
  refresh_seconds: 86400,
  // 占位页筛选：数据源未接入 → warehouses 候选为空数组 → FilterBar 禁用态
  filters: [{ param: 'warehouse', source: 'warehouses', label: '仓库' }],
  cards: [{ card: 'warehouse_sla', title: '发货时效与超时原因', chart: 'table', span: 12, params: ['warehouse'] }],
  payloads: {
    warehouse_sla: {
      chart: 'table',
      unit: '元',
      has_fact: false,
      columns: [
        { key: 'stage', title: '环节' },
        { key: 'orders', title: '单量', format: 'int' },
        { key: 'sla_24h', title: '24h达标率', format: 'pct' },
        { key: 'sla_48h', title: '48h达标率', format: 'pct' },
        // 超时六类原因
        { key: 'timeout_stockout', title: '超时-缺货', format: 'int' },
        { key: 'timeout_burst', title: '超时-爆单', format: 'int' },
        { key: 'timeout_logistics', title: '超时-物流', format: 'int' },
        { key: 'timeout_system', title: '超时-系统', format: 'int' },
        { key: 'timeout_address', title: '超时-地址异常', format: 'int' },
        { key: 'timeout_other', title: '超时-其他', format: 'int' },
      ],
      rows: [],
    },
  },
};

const l2Quarter: MockDashboard = {
  title: '季度预实（待接入）',
  refresh_seconds: 86400,
  filters: [],
  cards: [{ card: 'quarter_budget_actual', title: '季度预算 vs 实际', chart: 'table', span: 12 }],
  payloads: {
    quarter_budget_actual: {
      chart: 'table',
      unit: '元',
      has_fact: false,
      columns: [
        // 三板块 预算 vs 实际 结构一次到位
        { key: 'section', title: '板块' },
        { key: 'budget', title: '季度预算', format: 'wan' },
        { key: 'actual', title: '季度实际', format: 'wan' },
        { key: 'rate', title: '达成率', format: 'pct' },
      ],
      rows: [],
    },
  },
};

const l2Yoy: MockDashboard = {
  title: '月度同比（待接入）',
  refresh_seconds: 86400,
  filters: [],
  cards: [{ card: 'monthly_yoy', title: '月度同比', chart: 'table', span: 12 }],
  payloads: {
    monthly_yoy: {
      chart: 'table',
      unit: '元',
      has_fact: false,
      columns: [
        { key: 'metric', title: '指标' },
        { key: 'current', title: '本月', format: 'wan' },
        { key: 'last_year', title: '去年同月', format: 'wan' },
        // 同比率：除零/无基数 → null → 显示「—」，绝不显示 0% / -100%
        { key: 'yoy_rate', title: '同比率', format: 'pct' },
      ],
      rows: [],
    },
  },
};

const l2Contract: MockDashboard = {
  title: '合同核销（待接入）',
  refresh_seconds: 86400,
  filters: [],
  cards: [{ card: 'contract_writeoff', title: '品牌×渠道核销进度', chart: 'table', span: 12 }],
  payloads: {
    contract_writeoff: {
      chart: 'table',
      unit: '元',
      has_fact: false,
      columns: [
        { key: 'brand', title: '品牌' },
        { key: 'channel', title: '渠道' },
        { key: 'contract_amount', title: '合同金额', format: 'wan' },
        { key: 'written_off', title: '已核销', format: 'wan' },
        { key: 'progress', title: '核销进度', format: 'pct' },
      ],
      rows: [],
    },
  },
};

// 导航排序：l1 → 资金/月报 → 占位页集中尾部（标题带「（待接入）」）
const DASHBOARDS: Record<string, MockDashboard> = {
  'l1-cockpit': l1,
  'l2-region': l2Region,
  'l2-channel': l2Channel,
  'l2-product': l2Product,
  'l2-people': l2People,
  'l2-fund-safety': l2FundSafety,
  'l2-ecom': l2Ecom,
  'l2-dining': l2Dining,
  'l2-hall': l2Hall,
  'l2-inventory': l2Inventory,
  'l2-warehouse': l2Warehouse,
  'l2-quarter': l2Quarter,
  'l2-yoy': l2Yoy,
  'l2-contract': l2Contract,
};

/* ------------------------------------------------------------------ 出口 */

function notFound(what: string): never {
  throw new BiWebError('not_found', 404, `mock 后端没有该${what}`);
}

export interface MockFixtures {
  listDashboards(): DashboardSummary[];
  getDashboardDef(id: string): DashboardDefinition;
  getOptions(source: string): string[];
  getCardPayload(dashboardId: string, cardId: string): CardPayload;
}

export const mockFixtures: MockFixtures = {
  listDashboards() {
    return Object.entries(DASHBOARDS).map(([id, d]) => ({ id, title: d.title }));
  },

  getDashboardDef(id) {
    const d = DASHBOARDS[id];
    if (!d) notFound('看板');
    return {
      id,
      title: d.title,
      refresh_seconds: d.refresh_seconds,
      filters: d.filters ?? [],
      cards: d.cards.map((c) => ({
        card: c.card,
        title: c.title,
        chart: c.chart,
        span: c.span,
        on_click: null,
        params: c.params ?? [],
      })),
    };
  },

  getOptions(source) {
    if (!OPTIONS[source]) notFound('选项源');
    return [...OPTIONS[source]];
  },

  getCardPayload(dashboardId, cardId) {
    const d = DASHBOARDS[dashboardId];
    if (!d) notFound('看板');
    const p = d.payloads[cardId];
    if (!p) notFound('卡片');
    return p;
  },
};
