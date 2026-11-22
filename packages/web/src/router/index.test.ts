import { describe, it, expect, vi, beforeEach } from 'vitest'

// 桩掉懒加载页面，避免导航时拉入真实布局与 store 依赖
vi.mock('@/layouts/AppLayout.vue', () => ({ default: { name: 'AppLayoutStub', template: '<div />' } }))
vi.mock('@/layouts/AppShell.vue', () => ({ default: { name: 'AppShellStub', template: '<div />' } }))
vi.mock('@/pages/LoginPage.vue', () => ({ default: { name: 'LoginPageStub', template: '<div />' } }))
vi.mock('@/pages/DistillPage.vue', () => ({ default: { name: 'DistillPageStub', template: '<div />' } }))
vi.mock('@/pages/EmptyProjectPage.vue', () => ({ default: { name: 'EmptyProjectStub', template: '<div />' } }))
vi.mock('@/pages/ChatPage.vue', () => ({ default: { name: 'ChatPageStub', template: '<div />' }, invalidateLatestConversation: vi.fn() }))
vi.mock('@/pages/WorkspacePage.vue', () => ({ default: { name: 'WorkspacePageStub', template: '<div />' } }))
vi.mock('@/pages/PublishesPage.vue', () => ({ default: { name: 'PublishesPageStub', template: '<div />' } }))
vi.mock('@/pages/SchedulesPage.vue', () => ({ default: { name: 'SchedulesPageStub', template: '<div />' } }))
vi.mock('@/pages/TokenConfigPage.vue', () => ({ default: { name: 'TokenConfigPageStub', template: '<div />' } }))
vi.mock('@/pages/PublishPage.vue', () => ({ default: { name: 'PublishPageStub', template: '<div />' } }))
vi.mock('@/stores/project', () => ({
  useProjectStore: () => ({
    currentProject: null,
    projects: [],
    getSavedProjectId: () => null,
  }),
}))

import router from './index'

beforeEach(() => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: true, status: 200 }))
})

describe('router — 工作台 + 炼化入口', () => {
  it('根路径落到基础工作台 /app', async () => {
    await router.push('/')
    expect(router.currentRoute.value.path).toBe('/app')
  })

  it('登录页可直接访问', async () => {
    await router.push('/login')
    expect(router.currentRoute.value.path).toBe('/login')
  })

  it('炼化 Skill 独立入口保留', async () => {
    await router.push('/distill')
    expect(router.currentRoute.value.path).toBe('/distill')
  })

  it('项目对话页保留（基础工作台能力）', async () => {
    await router.push('/projects/p1/chat')
    expect(router.currentRoute.value.path).toBe('/projects/p1/chat')
  })

  it('工作区、发布、定时任务页保留', async () => {
    await router.push('/projects/p1/workspace')
    expect(router.currentRoute.value.path).toBe('/projects/p1/workspace')
    await router.push('/projects/p1/publishes')
    expect(router.currentRoute.value.path).toBe('/projects/p1/publishes')
    await router.push('/projects/p1/schedules')
    expect(router.currentRoute.value.path).toBe('/projects/p1/schedules')
  })

  it('API Key / Token 配置页保留', async () => {
    await router.push('/settings/tokens')
    expect(router.currentRoute.value.path).toBe('/settings/tokens')
  })

  it('未知路径回到工作台', async () => {
    await router.push('/whatever/else')
    expect(router.currentRoute.value.path).toBe('/app')
  })
})
