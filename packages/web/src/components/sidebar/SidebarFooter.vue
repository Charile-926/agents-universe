<template>
  <div class="sidebar-footer">
    <div class="user-info">
      <div class="user-info-main">
        <span class="user-avatar">{{ initials }}</span>
        <span class="user-name" :title="authStore.displayName || undefined">{{
          authStore.displayName || t('common.user')
        }}</span>
      </div>
      <div class="user-info-actions">
        <button class="settings-btn" :title="t('sidebar.footer.takeTour')" @click="startTour">
          <Compass :size="12" />
        </button>
        <button class="settings-btn" :title="t('sidebar.footer.tokenSettings')" @click="goSettings">
          <Settings :size="12" />
        </button>
        <button class="logout-btn" :title="t('sidebar.footer.logout')" @click="handleLogout">
          <LogOut :size="12" />
        </button>
      </div>
    </div>
    <span v-if="authStore.activeUsersCount > 1" class="active-users-hint">
      {{ t('sidebar.footer.activeUsers', { count: authStore.activeUsersCount }) }}
    </span>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { useRouter } from 'vue-router'
import { useT } from '@/i18n'
import { Settings, LogOut, Compass } from 'lucide-vue-next'
import { useAuthStore } from '@/stores/auth'
import { useTourStore } from '@/stores/tour'
import { withBase } from '@/utils/basePath'
import { apiFetch } from '@/api/client'

const authStore = useAuthStore()
const tourStore = useTourStore()
const router = useRouter()
const { t } = useT()

const initials = computed(() => {
  const name = authStore.displayName || ''
  if (!name) return '?'
  return name.slice(0, 2).toUpperCase()
})

function goSettings() {
  router.push('/settings/tokens')
}

function startTour() {
  void tourStore.start(0, router)
}

async function handleLogout() {
  try {
    await apiFetch('/api/auth/logout', { method: 'POST' })
  } catch {
    // 服务端失败也要清掉本地会话
  }
  authStore.logout()
  window.location.href = withBase('/login')
}
</script>
