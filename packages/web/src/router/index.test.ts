import { describe, it, expect, vi, beforeEach } from 'vitest'

// 桩掉懒加载页面，避免导航时拉入真实布局依赖
vi.mock('@/layouts/AppShell.vue', () => ({ default: { name: 'AppShellStub', template: '<div />' } }))
vi.mock('@/pages/DistillPage.vue', () => ({ default: { name: 'DistillPageStub', template: '<div />' } }))
vi.mock('@/pages/LoginPage.vue', () => ({ default: { name: 'LoginPageStub', template: '<div />' } }))

import router from './index'

beforeEach(() => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: true, status: 200 }))
})

describe('router — 单页路由表', () => {
  it('根路径进入炼化工作台', async () => {
    await router.push('/')
    expect(router.currentRoute.value.path).toBe('/')
  })

  it('/app 进入炼化工作台', async () => {
    await router.push('/app')
    expect(router.currentRoute.value.path).toBe('/app')
  })

  it('登录页可直接访问', async () => {
    await router.push('/login')
    expect(router.currentRoute.value.path).toBe('/login')
  })

  it('已下线的项目/对话路径回到工作台', async () => {
    await router.push('/projects/p1/chat')
    expect(router.currentRoute.value.path).toBe('/app')
  })

  it('未知路径回到工作台', async () => {
    await router.push('/settings/tokens')
    expect(router.currentRoute.value.path).toBe('/app')
  })
})
