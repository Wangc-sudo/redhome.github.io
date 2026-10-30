<template>
  <!-- 引导页：钉钉外打开 → 扫码登录 -->
  <div v-if="phase === 'need-dd'" class="guide-page dark">
    <div class="guide-card">
      <PourScene :bottle-url="bottleUrl" />
      <p class="brand-word">小易</p>
      <button class="login-btn" :disabled="qrLoading" @click="startQrLogin">
        {{ qrLoading ? '正在跳转…' : '钉钉扫码登录' }}
      </button>
      <p v-if="loginError" class="login-error">{{ loginError }}</p>
    </div>
  </div>

  <!-- 启动中（分阶段文案 + 进度条） -->
  <div v-else-if="phase === 'boot'" class="guide-page dark">
    <div class="guide-card">
      <PourScene :bottle-url="bottleUrl" />
      <p class="boot-text">{{ bootText }}</p>
      <div class="boot-bar"><div class="boot-bar-fill"></div></div>
    </div>
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
          <div v-if="sessionsLoading" class="session-skel" aria-hidden="true">
            <div v-for="n in 5" :key="n" class="skel-row"></div>
          </div>
          <template v-else>
            <div v-for="s in sessions" :key="s.sid"
                 class="session-item" :class="{ active: s.sid === currentSid }"
                 @click="switchSession(s.sid)">
              <span class="session-title">{{ s.title || '新对话' }}</span>
              <button class="session-del" @click.stop="removeSession(s.sid)" aria-label="删除">×</button>
            </div>
            <div v-if="!sessions.length" class="session-empty">暂无历史对话</div>
          </template>
        </div>
      </aside>
      <div v-if="sidebarOpen" class="sidebar-mask" @click="sidebarOpen = false"></div>

      <!-- 对话区 -->
      <main class="chat" ref="chatEl">
        <!-- 消息骨架屏（首次进入/切换会话加载中） -->
        <div v-if="messagesLoading" class="chat-skel" aria-hidden="true">
          <div class="skel-bubble skel-right"></div>
          <div class="skel-bubble skel-left skel-wide"></div>
          <div class="skel-bubble skel-right skel-short"></div>
          <div class="skel-bubble skel-left"></div>
        </div>

        <!-- 空会话引导 -->
        <div v-else-if="!messages.length && !sending" class="welcome">
          <PourScene :bottle-url="bottleUrl" small dark />
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
                <span v-if="m.meta && m.meta.tokens" class="meta-dim"
                      :title="`本条消耗 token：输入 ${m.meta.tokens.prompt} · 输出 ${m.meta.tokens.completion}（遥测口径）`">
                  · {{ fmtTokens(m.meta.tokens) }}
                </span>
                <span v-if="m.meta && m.meta.history_truncated" class="meta-dim">
                  · 仅参考最近 {{ m.meta.history_rounds }} 轮对话
                </span>
              </div>
              <div v-if="m.meta && (m.meta.export_snapshot_id || m.meta.export)" class="export-panel">
                <template v-if="m.meta.export">
                  <span v-if="['pending', 'running'].includes(m.meta.export.status)">Excel 生成中…</span>
                  <a v-else-if="m.meta.export.status === 'completed'"
                     :href="m.meta.export.download_url">下载 Excel</a>
                  <button v-else @click="startExport(m)">重新导出</button>
                  <span v-if="m.meta.export.status === 'failed'" class="export-error">生成失败，请重试</span>
                  <span v-else-if="m.meta.export.status === 'expired'" class="export-error">文件已过期</span>
                  <small v-if="exportScope(m.meta.export)">{{ exportScope(m.meta.export) }}</small>
                </template>
                <button v-else @click="startExport(m)">导出 Excel</button>
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
      <!-- 会话状态区（低调图标，悬停看明细） -->
      <div v-if="sessionStats.show" class="status-bar">
        <span class="stat"
              :title="`小易会参考最近 ${sessionStats.rounds} 轮对话，更早的内容不在记忆中`">◔ {{ sessionStats.rounds }} 轮上下文</span>
        <span v-if="sessionStats.hasTokens" class="stat"
              :title="`本会话累计 token：输入 ${sessionStats.prompt} · 输出 ${sessionStats.completion}（遥测口径）`">⚡ {{ sessionStats.totalText }}</span>
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
import PourScene from './PourScene.vue'

