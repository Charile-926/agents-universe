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
          :disabled="!projectStore.currentProject"
          @click="handleNewConversation"
        >
          <SquarePen :size="16" />
          <span>{{ t('layout.newChat') }}</span>
        </button>

        <ProjectTree />

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
      <!-- ChatGPT-style minimal top bar: agent + model pickers on the left,
           page tabs in the middle, panel actions on the right. -->
      <header class="main-topbar">
        <div class="topbar-left">
          <button
            v-if="leftCollapsed && !isMobile"
            class="topbar-icon-btn"
            @click="toggleLeft"
            :title="t('layout.toggleSidebar')"
          >
            <PanelLeft :size="18" />
          </button>
          <button
            class="agent-picker-btn"
            :title="t('layout.selectAgent')"
            @click="showAgentPicker = true"
          >
            <span class="agent-picker-name">{{ currentAgentName || t('layout.selectAgent') }}</span>
            <ChevronDown :size="14" />
          </button>
          <div class="model-picker">
            <button
              class="model-picker-btn"
              :title="t('layout.selectModel')"
              @click="modelMenuOpen = !modelMenuOpen"
            >
              <Sparkles v-if="selectedModelIsAuto" :size="12" />
              <span>{{ selectedModelLabel }}</span>
              <ChevronDown :size="12" />
            </button>
            <div v-if="modelMenuOpen" class="model-menu-backdrop" @click="modelMenuOpen = false" />
            <div v-if="modelMenuOpen" class="model-menu">
              <button
                v-for="opt in modelOptions"
                :key="opt.id"
                class="model-menu-item"
                :class="{ active: agentStore.selectedConfigId === opt.id }"
                @click="selectModel(opt.id)"
              >
                <Sparkles v-if="opt.auto" :size="13" />
                <span>{{ opt.label }}</span>
                <Check v-if="agentStore.selectedConfigId === opt.id" :size="13" class="model-menu-check" />
              </button>
            </div>
          </div>
        </div>

        <!-- Page-level navigation between chat / workspace / publishes /
             schedules — hidden on non-project routes (/app, /settings). -->
        <nav v-if="pageSegment" class="center-topnav">
          <button
            v-for="nav in navTabs"
            :key="nav.id"
            class="center-tab"
            :class="{ active: pageSegment === nav.id }"
            :data-tour-target="`center-nav-${nav.id}`"
            @click="goToPage(nav.id)"
          >{{ nav.label }}</button>
        </nav>

        <div class="topbar-right">
          <span
            v-if="pageSegment === 'chat' && convStore.conversationId && convStore.tokensUsed > 0"
            class="nav-tokens"
          >{{ t('layout.sessionTokens', { count: convStore.tokensUsed.toLocaleString() }) }}</span>
          <button
            v-if="pageSegment === 'chat' && convStore.messages.length > 0"
            class="topbar-icon-btn nav-compress"
            :disabled="isCompressDisabled"
            :title="compressing ? t('chatPanel.compressing') : t('chatPanel.compressContext')"
            @click="handleCompress"
          >
            <Shrink :size="16" />
          </button>
          <button
            class="topbar-icon-btn"
            :class="{ active: !rightCollapsed && activeTab === 'knowledge' }"
            :title="t('layout.openKnowledge')"
            data-tour-target="panel-tab-knowledge"
            @click="openPanel('knowledge')"
          >
            <BookOpen :size="17" />
          </button>
          <button
            class="topbar-icon-btn"
            :class="{ active: !rightCollapsed && activeTab === 'memory' }"
            :title="t('layout.openMemory')"
            data-tour-target="panel-tab-memory"
            @click="openPanel('memory')"
          >
            <Brain :size="17" />
          </button>
          <button
            class="topbar-icon-btn"
            :class="{ active: !rightCollapsed && activeTab === 'distill' }"
            :title="t('skillDistill.title')"
            @click="openPanel('distill')"
          >
            <Wand2 :size="17" />
          </button>
          <button
            class="topbar-icon-btn"
            :title="t('layout.togglePanel')"
            @click="toggleRight"
          >
            <PanelRight :size="17" />
          </button>
        </div>
      </header>

      <div class="center-content">
        <Transition name="route-fade" mode="out-in">
          <RouterView />
        </Transition>
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

    <!-- Agent picker (reuses the favorites/selection dialog) -->
    <Transition name="modal">
      <AgentPickerDialog v-if="showAgentPicker" @close="showAgentPicker = false" />
    </Transition>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, onBeforeUnmount, watch } from 'vue'
import { useT } from '@/i18n'
import { RouterView, useRouter, useRoute } from 'vue-router'
import { ChevronLeft, ChevronRight, ChevronDown, Bot, Menu, PanelRight, PanelLeft, BookOpen, Brain, Shrink, SquarePen, Sparkles, Check, Wand2 } from 'lucide-vue-next'
import { useProjectStore } from '@/stores/project'
import { useAgentStore, AUTO_MODEL_CONFIG_ID } from '@/stores/agent'
import { useConversationStore } from '@/stores/conversation'
import { conversationsApi } from '@/api/conversations'
import { projectsApi } from '@/api/projects'
import { localizedText } from '@/utils/localize'
import ProjectTree from '@/components/sidebar/ProjectTree.vue'
import SidebarFooter from '@/components/sidebar/SidebarFooter.vue'
import AgentPickerDialog from '@/components/sidebar/AgentPickerDialog.vue'
import ContextMeter from '@/components/knowledge/ContextMeter.vue'
import ConversationTreePanel from '@/components/conversations/ConversationTreePanel.vue'
import KnowledgePanel from '@/components/knowledge/KnowledgePanel.vue'
import MemoryPanel from '@/components/memory/MemoryPanel.vue'
import SkillDistillPanel from '@/components/skills/SkillDistillPanel.vue'
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
const activeTab = ref<'knowledge' | 'memory' | 'distill'>('knowledge')

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

