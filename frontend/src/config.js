// 前端地址配置 —— 只回答"后端在哪"，不含任何请求逻辑。
//
// HTTP 请求统一走 `api/client.js`（超时、错误结构、具名接口都在那里）；
// WebSocket 走 `websocket.js`。页面不直接读这里。
//
// 本地单用户：没有鉴权头、没有令牌。历史上的 getOwnerToken / authHeaders 等
// no-op 兼容函数已随多用户体系一起删除 —— 留着只会让新读者以为"这里可能要做鉴权"。

const _env = (typeof process !== 'undefined' && process.env) || {}

export const API_BASE = _env.VITE_API_BASE
  || import.meta?.env?.VITE_API_BASE
  || 'http://localhost:8000'

export const WS_BASE = _env.VITE_WS_BASE
  || import.meta?.env?.VITE_WS_BASE
  || 'ws://localhost:8000'

export default { API_BASE, WS_BASE }
