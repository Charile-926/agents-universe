import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount } from '@vue/test-utils'
import { i18n } from '@/i18n'
import AppLayout from './AppLayout.vue'

const routerPush = vi.hoisted(() => vi.fn())
const routeState = vi.hoisted(() => ({
  path: '/projects/p-1/publishes',
  params: { projectId: 'p-1' } as Record<string, string>,
  fullPath: '/projects/p-1/publishes',
}))
vi.mock('vue-router', () => ({
  useRoute: () => routeState,
  useRouter: () => ({ push: routerPush }),
  RouterView: { name: 'RouterViewStub', template: '<div />' },
}))

vi.mock('@/composables/useWebSocket', () => ({ closeAllConnections: vi.fn() }))
vi.mock('@/composables/useProjectData', () => ({ useProjectData: vi.fn() }))
vi.mock('@/pages/ChatPage.vue', () => ({
  default: { name: 'ChatPageStub', template: '<div />' },
  invalidateLatestConversation: vi.fn(),
}))

const projectStore = vi.hoisted(() => ({
  currentProject: { project_id: 'p-1' } as { project_id: string } | null,
  projects: [],
  setCurrentProject: vi.fn(),
  setProjects: vi.fn(),
}))
vi.mock('@/stores/project', () => ({ useProjectStore: () => projectStore }))
const agentStore = vi.hoisted(() => ({
  currentAgent: null,
  agents: [],
  modelConfigs: [],
  selectedConfigId: '',
  setSelectedConfigId: vi.fn(),
  fetchModelConfigs: vi.fn(),
  fetchAgents: vi.fn(),
}))
vi.mock('@/stores/agent', () => ({
  useAgentStore: () => agentStore,
  AUTO_MODEL_CONFIG_ID: '__auto__',
}))
const conversationStore = vi.hoisted(() => ({
  messages: [] as unknown[],
  isStreaming: false,
  isThinking: false,
  conversationId: null as string | null,
  tokensUsed: 0,
  reset: vi.fn(),
  loadHistory: vi.fn(),
}))
vi.mock('@/stores/conversation', () => ({ useConversationStore: () => conversationStore }))

vi.mock('@/api/projects', () => ({ projectsApi: { getProjects: vi.fn() } }))
vi.mock('@/api/conversations', () => ({ conversationsApi: { compress: vi.fn() } }))

// Child components — the topnav under test lives in this layout, not in them.
vi.mock('@/components/sidebar/ProjectTree.vue', () => ({ default: { name: 'ChildStub', template: '<div />' } }))
vi.mock('@/components/sidebar/AgentSwitcher.vue', () => ({ default: { name: 'ChildStub', template: '<div />' } }))
vi.mock('@/components/sidebar/AgentPickerDialog.vue', () => ({ default: { name: 'ChildStub', template: '<div />' } }))
vi.mock('@/components/sidebar/SidebarFooter.vue', () => ({ default: { name: 'ChildStub', template: '<div />' } }))
vi.mock('@/components/knowledge/ContextMeter.vue', () => ({ default: { name: 'ChildStub', template: '<div />' } }))
vi.mock('@/components/conversations/ConversationTreePanel.vue', () => ({ default: { name: 'ChildStub', template: '<div />' } }))
vi.mock('@/components/memory/MemoryPanel.vue', () => ({ default: { name: 'ChildStub', template: '<div />' } }))

// The conversation store mock is shared mutable state — reset the fields the
// topnav reads so no test inherits the previous one's ledger/conversation.
beforeEach(() => {
  Object.defineProperty(window, 'innerWidth', { value: 1280, configurable: true })
  routeState.params = { projectId: 'p-1' }
  projectStore.currentProject = { project_id: 'p-1' }
  routerPush.mockClear()
  conversationStore.conversationId = null
  conversationStore.tokensUsed = 0
  conversationStore.messages = []
  agentStore.modelConfigs = []
  agentStore.selectedConfigId = ''
  agentStore.setSelectedConfigId.mockClear()
})

