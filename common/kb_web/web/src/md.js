// markdown 渲染：marked + 轻量消毒（去 script/iframe/事件属性）。
// 小易答案是 LLM 产物，虽然内部场景风险低，v-html 前仍过一道。
import { marked } from 'marked'

marked.setOptions({ breaks: true, gfm: true })

export function renderMarkdown(text) {
  const html = marked.parse(text || '')
  const doc = new DOMParser().parseFromString(html, 'text/html')
  doc.querySelectorAll('script,iframe,object,embed,form').forEach((el) => el.remove())
  doc.querySelectorAll('*').forEach((el) => {
    Array.from(el.attributes).forEach((attr) => {
      if (attr.name.startsWith('on')) el.removeAttribute(attr.name)
      if (attr.name === 'href' && /^\s*javascript:/i.test(attr.value)) {
        el.removeAttribute('href')
      }
    })
  })
  return doc.body.innerHTML
}
