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

// C-01: owner token(localStorage 保存,与管理接口的 OWNER_TOKEN 对应)
const OWNER_TOKEN_KEY = 'skill_agent_owner_token';

export function getOwnerToken() {
  try {
    return localStorage.getItem(OWNER_TOKEN_KEY) || '';
  } catch (e) {
    return '';
  }
}

export function setOwnerToken(token) {
  try {
    const value = (token || '').trim();
    if (value) {
      localStorage.setItem(OWNER_TOKEN_KEY, value);
    } else {
      localStorage.removeItem(OWNER_TOKEN_KEY);
    }
  } catch (e) {
    console.warn('[Auth] 无法保存 owner token', e);
  }
}

export function authHeaders() {
  const token = getOwnerToken();
  return token ? { Authorization: `Bearer ${token}` } : {};
}

// 统一的后端请求入口:自动附带 owner token
export function apiFetch(url, options = {}) {
  const headers = { ...(options.headers || {}), ...authHeaders() };
  return fetch(url, { ...options, headers });
}

// 兼容 .env 文件写法(即使项目当前不使用 vite)
export default { API_BASE, WS_BASE, getOwnerToken, setOwnerToken, authHeaders, apiFetch };
