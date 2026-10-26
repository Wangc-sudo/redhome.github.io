// 钉钉 H5 免登与分享（方案 §6）：钉钉外优雅降级为引导页。

// 判定钉钉容器必须用 UA：alicdn 的 dingtalk-jsapi 在普通浏览器里也会创建
// window.dd（含 .runtime 命名空间），桥接初始化失败时报 4040 notInDingTalk
// 且 requestAuthCode 永不返回——用 window.dd 判定会把浏览器误判成钉钉容器，
// 页面卡死在启动态（2026-10-26 扫码登录实测暴露）。
export function inDingTalk() {
  return typeof navigator !== 'undefined'
    && /DingTalk/i.test(navigator.userAgent || '')
}

export function requestAuthCode(corpId) {
  return new Promise((resolve, reject) => {
    window.dd.runtime.permission.requestAuthCode({
      corpId,
      onSuccess: (result) => resolve(result.code),
      onFail: (err) => reject(err),
    })
  })
}

// 转发答案到钉钉会话（消息操作条「转发」）；容器不支持时返回 false。
export function shareToChat(text) {
  if (!inDingTalk() || !window.dd.biz || !window.dd.biz.chat) return false
  try {
    window.dd.biz.chat.pickConversation({
      isConfirm: true,
      title: '转发小易的回答',
      content: text,
      onSuccess: () => {},
      onFail: () => {},
    })
    return true
  } catch (err) {
    return false
  }
}
