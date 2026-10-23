import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

export default defineConfig({
  plugins: [vue()],
  base: '/kb/',
  build: { outDir: 'dist' },
  server: {
    proxy: { '/kb/api': 'http://localhost:8080' },
  },
})
