<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch, type Component } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { Dropdown as TDropdown } from 'tdesign-vue-next'
import {
  AlarmIcon,
  BatteryIcon,
  ChevronDownIcon,
  DashboardIcon,
  HistoryIcon,
  LinkIcon,
  LocationIcon,
  NotificationIcon,
  RobotIcon,
  SettingIcon,
  TaskIcon,
  VehicleIcon,
} from 'tdesign-icons-vue-next'
import { useAuthStore } from '@/stores/auth'
import { useMonitorStore } from '@/stores/monitor'
import { useSystemClock } from '@/composables/useSystemClock'
import { localizationLabel, taskTypeLabel } from '@/lib/ui-labels'
import brandLogo from '@/assets/yd/brand/youdao_brand_logo.png'
import techWave from '@/assets/yd/decorative/tech_wave.svg'

interface NavItem {
  label: string
  path?: string
  icon: Component
  permission?: string
  children?: Array<{ label: string; path: string; permission: string }>
}

const auth = useAuthStore()
const monitor = useMonitorStore()
const route = useRoute()
const router = useRouter()
const { now } = useSystemClock()

const nav: NavItem[] = [
  { label: '总览监控', path: '/monitor', icon: DashboardIcon, permission: 'robot.read' },
  {
    label: '任务管理',
    icon: TaskIcon,
    permission: 'robot.read',
    children: [
      { label: '任务调度', path: '/tasks', permission: 'robot.read' },
      { label: '巡检计划', path: '/patrol', permission: 'map.read' },
    ],
  },
  { label: '数据分析', path: '/history', icon: HistoryIcon, permission: 'robot.read' },
  {
    label: '设备管理',
    icon: VehicleIcon,
    permission: 'robot.read',
    children: [
      { label: '车辆配置', path: '/robots', permission: 'robot.read' },
      { label: '地图版本', path: '/maps', permission: 'map.read' },
      { label: '地图与点位', path: '/parking', permission: 'map.read' },
    ],
  },
  { label: '告警中心', path: '/alarms', icon: AlarmIcon, permission: 'alarm.read' },
  { label: '日志管理', path: '/audit', icon: NotificationIcon, permission: 'audit.read' },
  {
    label: '系统设置',
    icon: SettingIcon,
    permission: 'settings.manage',
    children: [
      { label: '系统状态', path: '/settings', permission: 'settings.manage' },
      { label: '用户权限', path: '/users', permission: 'user.manage' },
    ],
  },
]

const visibleNav = computed<NavItem[]>(() => {
  const result: NavItem[] = []
  for (const item of nav) {
    const children = (item.children || []).filter((child) => auth.can(child.permission))
    const selfVisible = !item.permission || auth.can(item.permission)
    if (!selfVisible && !children.length) continue
    result.push({ ...item, children: children.length ? children : undefined })
  }
  return result
})

const isLogin = computed(() => ['/login', '/change-password'].includes(route.path))
const isMonitor = computed(() => route.path === '/monitor')
const robot = computed(() => monitor.robot)
const activeTask = computed(() => monitor.activeTask)
const openGroup = ref<string>('')

const groupActive = (item: NavItem): boolean =>
  Boolean(item.children?.some((child) => child.path === route.path))

function toggleGroup(item: NavItem): void {
  openGroup.value = openGroup.value === item.label ? '' : item.label
}

const clock = computed(() =>
  new Intl.DateTimeFormat('zh-CN', { dateStyle: 'short', timeStyle: 'medium', hour12: false }).format(
    now.value,
  ),
)
const batteryLevel = computed(() => {
  const value = robot.value?.battery
  if (value == null) return 0
  return Math.max(0, Math.min(100, value))
})
const batteryBars = computed(() => Array.from({ length: 4 }, (_, index) => index + 1 <= Math.ceil(batteryLevel.value / 25)))

const userInitial = computed(() => (auth.user?.display_name || auth.user?.username || '?').slice(0, 1))

