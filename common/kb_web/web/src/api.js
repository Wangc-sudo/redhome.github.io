// 小易 Web API 封装：15s 超时 + GET 自动重试一次（弱网兜底，方案 §8）。

const TIMEOUT_MS = 15000

export class ApiError extends Error {
  constructor(status, message) {
    super(message || `HTTP ${status}`)
    this.status = status
  }
}

async function request(path, { method = 'GET', body, retry = method === 'GET' } = {}) {
  let lastError
  const attempts = retry ? 2 : 1
  for (let i = 0; i < attempts; i += 1) {
    const controller = new AbortController()
    const timer = setTimeout(() => controller.abort(), TIMEOUT_MS)
    try {
      const resp = await fetch(path, {
        method,
        headers: body ? { 'Content-Type': 'application/json' } : undefined,
        body: body ? JSON.stringify(body) : undefined,
        signal: controller.signal,
        credentials: 'same-origin',
      })
      clearTimeout(timer)
      if (resp.status === 401) {
        window.dispatchEvent(new CustomEvent('kb:unauthenticated'))
        throw new ApiError(401, '登录已失效')
      }
      if (!resp.ok) throw new ApiError(resp.status)
      return await resp.json()
    } catch (err) {
      clearTimeout(timer)
      lastError = err
      if (err instanceof ApiError && err.status === 401) throw err
      if (i + 1 >= attempts) throw err
    }
  }
  throw lastError
}

export const api = {
  publicConfig: () => request('/kb/api/public-config'),
  bootstrap: () => request('/kb/api/bootstrap'),
  authDingtalk: (authCode) => request('/kb/api/auth/dingtalk', {
    method: 'POST', body: { authCode }, retry: false,
  }),
  listSessions: () => request('/kb/api/sessions'),
  createSession: () => request('/kb/api/sessions', { method: 'POST' }),
  getSession: (sid) => request(`/kb/api/sessions/${sid}`),
  deleteSession: (sid) => request(`/kb/api/sessions/${sid}`, { method: 'DELETE', retry: false }),
  renameSession: (sid, title) => request(`/kb/api/sessions/${sid}`, {
    method: 'PATCH', body: { title }, retry: false,
  }),
  sendMessage: (sid, content) => request(`/kb/api/sessions/${sid}/messages`, {
    method: 'POST', body: { content }, retry: false,
  }),
  regenerate: (sid) => request(`/kb/api/sessions/${sid}/regenerate`, {
    method: 'POST', retry: false,
  }),
}
