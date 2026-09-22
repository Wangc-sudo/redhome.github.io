/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** 后端 Bearer token（BI_WEB_TOKEN）；dev 由 vite proxy 注入，此处用于直连场景。 */
  readonly VITE_BI_WEB_TOKEN?: string;
  /** VITE_MOCK=1 → 不请求后端，用 src/data/mock/fixtures.ts 的 CubeSchema fixture。 */
  readonly VITE_MOCK?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
