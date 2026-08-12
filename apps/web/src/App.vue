<script setup lang="ts">
import { computed, onMounted, onUnmounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import {
  AlarmIcon,
  ControlPlatformIcon,
  DashboardIcon,
  HistoryIcon,
  MapIcon,
  NotificationIcon,
  SettingIcon,
  TaskIcon,
  UserIcon,
  VehicleIcon,
} from 'tdesign-icons-vue-next'
import { useAuthStore } from '@/stores/auth'
import { useMonitorStore } from '@/stores/monitor'

const auth = useAuthStore()
const monitor = useMonitorStore()
const route = useRoute()
const router = useRouter()

const nav = [
  { group: '运行', path: '/monitor', label: '实时监控', icon: DashboardIcon, permission: 'robot.read' },
  { group: '运行', path: '/tasks', label: '任务调度', icon: TaskIcon, permission: 'robot.read' },
  { group: '运行', path: '/alarms', label: '火情报警', icon: AlarmIcon, permission: 'alarm.read' },
  { group: '运行', path: '/history', label: '历史回放', icon: HistoryIcon, permission: 'robot.read' },
  { group: '配置', path: '/patrol', label: '巡检计划', icon: ControlPlatformIcon, permission: 'map.read' },
  { group: '配置', path: '/maps', label: '地图与版本', icon: MapIcon, permission: 'map.read' },
  { group: '配置', path: '/parking', label: '点位与预设', icon: NotificationIcon, permission: 'map.read' },
  { group: '配置', path: '/robots', label: '车辆配置', icon: VehicleIcon, permission: 'robot.read' },
  { group: '系统', path: '/users', label: '用户权限', icon: UserIcon, permission: 'user.manage' },
  { group: '系统', path: '/audit', label: '审计日志', icon: NotificationIcon, permission: 'audit.read' },
  { group: '系统', path: '/settings', label: '系统状态', icon: SettingIcon, permission: 'settings.manage' },
]
const visibleNav = computed(() => nav.filter((item) => auth.can(item.permission)))
const groups = computed(() => [...new Set(visibleNav.value.map((item) => item.group))])
const isLogin = computed(() => ['/login', '/change-password'].includes(route.path))
const compact = computed(() => route.path === '/monitor')
const robot = computed(() => monitor.robot)
const clock = computed(() =>
  new Intl.DateTimeFormat('zh-CN', { dateStyle: 'short', timeStyle: 'medium', hour12: false }).format(
    new Date(),
  ),
)

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
  <div v-else class="app-shell" :class="{ 'monitor-shell': compact }">
    <aside class="sidebar" :class="{ compact }">
      <div class="brand">
        <span class="brand-symbol" aria-hidden="true">灭</span>
        <span class="brand-copy"><strong>智能灭火机器人</strong><small>云端监控与调度平台</small></span>
      </div>
      <nav aria-label="主导航">
        <section v-for="group in groups" :key="group" class="nav-group">
          <small>{{ group }}</small>
          <RouterLink
            v-for="item in visibleNav.filter((entry) => entry.group === group)"
            :key="item.path"
            :to="item.path"
            :title="item.label"
          >
            <component :is="item.icon" class="nav-icon" />
            <span>{{ item.label }}</span>
          </RouterLink>
        </section>
      </nav>
      <div class="sidebar-foot"><span>DEV</span><span>协议 1.2</span></div>
    </aside>
    <main class="workspace">
      <header class="topbar">
        <div class="system-strip">
          <strong>智能灭火机器人中控</strong>
          <span class="status-item"
            ><i :class="monitor.connected ? 'ok' : 'warn'"></i
            >{{ monitor.connected ? '链路正常' : '正在重连' }}</span
          >
          <span class="status-item"
            ><i :class="robot?.online_state === 'ONLINE' ? 'ok' : 'warn'"></i>R001
            {{ robot?.online_state || 'OFFLINE' }}</span
          >
          <span>电池 {{ robot?.battery == null ? '--' : `${robot.battery.toFixed(0)}%` }}</span>
          <span>任务 {{ monitor.activeTask?.type || '空闲' }}</span>
        </div>
        <div class="user-strip">
          <time>{{ clock }}</time
          ><span>{{ auth.user?.display_name }}</span
          ><button @click="logout">退出</button>
        </div>
      </header>
      <section class="page"><RouterView /></section>
    </main>
  </div>
</template>
