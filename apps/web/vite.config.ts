import { fileURLToPath, URL } from 'node:url'
import vue from '@vitejs/plugin-vue'
import { configDefaults, defineConfig } from 'vitest/config'

const backend = process.env.FIREBOT_DEV_BACKEND || 'http://localhost:8000'

export default defineConfig({
  plugins: [vue()],
  resolve: { alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) } },
  server: {
    proxy: {
      '/api': backend,
      '/health': backend,
      '/metrics': backend,
      '/ws': { target: backend.replace(/^http/, 'ws'), ws: true },
    },
  },
  test: { environment: 'jsdom', globals: true, exclude: [...configDefaults.exclude, 'e2e/**'] },
  build: {
    // History is route-lazy and ECharts uses only Line/Grid/Legend/Tooltip/Canvas.
    // The resulting isolated chart runtime is ~508 KiB; keep a tight, explicit budget.
    chunkSizeWarningLimit: 700,
    rollupOptions: {
      output: {
        manualChunks: {
          'tdesign-vendor': ['tdesign-vue-next', 'tdesign-icons-vue-next'],
          'vue-vendor': ['vue', 'vue-router', 'pinia', 'axios'],
        },
      },
    },
  },
})
