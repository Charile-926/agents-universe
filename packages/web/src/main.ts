import { createApp } from 'vue'
import { createPinia } from 'pinia'
import App from './App.vue'
import router from './router'
import { i18n } from './i18n'
import './index.css'
// 工作台与引导的全部样式：layout.css 是主布局，tour.css 是引导浮层。
import './styles/layout.css'
import './styles/tour.css'
// 单页外壳（登录后的 /distill 独立页）补充样式，放在最后避免覆盖工作台规则。
import './styles/app.css'

createApp(App).use(createPinia()).use(router).use(i18n).mount('#root')
