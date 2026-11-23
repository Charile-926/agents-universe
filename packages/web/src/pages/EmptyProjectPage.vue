<template>
  <div class="empty-project-page">
    <div class="empty-project-content">
      <div class="empty-project-logo">
        <span class="empty-project-logo-icon">
          <Bot :size="28" />
        </span>
        <h1 class="empty-project-greeting">{{ t('emptyProject.greeting') }}</h1>
        <p class="empty-project-tagline">{{ t('emptyProject.tagline') }}</p>
      </div>

      <div class="empty-project-cta">
        <p class="empty-project-hint">{{ t('emptyProject.hint') }}</p>
        <button class="btn-primary empty-project-btn" @click="showCreate = true">
          <Plus :size="16" />
          {{ t('emptyProject.createProject') }}
        </button>
      </div>
    </div>

    <Transition name="modal">
      <CreateProjectDialog v-if="showCreate" @close="showCreate = false" />
    </Transition>
  </div>
</template>

<script setup lang="ts">
import { ref } from 'vue'
import { useT } from '@/i18n'
import { Bot, Plus } from 'lucide-vue-next'
import CreateProjectDialog from '@/components/sidebar/CreateProjectDialog.vue'

const { t } = useT()
const showCreate = ref(false)
</script>
