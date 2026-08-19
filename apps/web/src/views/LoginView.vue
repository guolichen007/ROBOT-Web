<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { errorMessage } from '@/lib/api'
import { useAuthStore } from '@/stores/auth'
import brandLogo from '@/assets/yd/brand/youdao_brand_logo.png'
import shieldLock from '@/assets/yd/auth/shield_lock.svg'
import featurePatrol from '@/assets/yd/auth/feature_patrol.svg'
import featureData from '@/assets/yd/auth/feature_data.svg'
import featureSafety from '@/assets/yd/auth/feature_safety.svg'

const REMEMBER_KEY = 'yd_remember_username'
const auth = useAuthStore()
const route = useRoute()
const router = useRouter()
const username = ref('')
const password = ref('')
const remember = ref(false)
const error = ref('')

onMounted(() => {
  const saved = localStorage.getItem(REMEMBER_KEY)
  if (saved) {
    username.value = saved
    remember.value = true
  }
})

async function submit(): Promise<void> {
  error.value = ''
  try {
    await auth.login(username.value, password.value)
    if (remember.value) localStorage.setItem(REMEMBER_KEY, username.value)
    else localStorage.removeItem(REMEMBER_KEY)
    await router.push(
      auth.user?.must_change_password ? '/change-password' : String(route.query.next || '/monitor'),
    )
  } catch (reason) {
    error.value = errorMessage(reason)
  }
}
</script>

<template>
  <main class="yd-auth">
    <section class="yd-auth-left">
      <img class="yd-brand-logo" :src="brandLogo" alt="友道智造" />
      <div class="yd-hero">
        <h1>智巡未来&nbsp;&nbsp;安全可控</h1>
        <p class="yd-hero-sub">友 道 智 造 巡 检 机 器 人 控 制 平 台</p>
        <div class="yd-hero-rule"></div>
        <div class="yd-features">
          <div class="yd-feature">
            <img :src="featurePatrol" alt="" />
            <div><strong>智能巡检</strong><span>自主导航 精准巡检</span></div>
          </div>
          <div class="yd-feature">
            <img :src="featureData" alt="" />
            <div><strong>数据驱动</strong><span>实时感知 智能分析</span></div>
          </div>
          <div class="yd-feature">
            <img :src="featureSafety" alt="" />
            <div><strong>安全可控</strong><span>多重防护 稳定可靠</span></div>
          </div>
        </div>
      </div>
      <div class="yd-auth-scene"></div>
    </section>

    <section class="yd-auth-right">
      <form class="yd-auth-card" @submit.prevent="submit">
        <div class="yd-auth-head">
          <div class="yd-auth-head-icon"><img :src="shieldLock" alt="" /></div>
          <div>
            <h2>欢迎登录</h2>
            <p>友道智造巡检机器人控制平台</p>
          </div>
        </div>
        <div class="yd-field">
          <label for="login-username">账号</label>
          <div class="yd-input-shell">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <circle cx="12" cy="8" r="4" /><path d="M4 21c0-4.3 3.6-7 8-7s8 2.7 8 7" />
            </svg>
            <input
              id="login-username"
              v-model="username"
              name="username"
              autocomplete="username"
              placeholder="请输入账号"
              required
            />
          </div>
        </div>
        <div class="yd-field">
          <label for="login-password">密码</label>
          <div class="yd-input-shell">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <rect x="5" y="10" width="14" height="11" rx="2" /><path d="M8 10V7a4 4 0 0 1 8 0v3" />
            </svg>
            <input
              id="login-password"
              v-model="password"
              name="password"
              type="password"
              autocomplete="current-password"
              placeholder="请输入密码"
              minlength="8"
              required
              autofocus
            />
          </div>
        </div>
        <div class="yd-login-options">
          <label class="yd-remember"
            ><input v-model="remember" type="checkbox" /><span>记住账号</span></label
          >
        </div>
        <p v-if="error" class="yd-auth-error">{{ error }}</p>
        <button class="yd-login-btn" type="submit" :disabled="auth.loading">
          {{ auth.loading ? '验证中…' : '登录' }}
        </button>
        <p class="yd-first-login">首次登录需修改密码，请妥善保管账号信息</p>
      </form>
      <div class="yd-security-strip">
        <img :src="shieldLock" alt="" />
        <div>
          <strong>安全访问</strong>
          <span>该系统仅限授权人员访问，所有操作将被记录与审计</span>
        </div>
        <img class="yd-security-watermark" :src="shieldLock" alt="" />
      </div>
    </section>
    <footer class="yd-auth-footer">
      上海友道智造自动化科技有限公司&nbsp; | &nbsp;技术支持：400-xxxx-xxx&nbsp; | &nbsp;推荐使用 Chrome 浏览器（版本 100+） 获得最佳体验<br />©
      2024 友道智造 保留所有权利
    </footer>
  </main>
</template>
