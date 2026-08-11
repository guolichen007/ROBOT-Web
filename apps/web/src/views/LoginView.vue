<script setup lang="ts">
import { ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { errorMessage } from '@/lib/api'
import { useAuthStore } from '@/stores/auth'

const auth = useAuthStore()
const route = useRoute()
const router = useRouter()
const username = ref('admin')
const password = ref('')
const error = ref('')

async function submit(): Promise<void> {
  error.value = ''
  try {
    await auth.login(username.value, password.value)
    await router.push(
      auth.user?.must_change_password ? '/change-password' : String(route.query.next || '/monitor'),
    )
  } catch (reason) {
    error.value = errorMessage(reason)
  }
}
</script>

<template>
  <main class="login-page">
    <section class="login-brand">
      <div class="radar-visual"><span></span><i></i><b>R001</b></div>
      <div>
        <span class="overline">FIRE RESPONSE / CLOUD CONTROL</span>
        <h1>在风险抵达之前<br />看见每一次变化。</h1>
        <p>智能灭火机器人云控平台，连接任务、地图、告警与控制安全闭环。</p>
      </div>
      <footer><span>INTEGRATION-READY FINAL</span><span>SCHEMA 1.2</span></footer>
    </section>
    <section class="login-form-wrap">
      <form class="login-form" @submit.prevent="submit">
        <span class="eyebrow">AUTHORIZED ACCESS</span>
        <h2>登录控制台</h2>
        <p>使用平台账户继续。首次登录后必须修改初始密码。</p>
        <label>账号<input v-model="username" name="username" autocomplete="username" required /></label>
        <label
          >密码<input
            v-model="password"
            name="password"
            type="password"
            autocomplete="current-password"
            minlength="8"
            required
            autofocus
        /></label>
        <p v-if="error" class="form-error">{{ error }}</p>
        <button class="primary-button" type="submit" :disabled="auth.loading">
          {{ auth.loading ? '验证中…' : '进入平台' }}
        </button>
        <small class="security-copy">登录、令牌轮换和高风险操作均进入安全审计。</small>
      </form>
    </section>
  </main>
</template>
