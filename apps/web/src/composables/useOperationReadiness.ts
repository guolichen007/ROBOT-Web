import { computed, onUnmounted, ref, watch, type Ref } from 'vue'
import { api } from '@/lib/api'
import { reasonCodeLabel } from '@/lib/ui-labels'
import { useAuthStore } from '@/stores/auth'
import type { OperationReadiness, RobotState } from '@/types'

export interface ActionState {
  allowed: boolean
  reasons: string[]
  labels: string[]
}

/** Why the client currently holds no usable server authority (null = authority is SERVER). */
export type ReadinessFailure =
  | 'READINESS_UNKNOWN'
  | 'READINESS_EXPIRED'
  | 'READINESS_UNAVAILABLE'
  | 'READINESS_VEHICLE_MISMATCH'
  | 'READINESS_BOOT_MISMATCH'
  | 'READINESS_NOT_AUTHORITATIVE'

/**
 * WEB-R1C: the console's SINGLE control authority = server OperationReadiness.
 *
 * Fail closed, never fall back:
 *  - no answer yet (unknown) / request failed / vehicle switched / permissions changed → nothing allowed
 *  - expiry is the SERVER's `valid_until` (bounded by a local receive budget for clock skew)
 *  - the answer must belong to the selected vehicle AND its current boot session
 *  - only `source = SERVER_OPERATION_READINESS` may admit an action
 *  - every request carries a generation; any late response from an older generation (A→B→A,
 *    vehicle/boot/permission change) is discarded, so a stale answer can never be applied.
 * There is NO legacy robot_readiness fallback any more: `allowed()` ignores its legacy argument.
 */
export function useOperationReadiness(
  robot: Ref<RobotState | undefined>,
  intervalMs = 1000,
  clock: () => number = Date.now,
) {
  const auth = useAuthStore()
  const latest = ref<OperationReadiness | null>(null)
  const receivedAt = ref(0)
  const failed = ref<ReadinessFailure | ''>('')
  const now = ref(clock())
  let generation = 0
  let requestSeq = 0
  let appliedSeq = 0
  let timer = 0
  /** local upper bound on how long any answer is trusted, whatever valid_until says */
  const MAX_LOCAL_AGE_MS = 3000

  const permissionKey = computed(() => [...(auth.user?.permissions ?? [])].sort().join(','))
  const scopeKey = computed(
    () =>
      `${robot.value?.vehicle_id ?? ''}|${robot.value?.boot_id ?? ''}|${auth.user?.id ?? ''}|${permissionKey.value}`,
  )

  function invalidate(): void {
    generation += 1
    latest.value = null
    receivedAt.value = 0
    failed.value = ''
  }

  async function poll(): Promise<void> {
    now.value = clock()
    const vid = robot.value?.vehicle_id
    if (!vid) return
    const mine = generation
    const seq = ++requestSeq
    try {
      const { data } = await api.get<OperationReadiness>(`/robots/${vid}/operation-readiness`)
      // late / superseded: another vehicle, boot or permission scope, or an older overlapping poll
      if (mine !== generation || robot.value?.vehicle_id !== vid || seq < appliedSeq) return
      appliedSeq = seq
      if (!data || typeof data !== 'object' || !data.actions) throw new Error('READINESS_UNAVAILABLE')
      latest.value = data
      receivedAt.value = clock()
      now.value = receivedAt.value
      failed.value = ''
    } catch {
      if (mine !== generation || seq < appliedSeq - 1) return
      appliedSeq = Math.max(appliedSeq, seq)
      // a failed refresh invalidates the previous answer immediately (never keep acting on it)
      latest.value = null
      receivedAt.value = 0
      failed.value = 'READINESS_UNAVAILABLE'
    }
  }

  watch(
    scopeKey,
    () => {
      invalidate()
      void poll()
    },
    { immediate: true },
  )
  timer = window.setInterval(() => void poll(), intervalMs)
  onUnmounted(() => {
    generation += 1
    window.clearInterval(timer)
  })

  const failure = computed<ReadinessFailure | null>(() => {
    const r = latest.value
    if (!r) return failed.value || 'READINESS_UNKNOWN'
    if (r.vehicle_id !== robot.value?.vehicle_id) return 'READINESS_VEHICLE_MISMATCH'
    if (r.source !== 'SERVER_OPERATION_READINESS') return 'READINESS_NOT_AUTHORITATIVE'
    // boot: the answer must be about the boot session the console currently shows
    if ((r.boot_id ?? null) !== (robot.value?.boot_id ?? null)) return 'READINESS_BOOT_MISMATCH'
    const validUntil = Date.parse(r.valid_until)
    const evaluated = Date.parse(r.evaluated_at)
    if (!Number.isFinite(validUntil) || !Number.isFinite(evaluated) || validUntil <= evaluated)
      return 'READINESS_EXPIRED'
    // server validity window, measured on the local clock from the receive time (immune to a
    // skewed client clock), and additionally against wall-clock valid_until
    const window = validUntil - evaluated
    if (now.value - receivedAt.value > Math.min(window, MAX_LOCAL_AGE_MS)) return 'READINESS_EXPIRED'
    if (now.value > validUntil + MAX_LOCAL_AGE_MS) return 'READINESS_EXPIRED'
    return null
  })

  /** 'SERVER' = fresh authoritative answer; 'STALE' = we had/asked but it is unusable; 'UNKNOWN' = none yet */
  const authority = computed<'SERVER' | 'STALE' | 'UNKNOWN'>(() =>
    failure.value === null ? 'SERVER' : failure.value === 'READINESS_UNKNOWN' ? 'UNKNOWN' : 'STALE',
  )

  /** Server decision only. The second argument is ignored (kept for call-site compatibility). */
  function allowed(action: string, _legacy?: boolean): boolean {
    void _legacy
    return authority.value === 'SERVER' && latest.value!.actions[action]?.allowed === true
  }
  function reasons(action: string): string[] {
    if (failure.value) return [failure.value]
    const entry = latest.value!.actions[action]
    if (!entry) return ['ACTION_NOT_PROVIDED']
    return entry.allowed ? [] : entry.reasons.length ? entry.reasons : ['NOT_READY']
  }
  /** Server decision for one contract action, with readable reasons (never computed locally). */
  function state(action: string): ActionState {
    const codes = reasons(action)
    return {
      allowed: allowed(action),
      reasons: codes,
      labels: codes.map((code) => reasonCodeLabel(code) || code),
    }
  }
  /** test/diagnostic hook: advance the local clock used for expiry */
  function tick(): void {
    now.value = clock()
  }
  return { readiness: latest, authority, failure, allowed, reasons, state, tick, poll }
}
