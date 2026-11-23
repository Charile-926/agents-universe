import { createRouter, createWebHistory } from 'vue-router'
import { basePath, withApi } from '@/utils/basePath'
import { useProjectStore } from '@/stores/project'

/**
 * 路由表：基础工作台（项目 / 对话 / 工作区 / 发布 / 定时任务 / API Key 配置）
 * + 本地账号登录 + 炼化 Skill。
 *
 * 炼化 Skill 只是工作台里的一个核心能力：它是左侧栏的一级导航入口，
 * /distill 页面复用工作台外壳（AppLayout），不再单独套一层 AppShell。
 */
const router = createRouter({
  history: createWebHistory(basePath || '/'),
  routes: [
    {
      path: '/login',
      component: () => import('@/pages/LoginPage.vue'),
    },
    {
      path: '/distill',
      component: () => import('@/layouts/AppLayout.vue'),
      children: [
        {
          path: '',
          component: () => import('@/pages/DistillPage.vue'),
        },
      ],
    },
    {
      path: '/app',
      component: () => import('@/layouts/AppLayout.vue'),
      children: [
        {
          path: '',
          component: () => import('@/pages/EmptyProjectPage.vue'),
        },
      ],
    },
    {
      path: '/projects/:projectId',
      component: () => import('@/layouts/AppLayout.vue'),
      children: [
        {
          path: '',
          redirect: (to) => ({ path: `/projects/${to.params.projectId}/chat` }),
        },
        {
          path: 'chat',
          component: () => import('@/pages/ChatPage.vue'),
        },
        {
          path: 'workspace',
          component: () => import('@/pages/WorkspacePage.vue'),
        },
        {
          path: 'publishes',
          component: () => import('@/pages/PublishesPage.vue'),
        },
        {
          path: 'schedules',
          component: () => import('@/pages/SchedulesPage.vue'),
        },
        // 知识库与脚本已并入统一工作区 tab，旧链接重定向以保住书签。
        {
          path: 'knowledge',
          redirect: (to) => ({ path: `/projects/${to.params.projectId}/workspace` }),
        },
        {
          path: 'scripts',
          redirect: (to) => ({ path: `/projects/${to.params.projectId}/workspace` }),
        },
      ],
    },
    {
      path: '/settings/tokens',
      component: () => import('@/pages/TokenConfigPage.vue'),
    },
    {
      // 发布管理已并入项目布局，旧书签落到当前项目的 publishes。
      path: '/settings/publishes',
      redirect: () => {
        const store = useProjectStore()
        const wanted = store.currentProject?.project_id ?? store.getSavedProjectId()
        if (store.projects.length > 0) {
          const match = store.projects.find(p => p.project_id === wanted) ?? store.projects[0]
          return { path: `/projects/${match.project_id}/publishes` }
        }
        return wanted ? { path: `/projects/${wanted}/publishes` } : '/app'
      },
    },
    {
      // 对外已发布智能体的独立对话页，刻意放在 AppLayout 之外。
      path: '/p/:publishId',
      component: () => import('@/pages/PublishPage.vue'),
    },
    {
      path: '/',
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
  if (to.path === '/login') {
    next()
    return
  }
  if (authChecked) {
    next()
    return
  }
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
