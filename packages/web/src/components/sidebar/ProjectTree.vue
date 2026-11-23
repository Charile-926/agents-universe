<template>
  <div class="project-tree">
    <div class="nav-heading">
      <span>{{ t('sidebar.projects.title') }}</span>
      <button class="add-project-btn" :title="t('sidebar.common.manageFavorites')" @click="showPicker = true">
        <Plus :size="14" />
      </button>
    </div>

    <div class="project-list">
      <div v-if="listedProjects.length === 0" class="empty-hint empty-state">
        <span class="empty-state-icon"><Folder :size="16" /></span>
        <span>{{ t('sidebar.projects.emptyHint') }}</span>
      </div>
      <div
        v-for="project in listedProjects"
        :key="project.project_id"
        class="project-item"
        :class="{ active: projectStore.currentProject?.project_id === project.project_id }"
        @click="selectProject(project)"
      >
        <Folder :size="14" class="project-icon" />
        <span class="project-name">{{ project.display_name }}</span>
        <span v-if="project.category_label" class="project-category-tag">{{ localizedText(project.category_label, project.category_i18n?.label) }}</span>
        <span
          class="badge project-visibility-badge"
          :class="project.visibility === 'public' ? 'badge--green' : 'badge--muted'"
          :title="project.visibility === 'public' ? t('sidebar.projects.badgePublic') : t('sidebar.projects.badgePrivate')"
        >{{ project.visibility === 'public' ? t('sidebar.projects.badgePublic') : t('sidebar.projects.badgePrivate') }}</span>
      </div>
    </div>

    <Transition name="modal">
      <ProjectPickerDialog v-if="showPicker" @close="showPicker = false" />
    </Transition>
  </div>
</template>

<script setup lang="ts">
import { ref, computed } from 'vue'
import { useT } from '@/i18n'
import { useRouter } from 'vue-router'
import { Plus, Folder } from 'lucide-vue-next'
import { useProjectStore } from '@/stores/project'
import { useFavoritesStore } from '@/stores/favorites'
import type { Project } from '@/types'
import { localizedText } from '@/utils/localize'
import ProjectPickerDialog from './ProjectPickerDialog.vue'

const { t } = useT()
const projectStore = useProjectStore()
const favoritesStore = useFavoritesStore()
const router = useRouter()
const showPicker = ref(false)

/** 公共项目默认直接出现在侧栏；只有项目列表尚未加载时才回退到收藏缓存。 */
const listedProjects = computed<Project[]>(() => {
  if (projectStore.projects.length > 0) return projectStore.projects
  return favoritesStore.resolvedFavoriteProjects
})

function selectProject(project: Project) {
  // No early-return on the current project — after a browser back/refresh the
  // user can be stranded on /app while the current project is already set;
  // clicking it must still navigate into its chat page (setCurrentProject is
  // idempotent).
  projectStore.setCurrentProject(project)
  router.push(`/projects/${project.project_id}/chat`)
}
</script>
