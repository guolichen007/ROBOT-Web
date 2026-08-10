import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import { api, clearAccessToken, refreshAccessToken, setAccessToken } from '@/lib/api'
import type { UserProfile } from '@/types'

export const useAuthStore = defineStore('auth', () => {
  const user = ref<UserProfile | null>(null)
  const ready = ref(false)
  const loading = ref(false)
  const authenticated = computed(() => Boolean(user.value))

  async function login(username: string, password: string): Promise<void> {
    loading.value = true
    try {
      const { data } = await api.post('/auth/login', { username, password })
      setAccessToken(data.access_token)
      user.value = data.user
    } finally {
      loading.value = false
      ready.value = true
    }
  }

  async function restore(): Promise<void> {
    if (ready.value) return
    try {
      await refreshAccessToken()
      user.value = (await api.get('/auth/me')).data
    } catch {
      clearAccessToken()
      user.value = null
    } finally {
      ready.value = true
    }
  }

  async function logout(): Promise<void> {
    try {
      await api.post('/auth/logout')
    } finally {
      clearAccessToken()
      user.value = null
    }
  }

  function can(permission?: string): boolean {
    return !permission || Boolean(user.value?.permissions.includes(permission))
  }

  return { user, ready, loading, authenticated, login, restore, logout, can }
})