describe('AppLayout sidebar primary navigation', () => {
  it('renders 炼化 Skill 在最上，其后是工作区 / 定时任务 / 记忆', () => {
    routeState.path = '/projects/p-1/schedules'
    const wrapper = mount(AppLayout)
    const items = wrapper.findAll('.sidebar-nav-item')
    expect(items.map(t => t.text().trim())).toEqual([
      '炼化 Skill', '工作区', '定时任务', '记忆',
    ])
    expect(items[2].classes()).toContain('active')
    expect(items[1].classes()).not.toContain('active')
  })

  it('marks the schedules entry active on the schedules segment', () => {
    routeState.path = '/projects/p-1/schedules'
    const wrapper = mount(AppLayout)
    const items = wrapper.findAll('.sidebar-nav-item')
    expect(items[2].classes()).toContain('active')
    expect(items[1].classes()).not.toContain('active')
  })

  it('navigates to the workspace page when 工作区 is clicked', async () => {
    routeState.path = '/projects/p-1/chat'
    const wrapper = mount(AppLayout)
    await wrapper.findAll('.sidebar-nav-item')[1].trigger('click')
    expect(routerPush).toHaveBeenCalledWith('/projects/p-1/workspace')
  })

  it('opens the standalone 炼化 Skill page from the first nav entry', async () => {
    routeState.path = '/projects/p-1/chat'
    const wrapper = mount(AppLayout)
    await wrapper.findAll('.sidebar-nav-item')[0].trigger('click')
    expect(routerPush).toHaveBeenCalledWith('/distill')
  })

  it('no longer renders the retired top bar, pickers or deleted 发布/知识 entries', () => {
    routeState.path = '/projects/p-1/chat'
    const wrapper = mount(AppLayout)
    expect(wrapper.find('.main-topbar').exists()).toBe(false)
    expect(wrapper.find('.center-topnav').exists()).toBe(false)
    expect(wrapper.find('.model-picker').exists()).toBe(false)
    expect(wrapper.find('.agent-picker-btn').exists()).toBe(false)
    const labels = wrapper.findAll('.sidebar-nav-item').map(i => i.text().trim())
    expect(labels).not.toContain('发布')
    expect(labels).not.toContain('知识')
  })
})

describe('AppLayout panel toggles', () => {
  it('localizes the sidebar/panel toggle tooltips', () => {
    // Icon-only toggles are labelled solely by their tooltip; hardcoded
    // English there showed the wrong language on a zh-CN UI.
    routeState.path = '/projects/p-1/chat'
    const wrapper = mount(AppLayout)
    expect(wrapper.find('.sidebar-left .sidebar-toggle').attributes('title'))
      .toBe(i18n.global.t('layout.toggleSidebar'))
    expect(wrapper.find('.panel-right .sidebar-toggle').attributes('title'))
      .toBe(i18n.global.t('layout.togglePanel'))
  })

  it('clicking a project entry without a project shows a usage hint', async () => {
    routeState.path = '/app'
    routeState.params = {}
    projectStore.currentProject = null
    const wrapper = mount(AppLayout)
    await wrapper.findAll('.sidebar-nav-item')[1].trigger('click')
    expect(routerPush).not.toHaveBeenCalled()
    expect(wrapper.find('.app-hint-toast').text())
      .toBe(i18n.global.t('layout.projectRequiredHint'))
    projectStore.currentProject = { project_id: 'p-1' }
  })

  it('offers an expand button once the sidebar is collapsed', async () => {
    routeState.path = '/projects/p-1/chat'
    const wrapper = mount(AppLayout)
    expect(wrapper.find('.sidebar-expand-btn').exists()).toBe(false)
    await wrapper.find('.sidebar-left .sidebar-toggle').trigger('click')
    const expand = wrapper.find('.sidebar-expand-btn')
    expect(expand.exists()).toBe(true)
    await expand.trigger('click')
    expect(wrapper.find('.sidebar-left').classes()).not.toContain('collapsed')
  })

  it('right drawer only carries 记忆 now that 知识/炼化 were removed from it', () => {
    routeState.path = '/projects/p-1/chat'
    const wrapper = mount(AppLayout)
    const items = wrapper.findAll('.sidebar-nav-item')
    expect(items[3].text().trim()).toBe('记忆')
  })
})
