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
import {
  batterySeverity,
  channelSeverity,
  effectiveChannelSupportState,
  freshnessSeverity,
  linkSeverity,
  localizationSeverity,
  robotSeverity,
  taskSeverity,
  telemetryValueLabel,
} from '@/lib/telemetry-health'
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
const staleThreshold = computed(() => robot.value?.integration?.stale_seconds ?? null)
const offlineThreshold = computed(() => robot.value?.integration?.offline_seconds ?? null)
const nowMs = computed(() => now.value?.getTime() ?? Date.now())

// 字段级 effective state：随 now 每秒重派生，与 server channel_freshness.py 一致
function effectiveChannelState(channelName: string): string | undefined {
  return effectiveChannelSupportState(
    robot.value?.data_channels?.[channelName],
    staleThreshold.value,
    offlineThreshold.value,
    nowMs.value,
  )
}

const batteryLevel = computed(() => {
  const value = robot.value?.battery
  if (value == null) return 0
  return Math.max(0, Math.min(100, value))
})
// CONNECTED 才显示实时电量条；STALE / ERROR / NOT_CONNECTED / UNSUPPORTED 弱化（清空条）
const batteryBars = computed(() => {
  const state = effectiveChannelState('battery')
  const level = state === 'CONNECTED' ? batteryLevel.value : 0
  return Array.from({ length: 4 }, (_, index) => index + 1 <= Math.ceil(level / 25))
})

const freshnessAge = computed(() => {
  if (!robot.value?.server_received_at) return null
  return Math.max(0, (nowMs.value - Date.parse(robot.value.server_received_at)) / 1000)
})
const freshness = computed(() => {
  const age = freshnessAge.value
  if (age == null) return '数据离线'
  const stale = robot.value?.integration?.stale_seconds ?? 3
  const offline = robot.value?.integration?.offline_seconds ?? 10
  if (age >= offline) return '数据离线'
  if (age >= stale) return `数据陈旧 ${age.toFixed(1)}s`
  return '数据实时'
})
// 电量 severity：CONNECTED 按数值；STALE→warning；NOT_CONNECTED/UNSUPPORTED→unknown；ERROR→danger
const batterySeverityClass = computed(() => {
  const state = effectiveChannelState('battery')
  return state === 'CONNECTED' ? batterySeverity(robot.value?.battery) : channelSeverity(state)
})
const sevClass = computed(() => ({
  link: linkSeverity(monitor.connected),
  robot: robotSeverity(robot.value?.online_state),
  battery: batterySeverityClass.value,
  task: taskSeverity(activeTask.value?.type),
  localization: localizationSeverity(robot.value?.localization_status),
  topIr: channelSeverity(effectiveChannelState('top_ir')),
  bottomIr: channelSeverity(effectiveChannelState('bottom_ir')),
  smoke: channelSeverity(effectiveChannelState('smoke')),
  freshness:
    freshnessAge.value == null
      ? 'danger'
      : freshnessSeverity(
          freshnessAge.value,
          robot.value?.integration?.stale_seconds ?? 3,
          robot.value?.integration?.offline_seconds ?? 10,
        ),
}))
function sevLabel(severity: string): string {
  return (
    { normal: 'ok', active: 'active', warning: 'warn', danger: 'danger', unknown: 'muted' }[severity] || ''
  )
}
function batteryText(): string {
  return telemetryValueLabel(
    robot.value?.battery,
    effectiveChannelState('battery'),
    (v) => `${v.toFixed(0)}%`,
  )
}
function metricValue(value: number | null | undefined, channel: string, unit: string): string {
  return telemetryValueLabel(
    value,
    effectiveChannelState(channel),
    (v) => `${v.toFixed(channel === 'smoke' ? 2 : 1)} ${unit}`,
  )
}

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
        <div class="status-area">
          <div class="status-primary">
            <span class="status-cell"
              ><LinkIcon class="status-icon" :class="sevLabel(sevClass.link)" /><span>链路状态</span
              ><b :class="sevLabel(sevClass.link)">{{ monitor.connected ? '正常' : '正在重连' }}</b></span
            >
            <span class="status-cell"
              ><RobotIcon class="status-icon" :class="sevLabel(sevClass.robot)" /><span>机器人</span
              ><b :class="sevLabel(sevClass.robot)">{{
                robot?.online_state === 'ONLINE' ? '在线' : robot?.online_state === 'STALE' ? '陈旧' : '离线'
              }}</b></span
            >
            <span class="status-cell"
              ><BatteryIcon class="status-icon" /><span>电量</span
              ><b :class="sevLabel(sevClass.battery)">{{ batteryText() }}</b>
              <span class="battery-bars"
                ><i v-for="(on, index) in batteryBars" :key="index" :class="{ full: on }"></i
              ></span>
            </span>
            <span class="status-cell"
              ><TaskIcon class="status-icon" /><span>当前任务</span
              ><b :class="sevLabel(sevClass.task)">{{
                activeTask ? taskTypeLabel(activeTask.type) : '空闲'
              }}</b></span
            >
            <span class="status-cell"
              ><LocationIcon class="status-icon" /><span>定位状态</span
              ><b :class="sevLabel(sevClass.localization)">{{
                localizationLabel(robot?.localization_status)
              }}</b></span
            >
          </div>
          <div class="status-telemetry-row">
            <span class="status-cell status-telemetry"
              ><span>顶部热像</span
              ><b :class="sevLabel(sevClass.topIr)">{{ metricValue(robot?.top_ir, 'top_ir', '℃') }}</b></span
            >
            <span class="status-cell status-telemetry"
              ><span>底部红外</span
              ><b :class="sevLabel(sevClass.bottomIr)">{{
                metricValue(robot?.bottom_ir, 'bottom_ir', '℃')
              }}</b></span
            >
            <span class="status-cell status-telemetry"
              ><span>烟雾浓度</span
              ><b :class="sevLabel(sevClass.smoke)">{{ metricValue(robot?.smoke, 'smoke', '%') }}</b></span
            >
            <span class="status-cell status-telemetry"
              ><span>数据更新</span><b :class="sevLabel(sevClass.freshness)">{{ freshness }}</b></span
            >
          </div>
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
