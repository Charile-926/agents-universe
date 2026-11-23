<template>
  <div class="app-layout">
    <!-- Mobile Top Bar (visible only on small screens) -->
    <div class="mobile-topbar">
      <button class="mobile-topbar-btn" @click="toggleLeft" :title="t('layout.menu')">
        <Menu :size="20" />
      </button>
      <span class="mobile-topbar-title"><Bot :size="16" class="sidebar-title-icon" /> Agents Universe</span>
      <button class="mobile-topbar-btn" @click="toggleRight" :title="t('layout.panel')">
        <PanelRight :size="20" />
      </button>
    </div>

    <!-- Mobile Backdrop — tap to close any open sidebar -->
    <Transition name="fade">
      <div
        v-if="showBackdrop"
        class="mobile-backdrop"
        @click="closeMobilePanels"
      />
    </Transition>

    <!-- Left Sidebar — ChatGPT style: new chat, projects, conversation history -->
    <aside class="sidebar-left" :class="{ collapsed: leftCollapsed }">
      <div class="sidebar-header">
        <span v-if="!leftCollapsed" class="sidebar-title"><Bot :size="16" class="sidebar-title-icon" /> Agents Universe</span>
        <button class="sidebar-toggle" @click="toggleLeft" :title="t('layout.toggleSidebar')">
          <ChevronLeft v-if="!leftCollapsed" :size="16" />
          <ChevronRight v-else :size="16" />
        </button>
      </div>
      <template v-if="!leftCollapsed">
        <button
          class="new-chat-btn"
          :class="{ 'is-inactive': !projectStore.currentProject }"
          @click="handleNewConversation"
        >
          <SquarePen :size="16" />
          <span>{{ t('layout.newChat') }}</span>
        </button>

        <!-- 一级导航：炼化 Skill 固定在最上，是核心功能入口 -->
        <nav class="sidebar-nav">
          <button
            class="sidebar-nav-item"
            :class="{ active: route.path === '/distill' }"
            data-tour-target="nav-distill"
            @click="goDistill"
          >
            <Wand2 :size="15" /><span>{{ t('skillDistill.title') }}</span>
          </button>
          <button
            class="sidebar-nav-item"
            :class="{ active: pageSegment === 'workspace', 'is-inactive': !currentProjectId }"
            @click="goToPage('workspace')"
          >
            <LayoutGrid :size="15" /><span>{{ t('layout.tabWorkspace') }}</span>
          </button>
          <button
            class="sidebar-nav-item"
            :class="{ active: pageSegment === 'schedules', 'is-inactive': !currentProjectId }"
            @click="goToPage('schedules')"
          >
            <Clock :size="15" /><span>{{ t('layout.tabSchedules') }}</span>
          </button>
          <button
            class="sidebar-nav-item"
            :class="{ active: !rightCollapsed && activeTab === 'memory' }"
            data-tour-target="panel-tab-memory"
            @click="openPanel('memory')"
          >
            <Brain :size="15" /><span>{{ t('layout.tabMemory') }}</span>
          </button>
        </nav>

        <ProjectTree @navigate="closeMobilePanels" />

        <div class="sidebar-conversations" data-tour-target="panel-tab-conversations">
          <ConversationTreePanel
            embedded
            :project-id="currentProjectId"
            @new-conversation="handleNewConversation"
          />
        </div>

        <div class="sidebar-spacer" />
        <SidebarFooter />
      </template>
    </aside>

    <!-- Center panel -->
    <main class="center-panel">
      <button
        v-if="leftCollapsed && !isMobile"
        class="sidebar-expand-btn"
        @click="toggleLeft"
        :title="t('layout.toggleSidebar')"
      >
        <PanelLeft :size="17" />
      </button>
      <div class="center-content">
        <RouterView />
      </div>
    </main>

    <!-- Right Panel — knowledge / memory drawer, collapsed by default -->
    <aside class="panel-right" :class="{ collapsed: rightCollapsed }">
      <div class="panel-right-header">
        <div class="panel-tabs" v-if="!rightCollapsed">
          <button
            v-for="tab in tabs"
            :key="tab.id"
            class="panel-tab"
            :class="{ active: activeTab === tab.id }"
            @click="activeTab = tab.id"
          ><component :is="tab.icon" :size="13" /> {{ tab.label }}</button>
        </div>
        <button class="sidebar-toggle" @click="toggleRight" :title="t('layout.togglePanel')">
          <ChevronRight v-if="!rightCollapsed" :size="16" />
          <ChevronLeft v-else :size="16" />
        </button>
      </div>

      <template v-if="!rightCollapsed">
        <ContextMeter />
        <Transition name="route-fade" mode="out-in">
          <component
            :is="panelComponent"
            :key="activeTab"
            v-bind="panelProps"
          />
        </Transition>
      </template>
    </aside>

    <Transition name="fade">
      <div v-if="navHint" class="app-hint-toast" role="status">{{ navHint }}</div>
    </Transition>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, onBeforeUnmount, watch } from 'vue'
