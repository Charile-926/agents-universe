import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'

const api = vi.hoisted(() => ({ apiFetch: vi.fn() }))
// 部分 mock：保留真实的 ApiError 类，translateApiError 里的 instanceof 判断才成立
vi.mock('@/api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/client')>()
  return { ...actual, apiFetch: api.apiFetch }
})
// 登录成功后组件会把浏览器跳到 /app，这里去掉 basePath 前缀便于断言
vi.mock('@/utils/basePath', () => ({
  basePath: '',
  withBase: (path: string) => path,
  withApi: (path: string) => path,
}))

import LoginPage from '@/pages/LoginPage.vue'
import { useAuthStore } from '@/stores/auth'

function mountPage() {
  const pinia = createPinia()
  setActivePinia(pinia)
  return mount(LoginPage, { global: { plugins: [pinia] } })
}

describe('LoginPage — 账号密码登录', () => {
  beforeEach(() => {
    api.apiFetch.mockReset()
    // 组件提交成功后会赋值 window.location.href，避免 happy-dom 真去导航
    vi.stubGlobal('location', { href: '' })
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('默认展示用户名、密码与登录按钮', () => {
    const wrapper = mountPage()
    const inputs = wrapper.findAll('input')
    expect(inputs).toHaveLength(2)
    expect(inputs[0].attributes('placeholder')).toContain('用户名')
    expect(inputs[1].attributes('type')).toBe('password')
    expect(wrapper.find('.login-submit').text()).toBe('登录')
    expect(wrapper.text()).toContain('使用账号与密码登录')
  })

  it('登录成功后写入会话并跳转工作台', async () => {
    api.apiFetch.mockResolvedValue({ user_id: 'u-1', display_name: '张三' })
    const wrapper = mountPage()

    const inputs = wrapper.findAll('input')
    await inputs[0].setValue('  zhangsan  ')
    await inputs[1].setValue('secret123')
    await wrapper.find('form').trigger('submit')
    await flushPromises()

    expect(api.apiFetch).toHaveBeenCalledWith(
      '/api/auth/login',
      expect.objectContaining({ method: 'POST' }),
    )
    const body = JSON.parse((api.apiFetch.mock.calls[0][1] as { body: string }).body)
    expect(body.username).toBe('zhangsan') // 前后空格已裁掉
    expect(body.password).toBe('secret123')

    const store = useAuthStore()
    expect(store.isAuthenticated).toBe(true)
    expect(store.displayName).toBe('张三')
    expect(window.location.href).toBe('/app')
  })

  it('登录失败展示中文提示，不暴露服务端原文', async () => {
    api.apiFetch.mockRejectedValue(new Error('Internal Server Error: /var/lib/api.py'))
    const wrapper = mountPage()

    const inputs = wrapper.findAll('input')
    await inputs[0].setValue('zhangsan')
    await inputs[1].setValue('wrong-pass')
    await wrapper.find('form').trigger('submit')
    await flushPromises()

    const error = wrapper.find('.login-error')
    expect(error.exists()).toBe(true)
    expect(error.text()).toBe('操作失败，请稍后重试。')
    expect(wrapper.text()).not.toContain('/var/lib')
    expect(useAuthStore().isAuthenticated).toBe(false)
  })

  it('切到注册模式后出现昵称字段并改用注册接口', async () => {
    api.apiFetch.mockResolvedValue({ user_id: 'u-2', display_name: '李四' })
    const wrapper = mountPage()

    await wrapper.find('.login-switch').trigger('click')
    expect(wrapper.findAll('input')).toHaveLength(3)
    expect(wrapper.find('.login-submit').text()).toBe('注册并登录')

    const inputs = wrapper.findAll('input')
    await inputs[0].setValue('lisi')
    await inputs[2].setValue('secret123')
    await wrapper.find('form').trigger('submit')
    await flushPromises()

    expect(api.apiFetch).toHaveBeenCalledWith(
      '/api/auth/register',
      expect.objectContaining({ method: 'POST' }),
    )
    expect(useAuthStore().displayName).toBe('李四')
  })
})
