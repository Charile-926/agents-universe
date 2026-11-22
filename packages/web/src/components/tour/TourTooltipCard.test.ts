import { describe, it, expect, vi, afterEach } from 'vitest'
import { mount } from '@vue/test-utils'
import TourTooltipCard from './TourTooltipCard.vue'
import type { TourStep } from '@/tour/steps'

const tour = vi.hoisted(() => ({
  stepIndex: 0,
  waiting: false,
  next: vi.fn(),
  skip: vi.fn(),
  finish: vi.fn(),
}))
vi.mock('@/stores/tour', () => ({ useTourStore: () => tour }))

afterEach(() => {
  vi.clearAllMocks()
})

const WELCOME_STEP: TourStep = {
  id: 'welcome',
  titleKey: 'tour.welcome.title',
  bodyKey: 'tour.welcome.body',
  center: true,
}

describe('TourTooltipCard', () => {
  it('renders the current step title and body', () => {
    const wrapper = mount(TourTooltipCard, { props: { step: WELCOME_STEP }})
    expect(wrapper.find('.tour-tooltip-title').text()).toBe('欢迎使用 Agents Universe')
    expect(wrapper.text()).toContain('接下来我会一步步带你熟悉全部核心功能')
  })

  it('advances to the next step when 下一步 is clicked', async () => {
    const wrapper = mount(TourTooltipCard, { props: { step: WELCOME_STEP }})
    const nextBtn = wrapper.get('.tour-next-btn')
    expect(nextBtn.text()).toBe('下一步')
    await nextBtn.trigger('click')
    expect(tour.next).toHaveBeenCalled()
  })

  it('has no choice buttons any more (single-language UI)', () => {
    const wrapper = mount(TourTooltipCard, { props: { step: WELCOME_STEP }})
    expect(wrapper.find('.tour-choices').exists()).toBe(false)
    expect(wrapper.find('.tour-choice-btn').exists()).toBe(false)
  })
})
