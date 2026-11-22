<template>
  <div class="login-page">
    <div class="login-card">
      <h1 class="login-title">{{ t('auth.title') }}</h1>
      <p class="login-sub">{{ mode === 'login' ? t('auth.loginSub') : t('auth.registerSub') }}</p>
      <form @submit.prevent="handleSubmit">
        <label class="login-field">
          <span>{{ t('auth.username') }}</span>
          <input
            v-model="username"
            type="text"
            autocomplete="username"
            :placeholder="t('auth.usernamePlaceholder')"
            required
          />
        </label>
        <label v-if="mode === 'register'" class="login-field">
          <span>{{ t('auth.displayName') }}</span>
          <input v-model="displayName" type="text" :placeholder="t('auth.displayNamePlaceholder')" />
        </label>
        <label class="login-field">
          <span>{{ t('auth.password') }}</span>
          <input
            v-model="password"
            type="password"
            :autocomplete="mode === 'login' ? 'current-password' : 'new-password'"
            :placeholder="t('auth.passwordPlaceholder')"
            required
          />
        </label>
        <p v-if="error" class="login-error">{{ error }}</p>
        <button class="login-submit" type="submit" :disabled="busy">
          {{ busy ? t('auth.submitting') : (mode === 'login' ? t('auth.submitLogin') : t('auth.submitRegister')) }}
        </button>
      </form>
      <button class="login-switch" type="button" @click="toggleMode">
        {{ mode === 'login' ? t('auth.switchToRegister') : t('auth.switchToLogin') }}
      </button>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref } from 'vue'
import { useT } from '@/i18n'
import { useAuthStore } from '@/stores/auth'
import { withBase } from '@/utils/basePath'
import { translateApiError } from '@/utils/apiError'

const { t } = useT()
const authStore = useAuthStore()
const mode = ref<'login' | 'register'>('login')
const username = ref('')
const password = ref('')
const displayName = ref('')
const error = ref('')
const busy = ref(false)

function toggleMode() {
  mode.value = mode.value === 'login' ? 'register' : 'login'
  error.value = ''
}

async function handleSubmit() {
  error.value = ''
  busy.value = true
  try {
    if (mode.value === 'login') {
      await authStore.login(username.value.trim(), password.value)
    } else {
      await authStore.register(username.value.trim(), password.value, displayName.value.trim() || undefined)
    }
    // Full reload so App.vue re-fetches /api/me with the new session cookie.
    window.location.href = withBase('/app')
  } catch (e) {
    error.value = translateApiError(e)
  } finally {
    busy.value = false
  }
}
</script>

<style scoped>
.login-page {
  min-height: 100vh;
  display: flex;
  align-items: center;
  justify-content: center;
  background: #fff;
  padding: 24px;
}
.login-card { width: 100%; max-width: 380px; }
.login-title { font-size: 24px; font-weight: 600; color: #0d0d0d; margin: 0 0 6px; text-align: center; }
.login-sub { font-size: 14px; color: #6b6b6b; margin: 0 0 24px; text-align: center; }
.login-field { display: block; margin-bottom: 16px; }
.login-field span { display: block; font-size: 13px; color: #333; margin-bottom: 6px; }
.login-field input {
  width: 100%;
  box-sizing: border-box;
  padding: 10px 12px;
  border: 1px solid #d9d9d9;
  border-radius: 10px;
  font-size: 14px;
  outline: none;
}
.login-field input:focus { border-color: #0d0d0d; }
.login-error { color: #d92d20; font-size: 13px; margin: 0 0 12px; }
.login-submit {
  width: 100%;
  padding: 11px;
  background: #0d0d0d;
  color: #fff;
  border: none;
  border-radius: 10px;
  font-size: 14px;
  cursor: pointer;
}
.login-submit:disabled { opacity: .6; cursor: default; }
.login-switch {
  display: block;
  margin: 16px auto 0;
  background: none;
  border: none;
  color: #6b6b6b;
  font-size: 13px;
  cursor: pointer;
  text-decoration: underline;
}
</style>