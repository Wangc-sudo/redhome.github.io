<template>
  <!-- 引导页：钉钉外打开 -->
  <div v-if="phase === 'need-dd'" class="guide-page">
    <div class="guide-card">
      <div class="guide-logo">小易</div>
      <p>请在钉钉中打开本页面</p>
      <p class="guide-sub">钉钉 → 工作台 → 小易，或在客服群内点链接进入</p>
    </div>
  </div>

  <!-- 启动中 -->
  <div v-else-if="phase === 'boot'" class="guide-page">
    <div class="guide-card"><div class="guide-logo">小易</div><p>正在进入…</p></div>
  </div>

  <!-- 主界面 -->
  <div v-else class="layout">
    <header class="topbar">
      <button class="icon-btn menu-btn" @click="sidebarOpen = !sidebarOpen" aria-label="会话">☰</button>
      <div class="brand">小易 <span class="brand-sub">智能助手</span></div>
      <div class="user">{{ user.name || '同事' }}</div>
    </header>

    <div class="body">
      <!-- 侧边栏：会话历史 -->
      <aside class="sidebar" :class="{ open: sidebarOpen }">
        <button class="new-chat" @click="newSession">＋ 新对话</button>
        <div class="session-list">
          <div v-for="s in sessions" :key="s.sid"
               class="session-item" :class="{ active: s.sid === currentSid }"
               @click="switchSession(s.sid)">
            <span class="session-title">{{ s.title || '新对话' }}</span>
            <button class="session-del" @click.stop="removeSession(s.sid)" aria-label="删除">×</button>
          </div>
          <div v-if="!sessions.length" class="session-empty">暂无历史对话</div>
        </div>
      </aside>
      <div v-if="sidebarOpen" class="sidebar-mask" @click="sidebarOpen = false"></div>

      <!-- 对话区 -->
      <main class="chat" ref="chatEl">
        <!-- 空会话引导 -->
        <div v-if="!messages.length && !sending" class="welcome">
          <div class="guide-logo large">小易</div>
          <p class="welcome-title">你好，我是小易</p>
          <p class="welcome-sub">商品资料、产品手册、业务数据，随时问我</p>
          <div class="suggest-cards">
            <button v-for="q in suggestions" :key="q" class="suggest-card"
                    @click="ask(q)">{{ q }}</button>
          </div>
        </div>

        <!-- 消息流 -->
        <div v-for="(m, i) in messages" :key="i"
             class="msg-row" :class="m.role">
          <div class="bubble">
            <div v-if="m.role === 'user'" class="msg-text">{{ m.content }}</div>
            <template v-else>
              <div class="msg-md" v-html="renderMarkdown(m.content)"></div>
              <div v-if="m.attachments && m.attachments.length" class="attach-list">
                <a v-for="(a, j) in m.attachments" :key="j" class="attach-chip"
                   :href="a.url" target="_blank" rel="noopener">📎 {{ a.name }}</a>
              </div>
              <div class="msg-meta">
                <span v-if="m.meta && m.meta.latency_ms != null">
                  共 {{ (m.meta.latency_ms / 1000).toFixed(1) }}s
                  <template v-if="m.meta.tools && m.meta.tools.length">
                    · {{ m.meta.tools.length }} 次查询
                  </template>
                </span>
              </div>
              <div class="msg-actions">
                <button @click="copyText(m.content)">复制</button>
                <button v-if="canShare" @click="shareText(m.content)">转发</button>
                <button v-if="i === lastAssistantIndex" @click="regenerate">重新生成</button>
                <button @click="quoteMessage(m.content)">引用追问</button>
              </div>
            </template>
          </div>
        </div>

        <!-- 思考态 -->
        <div v-if="sending" class="msg-row assistant">
          <div class="bubble thinking">
            <span class="dots"><i></i><i></i><i></i></span>
            小易思考中… 已用 {{ elapsed }}s
            <div v-if="elapsed >= 10" class="thinking-more">数据量较大，正在仔细查询</div>
          </div>
        </div>

        <!-- 发送失败条 -->
        <div v-if="sendError" class="send-error">
          发送失败（网络异常）<button @click="retrySend">重发</button>
        </div>
      </main>
    </div>

    <!-- 输入区 -->
    <footer class="composer">
      <div v-if="quote" class="quote-bar">
        <span class="quote-text">{{ quote.slice(0, 60) }}</span>
        <button @click="quote = ''" aria-label="取消引用">×</button>
      </div>
      <div class="input-row">
        <textarea ref="inputEl" v-model="draft" rows="1"
                  placeholder="问小易…（Enter 发送，Shift+Enter 换行）"
                  :disabled="sending"
                  @keydown.enter.exact.prevent="send"
                  @input="autoGrow"></textarea>
        <button class="send-btn" :disabled="sending || !draft.trim()" @click="send">发送</button>
      </div>
    </footer>

    <div v-if="toast" class="toast">{{ toast }}</div>
  </div>
</template>

<script setup>
import { computed, nextTick, onMounted, onUnmounted, ref } from 'vue'
import { api } from './api'
import { inDingTalk, requestAuthCode, shareToChat } from './dd'
import { renderMarkdown } from './md'

