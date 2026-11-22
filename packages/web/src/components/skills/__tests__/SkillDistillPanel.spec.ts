import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'

const api = vi.hoisted(() => ({
  listSources: vi.fn(),
  generate: vi.fn(),
}))
vi.mock('@/api/skillsDistill', () => ({ skillsDistillApi: api }))

import SkillDistillPanel from '../SkillDistillPanel.vue'

const sources = {
  items: [
    {
      conversation_id: 'c-1',
      title: '季度报表自动化',
      project_name: '数据分析',
      updated_at: '2026-11-22T10:00:00Z',
      message_count: 12,
      tool_call_count: 3,
    },
  ],
  total: 1,
  limit: 20,
  offset: 0,
}

describe('SkillDistillPanel', () => {
  beforeEach(() => {
    api.listSources.mockReset()
    api.generate.mockReset()
    api.listSources.mockResolvedValue(sources)
    api.generate.mockResolvedValue({
      markdown: '---\nslug: "distilled-x"\n---\n\n# Skill',
      stats: {
        conversations: 1,
        messages: 12,
        tool_calls: 3,
        tools: ['filesystem'],
        truncated: false,
      },
    })
  })

  it('挂载后列出当前账号的会话', async () => {
    const wrapper = mount(SkillDistillPanel)
    await flushPromises()
    expect(api.listSources).toHaveBeenCalledTimes(1)
    expect(wrapper.text()).toContain('季度报表自动化')
    expect(wrapper.text()).toContain('3 次工具调用')
  })

  it('未选择来源时生成按钮不可用', async () => {
    const wrapper = mount(SkillDistillPanel)
    await flushPromises()
    expect(wrapper.find('.distill-generate').attributes('disabled')).toBeDefined()
    await wrapper.find('.distill-generate').trigger('click')
    expect(api.generate).not.toHaveBeenCalled()
  })

  it('选中会话后生成并展示 Skill 草稿', async () => {
    const wrapper = mount(SkillDistillPanel)
    await flushPromises()
    await wrapper.find('.distill-list input[type="checkbox"]').setValue(true)
    await wrapper.find('.distill-generate').trigger('click')
    await flushPromises()

    expect(api.generate).toHaveBeenCalledWith({
      conversation_ids: ['c-1'],
      all_conversations: false,
      include_tool_calls: true,
    })
    expect(wrapper.find('.distill-output').text()).toContain('slug: "distilled-x"')
    expect(wrapper.text()).toContain('汇总了 1 个会话')
  })

  it('“汇总全部会话”走 all_conversations 分支', async () => {
    const wrapper = mount(SkillDistillPanel)
    await flushPromises()
    const useAll = wrapper.findAll('input[type="checkbox"]')[0]
    await useAll.setValue(true)
    await wrapper.find('.distill-generate').trigger('click')
    await flushPromises()

    expect(api.generate).toHaveBeenCalledWith({
      conversation_ids: [],
      all_conversations: true,
      include_tool_calls: true,
    })
  })

  it('接口失败时给出中文提示', async () => {
    api.generate.mockRejectedValueOnce(new Error('boom'))
    const wrapper = mount(SkillDistillPanel)
    await flushPromises()
    await wrapper.findAll('input[type="checkbox"]')[1].setValue(true)
    await wrapper.find('.distill-generate').trigger('click')
    await flushPromises()
    expect(wrapper.find('.distill-error').text()).toBe('操作失败，请稍后重试。')
  })
})
