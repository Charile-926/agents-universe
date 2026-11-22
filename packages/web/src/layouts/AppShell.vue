<template>
  <div class="app-shell">
    <header class="app-shell-header">
      <div class="app-brand">
        <Wand2 :size="18" />
        <span class="app-brand-name">Agents Universe</span>
        <span class="app-brand-sub">对话炼化</span>
      </div>
      <div class="app-user">
        <span class="app-user-name" :title="authStore.displayName || undefined">
          {{ authStore.displayName || t('common.user') }}
        </span>
        <button class="app-logout" type="button" @click="handleLogout">
          <LogOut :size="14" />
          <span>{{ t('shell.logout') }}</span>
        </button>
      </div>
    </header>

    <main class="app-main">
      <RouterView />
    </main>
  </div>
</template>

<script setup lang="ts">
import { RouterView } from 'vue-router'
import { LogOut, Wand2 } from 'lucide-vue-next'
import { useT } from '@/i18n'
import { useAuthStore } from '@/stores/auth'
import { apiFetch } from '@/api/client'
import { withBase } from '@/utils/basePath'

const { t } = useT()
const authStore = useAuthStore()

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
