import { createApp } from 'vue'
import { createPinia } from 'pinia'
import App from './App.vue'
import router from './router'
import { i18n } from './i18n'
import './index.css'
// 工作台样式：layout.css 是主布局。
import './styles/layout.css'
// 单页外壳（登录后的 /distill 独立页）补充样式。
import './styles/app.css'
// 工作台排版覆盖层：顶栏下线后的输入框 / 智能体条 / 左侧一级导航，必须最后引入。
import './styles/workbench.css'

createApp(App).use(createPinia()).use(router).use(i18n).mount('#root')
