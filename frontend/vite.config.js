import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// M0-08: 加入 vitest 测试配置
export default defineConfig({
  plugins: [vue()],
  server: {
    port: 8888,
    hmr: false,  // 禁用 HMR，避免热更新导致 WebSocket 重连
    proxy: {
      '/api': {
        target: 'http://localhost:8000',
        changeOrigin: true
      },
      '/pubsub': {
        target: 'ws://localhost:8000',
        ws: true,
        changeOrigin: true,
      },
    }
  },
  test: {
    environment: 'jsdom',
    globals: true,
    include: ['tests/**/*.{test,spec}.{js,ts}'],
  },
})