import { useT } from '@/i18n'
import { RouterView, useRouter, useRoute } from 'vue-router'
import { ChevronLeft, ChevronRight, Bot, Menu, PanelRight, PanelLeft, Brain, SquarePen, Wand2, LayoutGrid, Clock } from 'lucide-vue-next'
import { useProjectStore } from '@/stores/project'
import { useAgentStore } from '@/stores/agent'
import { useConversationStore } from '@/stores/conversation'
import { projectsApi } from '@/api/projects'
import ProjectTree from '@/components/sidebar/ProjectTree.vue'
import SidebarFooter from '@/components/sidebar/SidebarFooter.vue'
import ContextMeter from '@/components/knowledge/ContextMeter.vue'
import ConversationTreePanel from '@/components/conversations/ConversationTreePanel.vue'
import MemoryPanel from '@/components/memory/MemoryPanel.vue'
import { closeAllConnections } from '@/composables/useWebSocket'
import { invalidateLatestConversation } from '@/pages/ChatPage.vue'
import { useProjectData } from '@/composables/useProjectData'

useProjectData()

/* ── Sidebar state ────────────────────────────────────────────── */
const MOBILE_BREAKPOINT = 1024
const leftCollapsed = ref(false)
// ChatGPT has no persistent right rail — the knowledge/memory drawer starts
// closed and opens from the top-bar icons.
const rightCollapsed = ref(true)
const isMobile = ref(false)
const activeTab = ref<'memory'>('memory')
const navHint = ref('')
let navHintTimer: ReturnType<typeof setTimeout> | undefined

/** Show a short inline hint when a project-scoped entry is clicked without a project. */
function showNavHint(message: string) {
  navHint.value = message
  if (navHintTimer) clearTimeout(navHintTimer)
  navHintTimer = setTimeout(() => { navHint.value = '' }, 2600)
}

/** Sync isMobile flag and auto-collapse sidebars when entering mobile. */
function checkMobile() {
  const wasMobile = isMobile.value
  isMobile.value = window.innerWidth <= MOBILE_BREAKPOINT
  // When transitioning to mobile, collapse both sidebars so the center panel is visible.
  if (!wasMobile && isMobile.value) {
    leftCollapsed.value = true
    rightCollapsed.value = true
  }
}

/** Update --app-height CSS var from visualViewport for accurate mobile height (keyboard support). */
function updateAppHeight() {
  if (window.visualViewport) {
    document.documentElement.style.setProperty(
      '--app-height',
      `${window.visualViewport.height}px`,
    )
  }
}

/** Backdrop is visible on mobile when at least one sidebar is open. */
const showBackdrop = computed(() =>
  isMobile.value && (!leftCollapsed.value || !rightCollapsed.value),
)

/** Toggle left sidebar — on mobile, ensure mutual exclusion with right. */
function toggleLeft() {
  if (isMobile.value) {
    if (leftCollapsed.value) {
      leftCollapsed.value = false
      rightCollapsed.value = true
    } else {
      leftCollapsed.value = true
    }
  } else {
    leftCollapsed.value = !leftCollapsed.value
  }
}

/** Toggle right sidebar — on mobile, ensure mutual exclusion with left. */
function toggleRight() {
  if (isMobile.value) {
    if (rightCollapsed.value) {
      rightCollapsed.value = false
      leftCollapsed.value = true
    } else {
      rightCollapsed.value = true
    }
  } else {
    rightCollapsed.value = !rightCollapsed.value
  }
}

/** Open the right drawer on a specific tab (left nav memory entry). */
function openPanel(tab: 'memory') {
  if (!rightCollapsed.value && activeTab.value === tab) {
    rightCollapsed.value = true
    return
  }
  activeTab.value = tab
  rightCollapsed.value = false
  if (isMobile.value) leftCollapsed.value = true
}

/** Close both sidebars (used by backdrop tap and route change on mobile). */
function closeMobilePanels() {
  leftCollapsed.value = true
  rightCollapsed.value = true
}

