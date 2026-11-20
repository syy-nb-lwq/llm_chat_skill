// 前端唯一的 HTTP 出口。
//
// 以前 13 处裸 fetch 各写一遍：有的检查 res.ok，有的不检查；
// 没有超时，网络挂了页面就一直转圈；错误结构各不相同（有的是 text，有的是 json）。
//
// 这里统一三件事：
//   1. baseURL —— 只从这里拼 API_BASE，页面不再关心地址
//   2. timeout —— AbortController，默认 15s，超时抛 ApiError(status=0)
//   3. 错误结构 —— 非 2xx 一律抛 ApiError（带 status / body），调用方只需 catch
//
// 页面只调具名函数（如 `memoryApi.profile(sid)`），不再出现 http 细节。

import { API_BASE } from '../config'

export const DEFAULT_TIMEOUT_MS = 15000

export class ApiError extends Error {
  constructor(message, { status = 0, url = '', body = '' } = {}) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.url = url
    this.body = body
  }

  /** 超时（没有 HTTP 状态码，是本地主动中断的） */
  get isTimeout() {
    return this.status === 0 && this.message.includes('超时')
  }

  get isNotFound() {
    return this.status === 404
  }

  get isForbidden() {
    return this.status === 403
  }
}

function buildUrl(path, query) {
  const base = String(API_BASE || '').replace(/\/+$/, '')
  const url = new URL(base + path)
  Object.entries(query || {}).forEach(([key, value]) => {
    if (value === undefined || value === null || value === '') return
    url.searchParams.set(key, String(value))
  })
  return url.toString()
}

function parseBody(text) {
  if (!text) return null
  try {
    return JSON.parse(text)
  } catch (e) {
    return text
  }
}

/**
 * 发一个请求。
 * @param {string} path 以 / 开头的路径，如 '/api/tools'
 * @param {{method?: string, query?: object, body?: any, timeout?: number, headers?: object}} options
 * @returns {Promise<any>} 解析后的 JSON（非 JSON 响应返回文本）
 * @throws {ApiError}
 */
export async function request(path, options = {}) {
  const {
    method = 'GET',
    query,
    body,
    timeout = DEFAULT_TIMEOUT_MS,
    headers = {},
  } = options

  const url = buildUrl(path, query)
  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(), timeout)

  const init = {
    method,
    headers: { Accept: 'application/json', ...headers },
    signal: controller.signal,
  }
  if (body !== undefined) {
    if (typeof FormData !== 'undefined' && body instanceof FormData) {
      // 上传必须让浏览器自己写 Content-Type —— 里面带 boundary，
      // 手写 'multipart/form-data' 会导致服务端解析不出字段。
      init.body = body
    } else {
      init.headers['Content-Type'] = 'application/json'
      init.body = JSON.stringify(body)
    }
  }

  try {
    const res = await fetch(url, init)
    const text = await res.text()
    const payload = parseBody(text)

    if (!res.ok) {
      const detail = (payload && (payload.detail || payload.error)) || text || res.statusText
      throw new ApiError(`HTTP ${res.status}: ${detail}`, {
        status: res.status,
        url,
        body: text,
      })
    }
    return payload
  } catch (err) {
    if (err instanceof ApiError) throw err
    if (err && err.name === 'AbortError') {
      throw new ApiError(`请求超时（${timeout}ms）`, { url })
    }
    throw new ApiError(`网络错误：${err && err.message ? err.message : err}`, { url })
  } finally {
    clearTimeout(timer)
  }
}

// ===== 具名接口：页面只认这些函数 =====

export const toolsApi = {
  list: () => request('/api/tools'),
}

export const skillsApi = {
  list: () => request('/api/skills'),

  /** 删除某个技能版本；`version` 为空表示删除该技能的全部版本 */
  remove: (name, version) =>
    request(
      version
        ? `/api/skills/${encodeURIComponent(name)}/${encodeURIComponent(version)}`
        : `/api/skills/${encodeURIComponent(name)}`,
      { method: 'DELETE' },
    ),
}