watch(
  route,
  (value) => {
    const group = visibleNav.value.find((item) => item.children?.some((child) => child.path === value.path))
    if (group) openGroup.value = group.label
  },
  { immediate: true },
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

function onUserMenuClick(data: { value?: unknown }): void {
  if (data.value === 'logout') void logout()
}
</script>

<template>
  <RouterView v-if="isLogin" />
  <div v-else class="app-shell">
    <aside class="sidebar">
      <div class="brand">
        <img class="brand-logo" :src="brandLogo" alt="友道智造" />
      </div>
      <nav aria-label="主导航">
        <template v-for="item in visibleNav" :key="item.label">
          <RouterLink
            v-if="!item.children"
            :to="item.path || '#'"
            class="nav-item"
            :class="{ active: route.path === item.path }"
            :title="item.label"
          >
            <component :is="item.icon" class="nav-icon" />
            <span>{{ item.label }}</span>
          </RouterLink>
          <div v-else class="nav-group">
            <button
              class="nav-item"
              :class="{ active: groupActive(item), open: openGroup === item.label || groupActive(item) }"
              type="button"
              @click="toggleGroup(item)"
            >
              <component :is="item.icon" class="nav-icon" />
              <span>{{ item.label }}</span>
              <i class="nav-arrow" />
            </button>
            <div v-show="openGroup === item.label || groupActive(item)" class="nav-sub">
              <RouterLink
                v-for="child in item.children"
                :key="child.path"
                :to="child.path"
                class="nav-sub-item"
                :class="{ active: route.path === child.path }"
              >
                <span>{{ child.label }}</span>
              </RouterLink>
            </div>
          </div>
        </template>
      </nav>
      <img class="sidebar-wave" :src="techWave" alt="" aria-hidden="true" />
      <div class="sidebar-foot">科技赋能&nbsp;&nbsp;领航未来</div>
    </aside>
    <main class="workspace">
      <div class="workspace-alert"></div>
      <header class="topbar">
        <div class="status-group">
          <span class="status-cell"
            ><LinkIcon class="status-icon" :class="{ ok: monitor.connected }" /><span
              >链路状态</span
            ><b :class="monitor.connected ? 'ok' : ''">{{
              monitor.connected ? '正常' : '正在重连'
            }}</b></span
          >
          <span class="status-cell"
            ><RobotIcon class="status-icon" :class="{ ok: robot?.online_state === 'ONLINE' }" /><span
              >机器人</span
            ><b :class="robot?.online_state === 'ONLINE' ? 'ok' : ''">{{
              robot?.online_state === 'ONLINE' ? '在线' : '离线'
            }}</b></span
          >
          <span class="status-cell"
            ><BatteryIcon class="status-icon" /><span>电量</span
            ><b>{{ robot?.battery == null ? '--' : `${robot.battery.toFixed(0)}%` }}</b>
            <span class="battery-bars"
              ><i v-for="(on, index) in batteryBars" :key="index" :class="{ full: on }"></i
            ></span>
          </span>
          <span class="status-cell"
            ><TaskIcon class="status-icon" /><span>当前任务</span
            ><b>{{ activeTask ? taskTypeLabel(activeTask.type) : '空闲' }}</b></span
          >
          <span class="status-cell"
            ><LocationIcon class="status-icon" /><span>定位状态</span
            ><b>{{ localizationLabel(robot?.localization_status) }}</b></span
          >
        </div>
        <div class="user-side">
          <time>{{ clock }}</time>
          <t-dropdown
            :options="[{ content: '退出登录', value: 'logout' }]"
            trigger="click"
            @click="onUserMenuClick"
          >
            <button class="user-trigger" type="button">
              <span class="avatar">{{ userInitial }}</span>
              <b>{{ auth.user?.display_name || auth.user?.username }}</b>
              <ChevronDownIcon class="user-chevron" />
            </button>
          </t-dropdown>
        </div>
      </header>
      <section class="page" :class="{ 'page--monitor': isMonitor }">
        <RouterView />
      </section>
    </main>
  </div>
</template>