const DEFAULT_SUGGESTIONS = [
  '习酒窖藏1988 箱规是多少',
  '酱香酒有哪些',
  '售后退款处理流程',
  '今天杭州区域销售情况',
]

const phase = ref('boot')
const user = ref({})
const sessions = ref([])
const currentSid = ref('')
const messages = ref([])
const draft = ref('')
const sending = ref(false)
const elapsed = ref(0)
const sendError = ref(false)
const sidebarOpen = ref(false)
const quote = ref('')
const toast = ref('')
const suggestions = ref(DEFAULT_SUGGESTIONS)
const chatEl = ref(null)
const inputEl = ref(null)

const canShare = computed(() => inDingTalk())
const lastAssistantIndex = computed(() => {
  for (let i = messages.value.length - 1; i >= 0; i -= 1) {
    if (messages.value[i].role === 'assistant') return i
  }
  return -1
})

let timer = null
let toastTimer = null

function showToast(text) {
  toast.value = text
  clearTimeout(toastTimer)
  toastTimer = setTimeout(() => { toast.value = '' }, 2000)
}

function autoGrow(e) {
  const el = e.target
  el.style.height = 'auto'
  el.style.height = `${Math.min(el.scrollHeight, 120)}px`
}

async function scrollBottom() {
  await nextTick()
  if (chatEl.value) chatEl.value.scrollTop = chatEl.value.scrollHeight
}

async function boot() {
  if (!inDingTalk()) { phase.value = 'need-dd'; return }
  try {
    const { corp_id: corpId } = await api.publicConfig()
    const code = await requestAuthCode(corpId)
    await api.authDingtalk(code)
    const info = await api.bootstrap()
    user.value = info
    if (info.guide && info.guide.suggestions && info.guide.suggestions.length) {
      suggestions.value = info.guide.suggestions
    }
    const { sessions: list } = await api.listSessions()
    sessions.value = list
    if (list.length) {
      await switchSession(list[0].sid)
    } else {
      await newSession()
    }
    phase.value = 'ready'
  } catch (err) {
    phase.value = 'need-dd'
  }
}

async function refreshSessions() {
  const { sessions: list } = await api.listSessions()
  sessions.value = list
}

async function newSession() {
  const { sid } = await api.createSession()
  await refreshSessions()
  currentSid.value = sid
  messages.value = []
  sidebarOpen.value = false
}

async function switchSession(sid) {
  const detail = await api.getSession(sid)
  currentSid.value = sid
  messages.value = detail.messages
  sidebarOpen.value = false
  scrollBottom()
}

async function removeSession(sid) {
  await api.deleteSession(sid)
  await refreshSessions()
  if (sid === currentSid.value) {
    if (sessions.value.length) await switchSession(sessions.value[0].sid)
    else await newSession()
  }
}

function startThinking() {
  sending.value = true
  elapsed.value = 0
  timer = setInterval(() => { elapsed.value += 1 }, 1000)
}

function stopThinking() {
  sending.value = false
  clearInterval(timer)
}

async function send() {
  const content = (quote.value
    ? `关于「${quote.value.slice(0, 60)}」：${draft.value}`
    : draft.value).trim()
  if (!content || sending.value) return
  const quoted = quote.value
  draft.value = ''
  quote.value = ''
  if (inputEl.value) inputEl.value.style.height = 'auto'
  messages.value.push({ role: 'user', content, ts: '' })
  scrollBottom()
  startThinking()
  try {
    const result = await api.sendMessage(currentSid.value, content)
    messages.value.push({
      role: 'assistant', content: result.reply, ts: '',
      meta: result.meta, attachments: result.attachments,
    })
    refreshSessions()
  } catch (err) {
    messages.value.pop()
    draft.value = content
    if (quoted) quote.value = quoted
    sendError.value = true
  } finally {
    stopThinking()
    scrollBottom()
  }
}

async function retrySend() {
  sendError.value = false
  await send()
}

async function ask(q) {
  draft.value = q
  await send()
}

async function regenerate() {
  if (sending.value) return
  startThinking()
  try {
    const result = await api.regenerate(currentSid.value)
    messages.value.push({
      role: 'assistant', content: result.reply, ts: '',
      meta: result.meta, attachments: result.attachments, regenerated: true,
    })
  } catch (err) {
    showToast('重新生成失败，请稍后再试')
  } finally {
    stopThinking()
    scrollBottom()
  }
}

async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text)
    showToast('已复制')
  } catch (err) {
    showToast('复制失败')
  }
}

function shareText(text) {
  if (!shareToChat(text)) showToast('当前环境不支持转发')
}

function quoteMessage(content) {
  quote.value = content
  nextTick(() => inputEl.value && inputEl.value.focus())
}

function onUnauthorized() {
  phase.value = 'need-dd'
}

onMounted(() => {
  window.addEventListener('kb:unauthenticated', onUnauthorized)
  boot()
})
onUnmounted(() => {
  window.removeEventListener('kb:unauthenticated', onUnauthorized)
  clearInterval(timer)
  clearTimeout(toastTimer)
})
</script>
