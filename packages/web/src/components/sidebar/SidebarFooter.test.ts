import { describe, it, expect, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import SidebarFooter from './SidebarFooter.vue'

const router = vi.hoisted(() => ({ push: vi.fn() }))
vi.mock('vue-router', () => ({ useRouter: () => router }))

const authStore = vi.hoisted(() => ({
  displayName: 'Test User',
  activeUsersCount: 1,
  logout: vi.fn(),
}))
vi.mock('@/stores/auth', () => ({ useAuthStore: () => authStore }))

describe('SidebarFooter', () => {
  it('no longer offers the publish-settings entry', () => {
    const wrapper = mount(SidebarFooter)
    const publishBtn = wrapper.findAll('button').find(b =>
      b.attributes('title') === '服务发布' || b.attributes('title') === 'Publish service',
    )
    expect(publishBtn).toBeUndefined()
  })

  it('keeps the remaining entries and token-settings navigation', async () => {
    const wrapper = mount(SidebarFooter)
    const buttons = wrapper.findAll('button')
    expect(buttons.length).toBe(2) // settings / logout

    const settingsBtn = buttons.find(b => b.attributes('title') === '密钥配置')
    expect(settingsBtn).toBeDefined()
    await settingsBtn!.trigger('click')
    expect(router.push).toHaveBeenCalledWith('/settings/tokens')
  })

  it('renders the display name with a full-name tooltip', () => {
    const wrapper = mount(SidebarFooter)
    const name = wrapper.get('.user-name')
    expect(name.text()).toBe('Test User')
    expect(name.attributes('title')).toBe('Test User')
  })

  it('omits the tooltip when there is no display name', () => {
    const original = authStore.displayName
    authStore.displayName = ''
    const wrapper = mount(SidebarFooter)
    expect(wrapper.get('.user-name').attributes('title')).toBeUndefined()
    authStore.displayName = original
  })

  it('keeps the settings and logout actions', () => {
    const wrapper = mount(SidebarFooter)
    expect(wrapper.findAll('.user-info-actions button').length).toBe(2)
  })

  it('no longer offers a language switcher (single-language UI)', () => {
    const wrapper = mount(SidebarFooter)
    const langBtn = wrapper.findAll('button').find(b =>
      b.attributes('title') === '切换语言' || b.attributes('title') === 'Switch language',
    )
    expect(langBtn).toBeUndefined()
  })
})
