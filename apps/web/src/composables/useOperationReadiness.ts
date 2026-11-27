import { computed, onUnmounted, ref, watch, type Ref } from 'vue'
import { api } from '@/lib/api'
import type { OperationReadiness, RobotState } from '@/types'

/**
 * WEB-R1B: poll the server OperationReadiness for the active vehicle (valid ~2 s).
 * Fail closed: no fresh server answer -> every action unavailable, reason shown.
 * Legacy robot_readiness keys are used ONLY when the server does not provide the authority.
 */
export function useOperationReadiness(robot: Ref<RobotState | undefined>, intervalMs = 1000) {
  const latest = ref<OperationReadiness | null>(null)
  const receivedAt = ref(0)
  const failed = ref('')
  const now = ref(Date.now())
  let timer = 0

  async function poll(): Promise<void> {
    now.value = Date.now()
    const vid = robot.value?.vehicle_id
    if (!vid) return
    try {
      const { data } = await api.get(`/robots/${vid}/operation-readiness`)
      if (robot.value?.vehicle_id !== vid) return
      latest.value = data
      receivedAt.value = Date.now()
      failed.value = ''
    } catch {
      failed.value = 'READINESS_UNAVAILABLE'
    }
  }
  watch(
    () => robot.value?.vehicle_id,
    () => {
      latest.value = robot.value?.operation_readiness ?? null
      receivedAt.value = latest.value ? Date.now() : 0
      void poll()
    },
    { immediate: true },
  )
  timer = window.setInterval(() => void poll(), intervalMs)
  onUnmounted(() => window.clearInterval(timer))

  const fresh = computed(
    () =>
      Boolean(latest.value) &&
      latest.value!.vehicle_id === robot.value?.vehicle_id &&
      now.value - receivedAt.value <= 3000,
  )
  const authority = computed<'SERVER' | 'STALE' | 'LEGACY'>(() =>
    fresh.value ? 'SERVER' : latest.value || failed.value ? 'STALE' : 'LEGACY',
  )
  function allowed(action: string, legacy: boolean): boolean {
    if (authority.value === 'SERVER') return latest.value!.actions[action]?.allowed === true
    if (authority.value === 'STALE') return false
    return legacy // transition only: a server without the endpoint
  }
  function reasons(action: string): string[] {
    if (authority.value === 'STALE') return ['READINESS_STALE']
    return latest.value?.actions[action]?.reasons ?? []
  }
  return { readiness: latest, authority, allowed, reasons }
}
