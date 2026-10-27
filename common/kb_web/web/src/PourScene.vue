<template>
  <div class="pour-scene" :class="{ small, dark }">
    <img class="pour-bottle" :src="bottleUrl" alt="习酒窖藏1988">
    <div class="pour-stream"></div>
    <svg class="pour-glass" viewBox="0 0 60 64" fill="none">
      <defs>
        <clipPath :id="clipId"><path d="M16 14 h28 c0 12 -5 22 -14 22 c-9 0 -14 -10 -14 -22 z"/></clipPath>
        <linearGradient :id="wineId" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0" stop-color="#ffffff" stop-opacity="0.96"/>
          <stop offset="1" stop-color="#dfe8f5" stop-opacity="0.82"/>
        </linearGradient>
      </defs>
      <!-- 白酒小酒杯（喇叭杯）：矮身阔口短柄小底足 -->
      <path d="M16 14 h28 c0 12 -5 22 -14 22 c-9 0 -14 -10 -14 -22 z"
            fill="#ffffff" fill-opacity="0.08" stroke="#e6e9f2" stroke-width="2" stroke-linejoin="round"/>
      <ellipse cx="30" cy="14" rx="14" ry="2.6" stroke="#e6e9f2" stroke-width="1.6"/>
      <!-- clip 必须挂在静态 g 上：挂在被动画的 rect 上裁剪会跟着液面一起平移（酒溢出杯外） -->
      <g :clip-path="`url(#${clipId})`">
        <rect class="pour-liquid" x="14" y="0" width="32" height="36" :fill="`url(#${wineId})`"/>
      </g>
      <path d="M30 36 v8 M20 48 h20" stroke="#e6e9f2" stroke-width="2" stroke-linecap="round"/>
    </svg>
  </div>
</template>

<script setup>
// 倒酒场景：酒瓶倾斜 → 酒线流出 → 杯中液面上升 → 回正循环（样式在 style.css，
// splash 页在 index.html 有同构内联副本——改动画参数时两处同步）
let seq = 0
defineProps({
  bottleUrl: { type: String, required: true },
  small: { type: Boolean, default: false },
  dark: { type: Boolean, default: false },
})
seq += 1
const clipId = `pourGlassClip${seq}`
const wineId = `pourWine${seq}`
</script>
