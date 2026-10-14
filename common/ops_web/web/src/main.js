import { createApp } from 'vue';
import App from './App.vue';
import router from './router.js';

// 挂载全 SPA 主界面到 index.html 的 #app。
createApp(App).use(router).mount('#app');
