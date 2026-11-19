// M0-04: 前端 API / WebSocket 配置
// 通过环境变量或默认配置,支持 dev / prod 切换
// 使用方式: import { API_BASE, WS_BASE } from './config'

const _env = (typeof process !== 'undefined' && process.env) || {};

export const API_BASE = _env.VITE_API_BASE
  || import.meta?.env?.VITE_API_BASE
  || 'http://localhost:8000';

export const WS_BASE = _env.VITE_WS_BASE
  || import.meta?.env?.VITE_WS_BASE
  || 'ws://localhost:8000';

// 本地单用户：没有鉴权。以下函数保留只为兼容旧 import，均为 no-op。
export function getOwnerToken() {
  return '';
}

export function setOwnerToken(token) {
  // no-op：本地单用户无需管理令牌
}

export function authHeaders() {
  return {};
}

// 统一的后端请求入口
export function apiFetch(url, options = {}) {
  return fetch(url, options);
}

// 兼容 .env 文件写法(即使项目当前不使用 vite)
export default { API_BASE, WS_BASE, getOwnerToken, setOwnerToken, authHeaders, apiFetch };
