import { fileURLToPath, URL } from 'node:url'
import vue from '@vitejs/plugin-vue'
import { configDefaults, defineConfig } from 'vitest/config'

export default defineConfig({
  plugins: [vue()],
  resolve: { alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) } },
  server: {
    proxy: {
      '/api': 'http://localhost:8000',
      '/health': 'http://localhost:8000',
      '/metrics': 'http://localhost:8000',
      '/ws': { target: 'ws://localhost:8000', ws: true },
    },
  },
  test: { environment: 'jsdom', globals: true, exclude: [...configDefaults.exclude, 'e2e/**'] },
  build: {
    // History is route-lazy and ECharts uses only Line/Grid/Legend/Tooltip/Canvas.
    // The resulting isolated chart runtime is ~508 KiB; keep a tight, explicit budget.
    chunkSizeWarningLimit: 520,
  },
})
