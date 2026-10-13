import { defineConfig } from 'vite';
import vue from '@vitejs/plugin-vue';

// 组织编排 SPA 构建配置。
//
// 产物直接落到 ops-web 的静态目录（common/ops_web/static/dist），
// 由 FastAPI 以 StaticFiles(/static → common/ops_web/static) 托管；
// SPA 入口路由为 GET /members，返回 dist/index.html。
// 因此 base 必须是 /static/dist/，index.html 内对资源的引用才会带上该前缀。
// 生产环境运行时不装 node、不构建，产物提交入库、随部署拉取即生效。
export default defineConfig({
  base: '/static/dist/',
  plugins: [vue()],
  build: {
    outDir: '../static/dist',
    emptyOutDir: true,
  },
});