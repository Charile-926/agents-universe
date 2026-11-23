<template>
  <div class="agent-strip" role="tablist" :aria-label="t('layout.selectAgent')">
    <button
      v-for="agent in agents"
      :key="agent.slug"
      type="button"
      role="tab"
      class="agent-chip"
      :class="{ active: agent.slug === currentSlug }"
      :aria-selected="agent.slug === currentSlug"
      :title="describe(agent)"
      @click="select(agent)"
    >
      <span class="agent-chip-avatar">{{ initials(agent) }}</span>
      <span class="agent-chip-name">{{ nameOf(agent) }}</span>
    </button>
  </div>
</template>

<script setup lang="ts">
/**
 * 对话框上方的智能体选择条。
 *
 * 十个内置智能体横排展示，点击即切换当前智能体；切换后由 ChatPage 的
 * agentSlug watch 负责重置会话并载入该智能体的最近会话，这里只管选择。
 */
import { computed } from 'vue'
import { useT } from '@/i18n'
import { useAgentStore } from '@/stores/agent'
import { localizedText } from '@/utils/localize'
import type { AgentInfo } from '@/types'

const agentStore = useAgentStore()
const { t } = useT()

const agents = computed(() => agentStore.agents)
const currentSlug = computed(() => agentStore.currentAgent?.slug ?? null)

function nameOf(agent: AgentInfo): string {
  return localizedText(agent.label, agent.i18n?.display_name) || agent.slug
}

function describe(agent: AgentInfo): string {
  return localizedText(agent.description, agent.i18n?.description) || nameOf(agent)
}

/** 中文取首字，英文取两个词的首字母。 */
function initials(agent: AgentInfo): string {
  const name = nameOf(agent).trim()
  if (!name) return 'AI'
  if (/[\u4e00-\u9fff]/.test(name)) return name.slice(0, 1)
  const words = name.split(/\s+/)
  return (words[0][0] + (words[1]?.[0] ?? '')).toUpperCase()
}

function select(agent: AgentInfo) {
  if (agent.slug === currentSlug.value) return
  agentStore.setCurrentAgent(agent)
}
</script>
