import { defineConfig } from 'vitest/config';

export default defineConfig({
  test: {
    // 默认 node（纯逻辑单测快）；需要 DOM 的用例用文件头 `// @vitest-environment jsdom` 覆盖
    environment: 'node',
    include: ['src/**/*.test.ts', 'src/**/*.test.tsx'],
    // 冒烟用例走 mock 后端，不依赖 18080 是否启动
    env: { VITE_MOCK: '1' },
  },
});