const DEFAULT_SUGGESTIONS = [
  '习酒窖藏1988 箱规是多少',
  '酱香酒有哪些',
  '售后退款处理流程',
  '今天杭州区域销售情况',
]

// 官方产品图（public 目录，base 感知）
const bottleUrl = `${import.meta.env.BASE_URL}bottle.png`

const phase = ref('boot')
const bootText = ref('正在验证身份…')
const user = ref({})
const sessions = ref([])
const currentSid = ref('')
const messages = ref([])
const sessionsLoading = ref(false)
const messagesLoading = ref(false)
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
const qrLoading = ref(false)
const loginError = ref('')

// 扫码登录回调失败回跳（/kb/?login_error=<code>）→ 中文提示（方案 §5.2）
const LOGIN_ERROR_TEXT = {
  expired: '登录已过期，请重新扫码',
  failed: '登录失败，请稍后再试',
  denied: '仅限本企业成员使用',
}

const canShare = computed(() => inDingTalk())

// 会话状态区：上下文轮数 + 累计 token（数据来自各消息 meta，纯前端聚合）
const sessionStats = computed(() => {
  let prompt = 0
  let completion = 0
  let pairs = 0
  let pendingUser = null
  let lastRounds = null
  let hasTokens = false
  for (const m of messages.value) {
    if (m.role === 'user') { pendingUser = m; continue }
    if (m.role !== 'assistant') continue
    if (pendingUser) { pairs += 1; pendingUser = null }
    const t = m.meta && m.meta.tokens
    if (t) {
      prompt += t.prompt || 0
      completion += t.completion || 0
      hasTokens = true
    }
    if (m.meta && m.meta.history_rounds != null) lastRounds = m.meta.history_rounds
  }
  const total = prompt + completion
  return {
    show: pairs > 0 || hasTokens,
    rounds: lastRounds != null ? lastRounds : Math.min(pairs, 6),
    prompt, completion, hasTokens,
    totalText: total >= 1000 ? `${(total / 1000).toFixed(1)}k` : `${total}`,
  }
})

function fmtTokens(t) {
  const total = (t.prompt || 0) + (t.completion || 0)
  return total >= 1000 ? `${(total / 1000).toFixed(1)}k tokens` : `${total} tokens`
}
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

// 本地秒开缓存：会话列表 + 最近一个会话的消息（仅在登录态校验通过后渲染，
// 网络真值到达后静默替换；容量满等异常一律忽略，缓存只是体验优化）
const CACHE_KEY = 'kbweb:boot-cache'

function readCache() {
  try {
    const raw = localStorage.getItem(CACHE_KEY)
    if (!raw) return null
    const c = JSON.parse(raw)
    if (!c || !Array.isArray(c.sessions)) return null
    return c
  } catch { return null }
}

function writeCache() {
  try {
    localStorage.setItem(CACHE_KEY, JSON.stringify({
      sid: currentSid.value,
      sessions: sessions.value.slice(0, 20),
      messages: messages.value.slice(-30),
    }))
  } catch { /* 忽略 */ }
}

// 登录成功后进主界面（免登与扫码登录共用）：bootstrap + 会话加载
async function enterApp() {
  const info = await api.bootstrap()
  user.value = info
  if (info.guide && info.guide.suggestions && info.guide.suggestions.length) {
    suggestions.value = info.guide.suggestions
  }
  bootText.value = '正在拉取会话…'
  // 缓存秒开：先渲上次的会话列表与最近消息，再拉真值静默替换
  const cache = readCache()
  if (cache && cache.sessions.length) {
    sessions.value = cache.sessions
    currentSid.value = cache.sid || cache.sessions[0].sid
    messages.value = cache.messages || []
  } else {
    sessionsLoading.value = true
    messagesLoading.value = true
  }
  phase.value = 'ready'
  scrollBottom()
  const { sessions: list } = await api.listSessions()
  sessions.value = list
  sessionsLoading.value = false
  if (list.length) {
    const keepSid = currentSid.value && list.some(s => s.sid === currentSid.value)
    await switchSession(keepSid ? currentSid.value : list[0].sid)
  } else {
    messagesLoading.value = false
    await newSession()
  }
  writeCache()
}

