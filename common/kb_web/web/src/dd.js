// 钉钉 H5 免登与分享（方案 §6）：钉钉外优雅降级为引导页。

export function inDingTalk() {
  return typeof window !== 'undefined' && !!window.dd && !!window.dd.runtime
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
