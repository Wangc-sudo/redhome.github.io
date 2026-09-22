import { defineConfig, loadEnv } from 'vite';
import react from '@vitejs/plugin-react';

// dev 时把 /api 代理到现有后端（18080），复用真实 bi_web 契约，无需改后端即可联调。
// build.sourcemap=true 用于线上错误监控定位源码（见 ARCHITECTURE.md §6）。
//
// Bearer：后端开 BI_WEB_TOKEN 后 /api 会要求 Authorization 头，浏览器直连不会带，
// 因此由 dev proxy 统一注入（见 .env.example）。生产环境由网关注入，不走这里。
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), '');
  const token = (env.BI_WEB_TOKEN ?? env.VITE_BI_WEB_TOKEN ?? '').trim();

  return {
    plugins: [react()],
    server: {
      port: 18090,
      proxy: {
        '/api': {
          target: 'http://127.0.0.1:18080',
          changeOrigin: true,
          headers: token ? { Authorization: `Bearer ${token}` } : undefined,
        },
      },
    },
    build: {
      sourcemap: true,
      rollupOptions: {
        output: {
          // 分包只切依赖，不动业务代码：echarts 1.3MB 单独成块（首屏非图表页不必等它），
          // react / 布局库同理。业务代码仍随主包，避免改动任何渲染逻辑。
          manualChunks: {
            react: ['react', 'react-dom'],
            echarts: ['echarts', 'echarts-for-react'],
            grid: ['react-grid-layout', 'react-resizable'],
          },
        },
      },
    },
  };
});
