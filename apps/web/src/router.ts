import { createRouter, createWebHistory, type RouteRecordRaw } from 'vue-router'
import { useAuthStore } from '@/stores/auth'

const routes: RouteRecordRaw[] = [
  { path: '/login', component: () => import('@/views/LoginView.vue'), meta: { public: true } },
  { path: '/change-password', component: () => import('@/views/ChangePasswordView.vue') },
  { path: '/', redirect: '/monitor' },
  {
    path: '/monitor',
    component: () => import('@/views/MonitorView.vue'),
    meta: { permission: 'robot.read' },
  },
  { path: '/robots', component: () => import('@/views/RobotsView.vue'), meta: { permission: 'robot.read' } },
  { path: '/maps', component: () => import('@/views/MapsView.vue'), meta: { permission: 'map.read' } },
  { path: '/parking', component: () => import('@/views/ParkingView.vue'), meta: { permission: 'map.read' } },
  { path: '/tasks', component: () => import('@/views/TasksView.vue'), meta: { permission: 'robot.read' } },
  { path: '/patrol', component: () => import('@/views/PatrolView.vue'), meta: { permission: 'map.read' } },
  { path: '/alarms', component: () => import('@/views/AlarmsView.vue'), meta: { permission: 'alarm.read' } },
  {
    path: '/history',
    component: () => import('@/views/HistoryView.vue'),
    meta: { permission: 'robot.read' },
  },
  { path: '/users', component: () => import('@/views/UsersView.vue'), meta: { permission: 'user.manage' } },
  { path: '/audit', component: () => import('@/views/AuditView.vue'), meta: { permission: 'audit.read' } },
  {
    path: '/settings',
    component: () => import('@/views/SettingsView.vue'),
    meta: { permission: 'settings.manage' },
  },
  { path: '/:pathMatch(.*)*', redirect: '/monitor' },
]

export const router = createRouter({ history: createWebHistory(), routes })

router.beforeEach(async (to) => {
  const auth = useAuthStore()
  await auth.restore()
  if (to.meta.public) return auth.authenticated ? '/monitor' : true
  if (!auth.authenticated) return { path: '/login', query: { next: to.fullPath } }
  if (auth.user?.must_change_password && to.path !== '/change-password') return '/change-password'
  if (!auth.user?.must_change_password && to.path === '/change-password') return '/monitor'
  const permission = to.meta.permission as string | undefined
  if (!auth.can(permission)) return '/monitor'
  return true
})
