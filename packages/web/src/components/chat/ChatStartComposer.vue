<template>
  <div class="composer composer-start">
    <textarea
      v-model="text"
      class="composer-start-input"
      :placeholder="placeholderText"
      rows="1"
      @keydown.enter.exact.prevent="start"
    />
    <div class="composer-toolbar">
      <button class="composer-attach" type="button" :title="t('composer.attach')" disabled>
        <Plus :size="18" />
      </button>

      <div class="composer-project-wrap">
        <button class="composer-chip" type="button" :title="t('composer.projectSwitch')" @click="projectOpen = !projectOpen">
          <Folder :size="13" />
          <span class="composer-chip-text">{{ projectName }}</span>
          <ChevronDown :size="12" />
        </button>
        <div v-if="projectOpen" class="composer-menu">
          <button
            v-for="p in projects"
            :key="p.project_id"
            class="composer-menu-item"
            :class="{ active: p.project_id === currentProjectId }"
            @click="pickProject(p)"
          >{{ projectLabel(p) }}</button>
        </div>
      </div>

      <div class="composer-providers">
        <button class="composer-chip" type="button" :title="t('layout.selectModel')" @click="modelOpen = !modelOpen">
          <span class="composer-chip-text">{{ modelLabel }}</span>
          <ChevronDown :size="12" />
        </button>
        <div v-if="modelOpen" class="composer-menu composer-menu-right">
          <button
            v-for="opt in modelOptions"
            :key="opt.id"
            class="composer-menu-item"
            :class="{ active: agentStore.selectedConfigId === opt.id }"
            @click="pickModel(opt.id)"
          >{{ opt.label }}</button>
        </div>
      </div>

      <button
        class="submit-btn"
        type="button"
        :disabled="!text.trim()"
        :title="t('composer.sendTitle')"
        @click="start"
      >
        <ArrowUp :size="16" />
      </button>
    </div>
  </div>
</template>

<script setup lang="ts">
/**
 * 空会话状态下与正式 Composer 外观一致的起始输入框。
 *
 * 提交时把文本交给 ChatPage 暂存并创建会话，真正的 Composer 挂载后会
 * 消费这份草稿，因此首条消息的输入体验和后续对话完全一致。
 */
import { computed, ref } from 'vue'
import { useRouter } from 'vue-router'
import { useT } from '@/i18n'
import { Plus, Folder, ChevronDown, ArrowUp } from 'lucide-vue-next'
import { useAgentStore, AUTO_MODEL_CONFIG_ID } from '@/stores/agent'
import { useProjectStore } from '@/stores/project'
import { localizedText } from '@/utils/localize'
import type { Project } from '@/types'

const emit = defineEmits<{ start: [text: string] }>()

const { t } = useT()
const router = useRouter()
const agentStore = useAgentStore()
const projectStore = useProjectStore()

const text = ref('')
const projectOpen = ref(false)
const modelOpen = ref(false)

const placeholderText = computed(() =>
  localizedText(
    agentStore.currentAgent?.placeholder,
    agentStore.currentAgent?.i18n?.placeholder,
    t('composer.defaultPlaceholder'),
  ),
)

const projects = computed(() => projectStore.projects)
const currentProjectId = computed(() => projectStore.currentProject?.project_id ?? null)
const projectName = computed(() => {
  const p = projectStore.currentProject
  return p ? projectLabel(p) : t('layout.tabWorkspace')
})

function projectLabel(p: Project): string {
  return p.display_name || p.slug
}

const modelOptions = computed<Array<{ id: string; label: string; auto?: boolean }>>(() => [
  { id: AUTO_MODEL_CONFIG_ID, label: t('composer.autoModel'), auto: true },
  ...agentStore.modelConfigs.map((c): { id: string; label: string; auto?: boolean } => ({
    id: c.config_id,
    label: c.is_system ? `${c.model_id} Default` : c.model_id,
  })),
])

const modelLabel = computed(() => {
  const found = modelOptions.value.find((o) => o.id === agentStore.selectedConfigId)
  return found?.label ?? modelOptions.value[0]?.label ?? ''
})

function pickModel(id: string) {
  agentStore.setSelectedConfigId(id)
  modelOpen.value = false
}

function pickProject(p: Project) {
  projectOpen.value = false
  if (p.project_id === currentProjectId.value) return
  projectStore.setCurrentProject(p)
  router.push(`/projects/${p.project_id}/chat`)
}

function start() {
  const value = text.value.trim()
  if (!value) return
  emit('start', value)
  text.value = ''
}
</script>