/** Open the right drawer on a specific tab (top-bar knowledge/memory icons). */
function openPanel(tab: 'knowledge' | 'memory' | 'distill') {
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
})

/* ── Auto-close sidebars on navigation (mobile) ───────────────── */
const route = useRoute()
watch(() => route.fullPath, () => {
  if (isMobile.value) closeMobilePanels()
})

/* ── Panel tabs & content ─────────────────────────────────────── */
const { t } = useT()
const tabs = computed(() => [
  { id: 'knowledge' as const, label: t('layout.tabKnowledge'), icon: BookOpen },
  { id: 'memory' as const, label: t('layout.tabMemory'), icon: Brain },
  { id: 'distill' as const, label: t('skillDistill.title'), icon: Wand2 },
])

const projectStore = useProjectStore()
const agentStore = useAgentStore()
const convStore = useConversationStore()
const router = useRouter()

/* ── Top-bar agent picker ─────────────────────────────────────── */
const showAgentPicker = ref(false)
const currentAgentName = computed(() => {
  const a = agentStore.currentAgent
  return a ? localizedText(a.label, a.i18n?.display_name) : ''
})

/* ── Top-bar model picker ─────────────────────────────────────── */
const modelMenuOpen = ref(false)
const modelOptions = computed<Array<{ id: string; label: string; auto?: boolean }>>(() => [
  { id: AUTO_MODEL_CONFIG_ID, label: t('composer.autoModel'), auto: true },
  ...agentStore.modelConfigs.map((c): { id: string; label: string; auto?: boolean } => ({
    id: c.config_id,
    label: c.is_system ? `${c.model_id} Default` : c.model_id,
  })),
])
const selectedModelIsAuto = computed(
  () => !agentStore.selectedConfigId || agentStore.selectedConfigId === AUTO_MODEL_CONFIG_ID,
)
const selectedModelLabel = computed(() => {
  const found = modelOptions.value.find((o) => o.id === agentStore.selectedConfigId)
  return found?.label ?? modelOptions.value[0]?.label ?? ''
})
function selectModel(id: string) {
  agentStore.setSelectedConfigId(id)
  modelMenuOpen.value = false
}
onMounted(() => { agentStore.fetchModelConfigs() })

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
  const m = route.path.match(/^\/projects\/[^/]+\/(chat|workspace|knowledge|scripts|publishes|schedules)(?:\/|$)/)
  if (!m) return null
  if (m[1] === 'chat' || m[1] === 'publishes' || m[1] === 'schedules') return m[1]
  return 'workspace'
})

const navTabs = computed(() => [
  { id: 'chat' as const, label: t('layout.tabConversations') },
  { id: 'workspace' as const, label: t('layout.tabWorkspace') },
  { id: 'publishes' as const, label: t('layout.tabPublishes') },
  { id: 'schedules' as const, label: t('layout.tabSchedules') },
])

// Compress targets the active conversation; lives in the top bar's right group.
const compressing = ref(false)
const isCompressDisabled = computed(
  () => convStore.isStreaming || convStore.isThinking || compressing.value,
)

async function handleCompress() {
  const id = convStore.conversationId
  if (!id || compressing.value) return
  if (!window.confirm(t('chatPanel.compressConfirm'))) return
  compressing.value = true
  try {
    const res = await conversationsApi.compress(id)
    convStore.loadHistory(res.messages, id)
  } catch (e) {
    window.alert(e instanceof Error ? e.message : t('chatPanel.compressFailed'))
  } finally {
    compressing.value = false
  }
}

function goToPage(segment: 'chat' | 'workspace' | 'publishes' | 'schedules') {
  const pid = route.params.projectId
  if (!pid) return
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

const panelComponent = computed(() => {
  if (activeTab.value === 'memory') return MemoryPanel
  if (activeTab.value === 'distill') return SkillDistillPanel
  return KnowledgePanel
})

const panelProps = computed(() => {
  if (activeTab.value === 'memory' || activeTab.value === 'distill') return {}
  return { projectId: currentProjectId.value }
})

function handleNewConversation() {
  closeAllConnections()
  convStore.reset()
  // Invalidate any in-flight latest-conversation load: its response would
  // otherwise resurrect the old conversation over the fresh empty state
  // . The ?new=1 marker stops the (possibly re-mounted) ChatPage
  // from auto-restoring the latest conversation after a non-chat-page click.
  invalidateLatestConversation()
  const id = projectStore.currentProject?.project_id
  if (id) router.push(`/projects/${id}/chat?new=1`)
}
</script>