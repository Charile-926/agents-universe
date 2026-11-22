import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'

const api = vi.hoisted(() => ({ apiFetch: vi.fn() }))
vi.mock('@/api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/client')>()
  return { ...actual, apiFetch: api.apiFetch }
})
vi.mock('@/utils/basePath', () => ({
  basePath: '',
  withBase: (path: string) => path,
  withApi: (path: string) => path,
}))

import AppShell from '@/layouts/AppShell.vue'
import { useAuthStore } from '@/stores/auth'

function mountShell() {
  const pinia = createPinia()
  setActivePinia(pinia)
  useAuthStore().setUser({ userId: 'u-1', displayName: '张三' })
  return mount(AppShell, {
    global: { plugins: [pinia], stubs: { RouterView: { template: '<div class="route-slot" />' } } },
  })
}

describe('AppShell — 工作台外壳', () => {
  beforeEach(() => {
    api.apiFetch.mockReset()
    api.apiFetch.mockResolvedValue(undefined)
    vi.stubGlobal('location', { href: '' })
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('顶栏展示品牌、当前用户与退出入口', () => {
    const wrapper = mountShell()
    expect(wrapper.text()).toContain('Agents Universe')
    expect(wrapper.find('.app-user-name').text()).toBe('张三')
    expect(wrapper.find('.app-logout').text()).toBe('退出登录')
    expect(wrapper.find('.route-slot').exists()).toBe(true)
  })

  it('未登录时不显示占位用户名', () => {
    const pinia = createPinia()
    setActivePinia(pinia)
    const wrapper = mount(AppShell, {
      global: { plugins: [pinia], stubs: { RouterView: true } },
    })
    expect(wrapper.find('.app-user-name').text()).toBe('用户')
  })

  it('退出时调用登出接口、清空会话并回到登录页', async () => {
    const wrapper = mountShell()
    await wrapper.find('.app-logout').trigger('click')
    await flushPromises()

    expect(api.apiFetch).toHaveBeenCalledWith('/api/auth/logout', expect.objectContaining({ method: 'POST' }))
    expect(useAuthStore().isAuthenticated).toBe(false)
    expect(window.location.href).toBe('/login')
  })

  it('登出接口失败也要清空本地会话', async () => {
    api.apiFetch.mockRejectedValue(new Error('network down'))
    const wrapper = mountShell()
    await wrapper.find('.app-logout').trigger('click')
    await flushPromises()

    expect(useAuthStore().isAuthenticated).toBe(false)
    expect(useAuthStore().displayName).toBeNull()
  })
})
