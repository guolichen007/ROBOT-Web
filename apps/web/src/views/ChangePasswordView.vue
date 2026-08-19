<script setup lang="ts">
import { ref } from 'vue'
import { useRouter } from 'vue-router'
import { api, errorMessage } from '@/lib/api'
import { useAuthStore } from '@/stores/auth'
import brandLogo from '@/assets/yd/brand/youdao_brand_logo.png'
import shieldLock from '@/assets/yd/auth/shield_lock.svg'
import featurePatrol from '@/assets/yd/auth/feature_patrol.svg'
import featureData from '@/assets/yd/auth/feature_data.svg'
import featureSafety from '@/assets/yd/auth/feature_safety.svg'

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
  <main class="yd-auth yd-auth--password">
    <section class="yd-auth-left">
      <img class="yd-brand-logo" :src="brandLogo" alt="友道智造" />
      <div class="yd-hero">
        <h1>先建立你的<br />安全身份。</h1>
        <p class="yd-hero-sub">首 次 登 录 安 全 策 略</p>
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
    </section>

    <section class="yd-auth-right">
      <form class="yd-auth-card" @submit.prevent="submit">
        <div class="yd-auth-head">
          <div class="yd-auth-head-icon"><img :src="shieldLock" alt="" /></div>
          <div>
            <h2>修改初始密码</h2>
            <p>密码轮换后，当前刷新令牌族会立即撤销</p>
          </div>
        </div>
        <div class="yd-field">
          <label for="cp-current">当前密码</label>
          <div class="yd-input-shell">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <rect x="5" y="10" width="14" height="11" rx="2" /><path d="M8 10V7a4 4 0 0 1 8 0v3" />
            </svg>
            <input
              id="cp-current"
              v-model="currentPassword"
              type="password"
              autocomplete="current-password"
              placeholder="请输入当前密码"
              required
            />
          </div>
        </div>
        <div class="yd-field">
          <label for="cp-new">新密码</label>
          <div class="yd-input-shell">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <rect x="5" y="10" width="14" height="11" rx="2" /><path d="M8 10V7a4 4 0 0 1 8 0v3" />
            </svg>
            <input
              id="cp-new"
              v-model="newPassword"
              type="password"
              autocomplete="new-password"
              placeholder="至少 12 位"
              minlength="12"
              required
            />
          </div>
        </div>
        <div class="yd-field">
          <label for="cp-confirm">确认新密码</label>
          <div class="yd-input-shell">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <rect x="5" y="10" width="14" height="11" rx="2" /><path d="M8 10V7a4 4 0 0 1 8 0v3" />
            </svg>
            <input
              id="cp-confirm"
              v-model="confirmation"
              type="password"
              autocomplete="new-password"
              placeholder="再次输入新密码"
              minlength="12"
              required
            />
          </div>
        </div>
        <p v-if="error" class="yd-auth-error">{{ error }}</p>
        <button class="yd-login-btn" type="submit" :disabled="busy">
          {{ busy ? '提交中…' : '修改并重新登录' }}
        </button>
      </form>
      <div class="yd-security-strip">
        <img :src="shieldLock" alt="" />
        <div>
          <strong>安全策略</strong>
          <span>新密码至少 12 位；修改完成后需重新登录并进入审计</span>
        </div>
      </div>
    </section>
    <footer class="yd-auth-footer">
      上海友道智造自动化科技有限公司&nbsp; | &nbsp;技术支持：400-xxxx-xxx&nbsp; | &nbsp;推荐使用 Chrome 浏览器（版本 100+） 获得最佳体验<br />©
      2024 友道智造 保留所有权利
    </footer>
  </main>
</template>
