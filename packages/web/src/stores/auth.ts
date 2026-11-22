import { defineStore } from 'pinia'
import { ref } from 'vue'
import { apiFetch } from '@/api/client'

export const useAuthStore = defineStore('auth', () => {
  const userId = ref<string | null>(null)
  const displayName = ref<string | null>(null)
  const isAuthenticated = ref(false)
  const activeUsersCount = ref(0)

  function setUser(user: { userId: string; displayName: string }) {
    userId.value = user.userId
    displayName.value = user.displayName
    isAuthenticated.value = true
  }

  function setActiveUsersCount(count: number) {
    activeUsersCount.value = count
  }

  function logout() {
    userId.value = null
    displayName.value = null
    isAuthenticated.value = false
  }

  async function login(username: string, password: string) {
    const data = await apiFetch<{ user_id: string; display_name: string }>(
      '/api/auth/login',
      { method: 'POST', body: JSON.stringify({ username, password }) },
    )
    setUser({ userId: data.user_id, displayName: data.display_name })
  }

  async function register(username: string, password: string, displayName?: string) {
    const data = await apiFetch<{ user_id: string; display_name: string }>(
      '/api/auth/register',
      { method: 'POST', body: JSON.stringify({ username, password, display_name: displayName }) },
    )
    setUser({ userId: data.user_id, displayName: data.display_name })
  }

  return { userId, displayName, isAuthenticated, activeUsersCount, setUser, setActiveUsersCount, logout, login, register }
})
