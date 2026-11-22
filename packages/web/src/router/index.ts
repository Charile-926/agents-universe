import { createRouter, createWebHistory } from 'vue-router'
import { basePath, withApi } from '@/utils/basePath'

/**
 * 单页路由：登录页 + 炼化工作台。
 *
 * 原多页面工作区（对话 / 工作区 / 发布 / 定时任务）已下线，所有旧链接统一
 * 重定向到工作台，避免书签 404。
 */
const router = createRouter({
  history: createWebHistory(basePath || '/'),
  routes: [
    {
      path: '/',
      component: () => import('@/layouts/AppShell.vue'),
      children: [
        {
          path: '',
          component: () => import('@/pages/DistillPage.vue'),
        },
      ],
    },
    {
      path: '/app',
      component: () => import('@/layouts/AppShell.vue'),
      children: [
        {
          path: '',
          component: () => import('@/pages/DistillPage.vue'),
        },
      ],
    },
    {
      path: '/login',
      component: () => import('@/pages/LoginPage.vue'),
    },
    {
      path: '/projects/:pathMatch(.*)*',
      redirect: '/app',
    },
    {
      path: '/:pathMatch(.*)*',
      redirect: '/app',
    },
  ],
})

// 首次导航校验一次登录态，之后信任会话（401 由 API 层统一跳登录）
let authChecked = false

router.beforeEach(async (to, _from, next) => {
  if (to.path === '/login') { next(); return }
  if (authChecked) { next(); return }
  try {
    const res = await fetch(withApi('/api/me'), { credentials: 'include' })
    if (res.status === 401) {
      next('/login')
      return
    }
    authChecked = true
    next()
  } catch {
    // 网络异常先放行，由后端鉴权兜底
    authChecked = true
    next()
  }
})

export default router
