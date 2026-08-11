<script setup lang="ts">
import { computed, onMounted, onUnmounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useAuthStore } from '@/stores/auth'
import { useMonitorStore } from '@/stores/monitor'

const auth = useAuthStore()
const monitor = useMonitorStore()
const route = useRoute()
const router = useRouter()

const nav = [
  { path: '/monitor', label: '态势监控', mark: '⌁', permission: 'robot.read' },
  { path: '/robots', label: '机器人', mark: 'R', permission: 'robot.read' },
  { path: '/maps', label: '地图版本', mark: '◇', permission: 'map.read' },
  { path: '/parking', label: '车位点位', mark: 'P', permission: 'map.read' },
  { path: '/tasks', label: '任务调度', mark: 'T', permission: 'robot.read' },
  { path: '/alarms', label: '火情报警', mark: '!', permission: 'alarm.read' },
  { path: '/history', label: '历史回放', mark: '↺', permission: 'robot.read' },
  { path: '/users', label: '用户权限', mark: 'U', permission: 'user.manage' },
  { path: '/audit', label: '审计日志', mark: 'A', permission: 'audit.read' },
  { path: '/settings', label: '系统状态', mark: 'S', permission: 'settings.manage' },
]
const visibleNav = computed(() => nav.filter((item) => auth.can(item.permission)))
const isLogin = computed(() => ['/login', '/change-password'].includes(route.path))
const robot = computed(() => monitor.robot)

onMounted(() => {
  if (auth.authenticated && route.path !== '/login') void monitor.start()
})
onUnmounted(() => monitor.disconnect())

async function logout(): Promise<void> {
  monitor.disconnect()
  await auth.logout()
  await router.push('/login')
}
</script>

<template>
  <RouterView v-if="isLogin" />
  <div v-else class="app-shell">
    <aside class="sidebar">
      <div class="brand">
        <span class="brand-symbol"><i></i><i></i><i></i></span>
        <span><strong>FIREBOT</strong><small>云控平台 · V2 BASELINE</small></span>
      </div>
      <nav aria-label="主导航">
        <RouterLink v-for="item in visibleNav" :key="item.path" :to="item.path">
          <span class="nav-mark">{{ item.mark }}</span
          ><span>{{ item.label }}</span>
        </RouterLink>
      </nav>
      <div class="sidebar-foot">
        <span class="profile-chip">DEV</span>
        <span>协议 1.2</span>
      </div>
    </aside>
    <main class="workspace">
      <header class="topbar">
        <div class="system-strip">
          <span class="live-dot" :class="monitor.connected ? 'ok' : 'warn'"></span>
          <span>{{ monitor.connected ? '实时链路已连接' : '实时链路重连中' }}</span>
          <span class="divider"></span>
          <span>R001</span>
          <strong :class="`state-${(robot?.online_state || 'offline').toLowerCase()}`">{{
            robot?.online_state || 'OFFLINE'
          }}</strong>
          <span>{{ robot?.battery?.toFixed?.(0) ?? '--' }}%</span>
        </div>
        <div class="user-strip">
          <span>{{ auth.user?.display_name }}</span>
          <button class="text-button" @click="logout">退出</button>
        </div>
      </header>
      <section class="page"><RouterView /></section>
    </main>
  </div>
</template>