// 浏览器扫码登录：取 login_url 整页跳转钉钉授权页（二维码在钉钉域渲染）
async function startQrLogin() {
  qrLoading.value = true
  loginError.value = ''
  try {
    const { login_url: url } = await api.qrLoginUrl()
    window.location.assign(url)
  } catch (err) {
    qrLoading.value = false
    loginError.value = '扫码登录暂不可用，请在钉钉中打开'
  }
}

async function boot() {
  // 扫码登录回调失败回跳的错误提示（清掉 query 避免刷新残留）
  const errCode = new URLSearchParams(window.location.search).get('login_error')
  if (errCode) {
    loginError.value = LOGIN_ERROR_TEXT[errCode] || '登录失败，请稍后再试'
    window.history.replaceState(null, '', window.location.pathname)
  }
  if (inDingTalk()) {
    // 钉钉容器内：JSAPI 免登（扫码按钮不出现）
    try {
      const { corp_id: corpId } = await api.publicConfig()
      bootText.value = '正在免密登录…'
      const code = await requestAuthCode(corpId)
      await api.authDingtalk(code)
      await enterApp()
    } catch (err) {
      phase.value = 'need-dd'
    }
    return
  }
  // 普通浏览器：已有 session 直进，否则展示扫码登录页
  try {
    await enterApp()
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
  messagesLoading.value = false
  sidebarOpen.value = false
  writeCache()
}

async function switchSession(sid) {
  if (sid !== currentSid.value || !messages.value.length) messagesLoading.value = true
  const detail = await api.getSession(sid)
  currentSid.value = sid
  messages.value = detail.messages
  messagesLoading.value = false
  sidebarOpen.value = false
  scrollBottom()
  writeCache()
  trackAllExports()
}

async function removeSession(sid) {
  await api.deleteSession(sid)
  await refreshSessions()
  if (sid === currentSid.value) {
    if (sessions.value.length) await switchSession(sessions.value[0].sid)
    else await newSession()
  } else {
    writeCache()
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
    trackAllExports()
    refreshSessions()
    writeCache()
  } catch (err) {
    // 先拉后发（2026-10-26）：服务端不受客户端断连/超时影响，答案可能
    // 已答完落库——先拉会话核对，末尾已是本轮问答（同内容用户消息 + 紧
    // 跟 assistant 回复）则直接渲染，避免盲目重发造成重复消息与双倍
    // LLM 成本。拉取也失败或末尾不是本轮答案，才按发送失败处理。
    try {
      const detail = await api.getSession(currentSid.value)
      const msgs = detail.messages || []
      const last = msgs[msgs.length - 1]
      const prev = msgs[msgs.length - 2]
      if (last && last.role === 'assistant' && prev
          && prev.role === 'user' && prev.content === content) {
        messages.value = msgs
        sendError.value = false
        trackAllExports()
        refreshSessions()
        writeCache()
        return
      }
    } catch (e2) { /* 拉取失败则按发送失败处理 */ }
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
    trackAllExports()
    writeCache()
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

const exportTimers = new Map()

function exportScope(task) {
  const items = task && task.query_summary && task.query_summary.items
  if (!items || !items.length) return ''
  return items.map((item) => {
    const scope = item.data_scope || '查询范围见导出说明'
    return item.data_version ? `${scope} · 数据截至 ${item.data_version}` : scope
  }).join('；')
}

async function startExport(message) {
  const snapshotId = message.meta && message.meta.export_snapshot_id
  if (!snapshotId || !currentSid.value) return
  try {
    message.meta.export = await api.createExport(currentSid.value, snapshotId)
    trackExport(message)
  } catch (err) {
    showToast(err.status === 403 ? '没有导出该结果的权限' : '导出创建失败，请稍后重试')
  }
}

function trackExport(message) {
  const task = message.meta && message.meta.export
  if (!task || !['pending', 'running'].includes(task.status)) return
  const key = task.export_id
  const sid = currentSid.value
  if (exportTimers.has(key)) return
  const timer = setTimeout(async () => {
    exportTimers.delete(key)
    try {
      message.meta.export = await api.exportStatus(sid, key)
      trackExport(message)
    } catch (err) {
      showToast('导出状态刷新失败，请稍后刷新会话')
    }
  }, 1000)
  exportTimers.set(key, timer)
}

function trackAllExports() {
  messages.value.forEach(trackExport)
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
  exportTimers.forEach(clearTimeout)
  exportTimers.clear()
})
</script>
