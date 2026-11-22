import { describe, it, expect, vi } from 'vitest'
import { mount } from '@vue/test-utils'

vi.mock('@/components/skills/SkillDistillPanel.vue', () => ({
  default: { name: 'SkillDistillPanelStub', template: '<div class="panel-stub" />' },
}))

import DistillPage from '@/pages/DistillPage.vue'

describe('DistillPage — 单页炼化工作台', () => {
  it('标题与说明为中文，并只挂载炼化面板', () => {
    const wrapper = mount(DistillPage)
    expect(wrapper.find('.distill-page-title').text()).toBe('炼化 Skill')
    expect(wrapper.find('.distill-page-sub').text()).toContain('聊天记录与工具调用记录')
    expect(wrapper.findAll('.panel-stub')).toHaveLength(1)
  })

  it('不再包含语言切换、侧栏等已下线的入口', () => {
    const wrapper = mount(DistillPage)
    const html = wrapper.html()
    expect(html).not.toContain('language')
    expect(html).not.toContain('sidebar')
  })
})
