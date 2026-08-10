<script setup lang="ts">
import { ref } from 'vue'
import { useRouter } from 'vue-router'
import { api, errorMessage } from '@/lib/api'
import { useAuthStore } from '@/stores/auth'

const auth = useAuthStore()
const router = useRouter()
const currentPassword = ref('')
const newPassword = ref('')
const confirmation = ref('')
const error = ref('')
const busy = ref(false)

async function submit(): Promise<void> {
  error.value = ''
  if (newPassword.value !== confirmation.value) {
    error.value = '两次输入的新密码不一致'
    return
  }
  busy.value = true
  try {
    await api.post('/auth/change-password', {
      current_password: currentPassword.value,
      new_password: newPassword.value,
    })
    await auth.logout()
    await router.push('/login')
  } catch (reason) {
    error.value = errorMessage(reason)
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <main class="login-page">
    <section class="login-brand">
      <div class="radar-visual"><span></span><i></i><b>SECURE</b></div>
      <div>
        <span class="overline">FIRST LOGIN POLICY</span>
        <h1>先建立你的<br />安全身份。</h1>
        <p>初始凭据只用于首次进入。修改完成后，当前刷新令牌族会立即撤销。</p>
      </div>
    </section>
    <section class="login-form-wrap">
      <form class="login-form" @submit.prevent="submit">
        <span class="eyebrow">PASSWORD ROTATION</span>
        <h2>修改初始密码</h2>
        <p>{{ auth.user?.display_name }}，新密码至少 12 位。</p>
        <label
          >当前密码<input v-model="currentPassword" type="password" autocomplete="current-password" required
        /></label>
        <label
          >新密码<input
            v-model="newPassword"
            type="password"
            autocomplete="new-password"
            minlength="12"
            required
        /></label>
        <label
          >确认新密码<input
            v-model="confirmation"
            type="password"
            autocomplete="new-password"
            minlength="12"
            required
        /></label>
        <p v-if="error" class="form-error">{{ error }}</p>
        <button class="primary-button" type="submit" :disabled="busy">
          {{ busy ? '提交中…' : '修改并重新登录' }}
        </button>
      </form>
    </section>
  </main>
</template>