onMounted(() => {
  checkMobile()
  window.addEventListener('resize', checkMobile)
  updateAppHeight()
  window.visualViewport?.addEventListener('resize', updateAppHeight)
})

onBeforeUnmount(() => {
  window.removeEventListener('resize', checkMobile)
  window.visualViewport?.removeEventListener('resize', updateAppHeight)
  if (navHintTimer) clearTimeout(navHintTimer)
})

/* ── Auto-close sidebars on navigation (mobile) ───────────────── */
const route = useRoute()
watch(() => route.fullPath, () => {
  if (isMobile.value) closeMobilePanels()
})

/* ── Panel tabs & content ─────────────────────────────────────── */
const { t } = useT()
const tabs = computed(() => [
  { id: 'memory' as const, label: t('layout.tabMemory'), icon: Brain },
])

const projectStore = useProjectStore()
const agentStore = useAgentStore()
const convStore = useConversationStore()
const router = useRouter()

/* ── 一级导航 ────────────────────────────────────────────────── */
// 智能体由对话框上方的 AgentStrip 选择、模型由 Composer 的 chip 选择，
// 原先常驻顶栏的两个选择器因此下线。

onMounted(() => { agentStore.fetchModelConfigs() })

function goDistill() {
  router.push('/distill')
}

// Project agents are scoped to the current project; refetch when it changes.
// Previously triggered by AgentSwitcher's mount — the ChatGPT layout retired
// that component, so the layout owns the fetch now (fetchAgents reconciles
// the current-agent selection internally).
watch(
  () => projectStore.currentProject?.project_id ?? null,
  (projectId) => { agentStore.fetchAgents(projectId) },
  { immediate: true },
)

/* ── Center top navigation ─────────────────────────────────────── */
// /projects/{pid}/(chat|workspace|publishes|schedules) page segment; non-project
// routes (/app, /settings) have no page nav. Knowledge and scripts were merged
// into the unified workspace tab, so their old segments also count as workspace
// (they redirect to it).
const pageSegment = computed(() => {
  const m = route.path.match(/^\/projects\/[^/]+\/(chat|workspace|knowledge|scripts|schedules)(?:\/|$)/)
  if (!m) return null
  if (m[1] === 'chat' || m[1] === 'schedules') return m[1]
  return 'workspace'
})

function goToPage(segment: 'chat' | 'workspace' | 'schedules') {
  const pid = route.params.projectId
  if (!pid) {
    showNavHint(t('layout.projectRequiredHint'))
    return
  }
  // No query forwarding: ?new=1 belongs to a specific entry flow
  // and must not leak into a tab switch (a stale ?new=1 would silently reset
  // the conversation). The mobile sidebar auto-close is handled by the
  // existing route.fullPath watcher below.
  router.push(`/projects/${pid}/${segment}`)
}

/* ── Route param ↔ project store sync ─────────────────────────── */
// ProjectTree pushes both together, but direct URL navigation / back-forward
// (same route record, component reused) changes only route.params.projectId.
// Without this, pages reading currentProject (ChatPage, WorkspacePage)
// keep showing the OLD project while the URL says otherwise, and
// WorkspacePage could even save edits into the wrong project.
const routePid = computed(() => route.params.projectId as string | undefined)
let pidSyncSeq = 0
watch(routePid, async (pid) => {
  if (!pid) return
  if (projectStore.currentProject?.project_id === pid) return
  const seq = ++pidSyncSeq
  let list = projectStore.projects
  if (list.length === 0) {
    try {
      list = await projectsApi.getProjects()
      projectStore.setProjects(list)
    } catch {
      return // auth/network errors surface via the route guard; leave as-is
    }
  }
  if (seq !== pidSyncSeq) return
  const match = list.find((p) => p.project_id === pid)
  if (match) projectStore.setCurrentProject(match)
}, { immediate: true })

const currentProjectId = computed(() => projectStore.currentProject?.project_id)

const panelComponent = computed(() => MemoryPanel)
const panelProps = computed(() => ({}))

function handleNewConversation() {
  closeAllConnections()
  convStore.reset()
  // Invalidate any in-flight latest-conversation load: its response would
  // otherwise resurrect the old conversation over the fresh empty state
  // . The ?new=1 marker stops the (possibly re-mounted) ChatPage
  // from auto-restoring the latest conversation after a non-chat-page click.
  invalidateLatestConversation()
  const id = projectStore.currentProject?.project_id
  if (!id) {
    showNavHint(t('layout.projectRequiredHint'))
    return
  }
  router.push(`/projects/${id}/chat?new=1`)
}
</script>
