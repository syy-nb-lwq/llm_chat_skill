// M0-04: 前端 API / WebSocket 配置
// 通过环境变量或默认配置,支持 dev / prod 切换
// 使用方式: import { API_BASE, WS_BASE } from './config'

const _env = (typeof process !== 'undefined' && process.env) || {};

// 开发模式(通过 Vite dev server): 使用相对路径,走 Vite proxy
// 生产模式:需要配置 VITE_API_BASE / VITE_WS_BASE 指向实际后端地址
export const API_BASE = _env.VITE_API_BASE
  || import.meta?.env?.VITE_API_BASE
  || (
    import.meta?.env?.DEV !== false
      ? ''  // dev: 相对路径,走 Vite proxy
      : 'http://localhost:8000'
  );

export const WS_BASE = _env.VITE_WS_BASE
  || import.meta?.env?.VITE_WS_BASE
  || (
    import.meta?.env?.DEV !== false
      ? ''  // dev: 相对路径,走 Vite proxy
      : 'ws://localhost:8000'
  );

// 兼容 .env 文件写法(即使项目当前不使用 vite)
export default { API_BASE, WS_BASE };