export const skillDraftsApi = {
  /**
   * 待批准的方法论草稿。
   *
   * 为什么要单独一组接口：agent 做成一件多步任务后会自己提炼方法论，
   * 但**人不批准就不进技能库**（垃圾技能比没有技能更糟）。
   * 之前这批草稿只在后端躺着，界面上无处可见 —— 于是"沉淀了 15 条、
   * 技能库 0 条"。
   */
  list: () => request('/api/skill-drafts'),
  approve: (id) =>
    request(`/api/skill-drafts/${encodeURIComponent(id)}/approve`, { method: 'POST' }),
  discard: (id) =>
    request(`/api/skill-drafts/${encodeURIComponent(id)}`, { method: 'DELETE' }),
}

export const competenceApi = {
  /**
   * 能力档案：技能晋升之前的观察记录。
   *
   * 为什么需要它：技能是「一类任务的做法」，一次任务只是样本。
   * 某一类事做过几次、还差几次、有没有被纠正过 —— 这些看不见的话，
   * 界面只会显示「暂无技能」，用户会以为沉淀坏了。
   */
  list: () => request('/api/competences'),
  forget: (key) =>
    request(`/api/competences/${encodeURIComponent(key)}`, { method: 'DELETE' }),
}

export const uploadApi = {
  /**
   * 把一个本地文件送到后端上传区，拿回后端可读的绝对路径。
   *
   * 文档工具（read_document）吃的是**后端磁盘上的路径**，浏览器里的 File
   * 对象对后端不可见 —— 不落盘，能力再全也让用户无从触发。
   *
   * 超时给足 2 分钟：30 MB 的 PDF 走局域网也够用了。
   */
  send: (file, sessionId) => {
    const form = new FormData()
    form.append('file', file, file.name)
    form.append('session_id', sessionId || '')
    return request('/api/upload', { method: 'POST', body: form, timeout: 120000 })
  },
}

export const sessionApi = {  /** 全部会话（新→旧），每条带 title / message_count / updated_at */
  list: () => request('/api/sessions'),

  /** 某个会话的历史消息（短期记忆，全量保留） */
  messages: (sessionId, limit = 200) =>
    request('/api/session/messages', { query: { session_id: sessionId, limit } }),

  /** 删除会话及其全部内容——不可恢复，调用方必须先让用户确认 */
  remove: (sessionId) =>
    request(`/api/sessions/${encodeURIComponent(sessionId)}`, { method: 'DELETE' }),
}

export const memoryApi = {
  /** 长期记忆·画像（含正在注入的文本块） */
  profile: (sessionId) => request('/api/memory/profile', { query: { session_id: sessionId } }),

  /** 记忆全景：短期 + 长期 + 经验 + 草稿 */
  overview: (sessionId) =>
    request('/api/memory/overview', { query: { session_id: sessionId }, timeout: 20000 }),

  /** 忘掉一条 */
  forget: (sessionId, entryId) =>
    request(`/api/memory/profile/${encodeURIComponent(entryId)}`, {
      method: 'DELETE',
      query: { session_id: sessionId },
    }),

  /** 忘掉关于我的一切 */
  forgetAll: (sessionId) =>
    request('/api/memory/profile', { method: 'DELETE', query: { session_id: sessionId } }),

  stats: () => request('/api/memory/stats'),
}

export const patchesApi = {
  list: () => request('/api/patches'),

  /** 批准并发布（后端会跑回归样例，未通过的会放在 validation.cases 里返回） */
  approve: (patchId) =>
    request(`/api/patches/${encodeURIComponent(patchId)}/approve`, { method: 'POST' }),

  reject: (patchId) =>
    request(`/api/patches/${encodeURIComponent(patchId)}/reject`, { method: 'POST' }),
}

export const featuresApi = {
  list: () => request('/api/features'),

  toggleSelfEvolution: (enabled, persist = true) =>
    request('/api/features/self-evolution', {
      method: 'POST',
      body: { enabled, persist },
    }),
}

export const reflectionsApi = {
  list: () => request('/api/reflections'),

  /** 请求一次后台复盘（异步任务，返回后需要重新拉列表） */
  request: () => request('/api/reflections/request', { method: 'POST' }),
}

/**
 * 拼一个可直接放进 `href` / `<img src>` 的地址（不走 fetch，因此没有超时与错误包装）。
 * 用于产物下载这类"交给浏览器"的场景。
 */
export function apiUrl(path, query) {
  return buildUrl(path, query)
}
