import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { defineComponent, h, nextTick, ref, type Ref } from 'vue'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { useOperationReadiness } from '@/composables/useOperationReadiness'
import { useAuthStore } from '@/stores/auth'
import type { OperationReadiness, RobotState } from '@/types'
import { serverReadiness } from './fixtures/readiness'

// WEB-R1C R1C-2: the console's single control authority. Every case below is a counterexample the
// R1B implementation accepted (local receive time only, LEGACY fallback, no boot / permission /
// generation binding). Each must now end with NOTHING allowed.
const { getMock, authState } = vi.hoisted(() => ({
  getMock: vi.fn(),
  authState: { user: { id: 'u1', permissions: ['robot.read', 'patrol.create'] as string[] } },
}))
vi.mock('@/lib/api', () => ({ api: { get: getMock }, errorMessage: String }))
vi.mock('@/stores/auth', async () => {
  const { reactive } = await import('vue')
  const state = reactive(authState)
  return { useAuthStore: () => state }
})

const BOOT_A = '11111111-1111-4111-8111-111111111111'
const BOOT_B = '22222222-2222-4222-8222-222222222222'
type Deferred = { resolve: (v: OperationReadiness) => void; reject: (e: unknown) => void }

function harness(robot: Ref<RobotState | undefined>, clock: () => number) {
  let api!: ReturnType<typeof useOperationReadiness>
  const C = defineComponent({
    setup() {
      api = useOperationReadiness(robot, 60_000, clock) // long interval: polls driven explicitly
      return () => h('div')
    },
  })
  const wrapper = mount(C)
  return {
    wrapper,
    get ops() {
      return api
    },
  }
}
const r = (vehicle_id: string, boot_id: string | null = BOOT_A): RobotState =>
  ({ id: vehicle_id, vehicle_id, boot_id, online_state: 'ONLINE', enabled: true }) as RobotState

let now = 1_000_000
const clock = () => now
beforeEach(() => {
  setActivePinia(createPinia())
  now = Date.parse('2026-11-27T08:00:00Z')
  ;(useAuthStore() as unknown as typeof authState).user = {
    id: 'u1',
    permissions: ['robot.read', 'patrol.create'],
  }
  getMock.mockReset()
})
afterEach(() => vi.useRealTimers())

describe('useOperationReadiness — fail closed, single server authority', () => {
  it('unknown (no answer yet) allows nothing — the R1B LEGACY fallback is gone', async () => {
    getMock.mockReturnValue(new Promise(() => {})) // never answers
    const robot = ref(r('R001'))
    const { ops } = harness(robot, clock)
    expect(ops.authority.value).toBe('UNKNOWN')
    expect(ops.allowed('patrol_start', true)).toBe(false) // legacy=true must be ignored
    expect(ops.state('patrol_start').reasons).toEqual(['READINESS_UNKNOWN'])
  })

  it('expires by the SERVER valid_until window, not by an arbitrary local 3 s', async () => {
    // server says valid for 2 s
    getMock.mockResolvedValue({
      data: serverReadiness(
        'R001',
        { patrol_start: true },
        { bootId: BOOT_A, validMs: 2000, evaluatedAt: now },
      ),
    })
    const robot = ref(r('R001'))
    const { ops } = harness(robot, clock)
    await flushPromises()
    expect(ops.allowed('patrol_start')).toBe(true)
    now += 1900
    ops.tick()
    expect(ops.allowed('patrol_start')).toBe(true)
    now += 200 // 2.1 s after receive: past valid_until, still < R1B's 3 s local budget
    ops.tick()
    expect(ops.allowed('patrol_start')).toBe(false)
    expect(ops.failure.value).toBe('READINESS_EXPIRED')
  })

  it('an answer whose valid_until is already in the past (or not after evaluated_at) is rejected', async () => {
    const stale = serverReadiness(
      'R001',
      { patrol_start: true },
      { bootId: BOOT_A, evaluatedAt: now - 60_000, validMs: 2000 },
    )
    getMock.mockResolvedValue({ data: stale })
    const { ops } = harness(ref(r('R001')), clock)
    await flushPromises()
    expect(ops.allowed('patrol_start')).toBe(false)
    expect(ops.failure.value).toBe('READINESS_EXPIRED')
    const broken = {
      ...serverReadiness('R001', { patrol_start: true }, { bootId: BOOT_A }),
      valid_until: '2020-01-01T00:00:00Z',
    }
    getMock.mockResolvedValue({ data: broken })
    await ops.poll()
    expect(ops.allowed('patrol_start')).toBe(false)
  })

  it('a failed refresh invalidates the previous good answer immediately', async () => {
    getMock.mockResolvedValueOnce({
      data: serverReadiness('R001', { stop_motion: true }, { bootId: BOOT_A, evaluatedAt: now }),
    })
    const { ops } = harness(ref(r('R001')), clock)
    await flushPromises()
    expect(ops.allowed('stop_motion')).toBe(true)
    getMock.mockRejectedValueOnce(new Error('502'))
    await ops.poll()
    expect(ops.allowed('stop_motion')).toBe(false)
    expect(ops.failure.value).toBe('READINESS_UNAVAILABLE')
  })

  it('boot change voids the answer even before the next poll; an answer for the old boot is never applied', async () => {
    getMock.mockResolvedValue({
      data: serverReadiness('R001', { stop_motion: true }, { bootId: BOOT_A, evaluatedAt: now }),
    })
    const robot = ref(r('R001', BOOT_A))
    const { ops } = harness(robot, clock)
    await flushPromises()
    expect(ops.allowed('stop_motion')).toBe(true)
    // vehicle rebooted (snapshot/WS now shows BOOT_B); the in-flight server answer still says BOOT_A
    const pending: Deferred[] = []
    getMock.mockImplementation(
      () => new Promise((resolve, reject) => pending.push({ resolve: (d) => resolve({ data: d }), reject })),
    )
    robot.value = r('R001', BOOT_B)
    await nextTick()
    expect(ops.allowed('stop_motion')).toBe(false)
    pending[0].resolve(serverReadiness('R001', { stop_motion: true }, { bootId: BOOT_A, evaluatedAt: now }))
    await flushPromises()
    expect(ops.allowed('stop_motion')).toBe(false)
    expect(ops.failure.value).toBe('READINESS_BOOT_MISMATCH')
  })

  it('permission change (same vehicle) drops the answer and re-asks', async () => {
    getMock.mockResolvedValue({
      data: serverReadiness('R001', { patrol_start: true }, { bootId: BOOT_A, evaluatedAt: now }),
    })
    const { ops } = harness(ref(r('R001')), clock)
    await flushPromises()
    expect(ops.allowed('patrol_start')).toBe(true)
    getMock.mockReturnValue(new Promise(() => {}))
    ;(useAuthStore() as unknown as typeof authState).user = { id: 'u1', permissions: ['robot.read'] } // patrol.create revoked
    await nextTick()
    expect(ops.allowed('patrol_start')).toBe(false)
    expect(ops.authority.value).toBe('UNKNOWN')
  })

  it('A→B→A fast switch: the late answer of the FIRST A request is discarded, B never leaks into A', async () => {
    const pending: Array<Deferred & { vid: string }> = []
    getMock.mockImplementation((url: string) => {
      const vid = /robots\/([^/]+)\//.exec(url)![1]
      return new Promise((resolve, reject) =>
        pending.push({ vid, resolve: (d) => resolve({ data: d }), reject }),
      )
    })
    const robot = ref(r('R001'))
    const { ops } = harness(robot, clock)
    robot.value = r('R002')
    await nextTick()
    robot.value = r('R001')
    await nextTick()
    expect(pending.map((p) => p.vid)).toEqual(['R001', 'R002', 'R001'])
    // the second A answers first: denied
    pending[2].resolve(serverReadiness('R001', { patrol_start: false }, { bootId: BOOT_A, evaluatedAt: now }))
    await flushPromises()
    expect(ops.allowed('patrol_start')).toBe(false)
    // then the FIRST (older generation) A answer arrives late saying allowed → must be ignored
    pending[0].resolve(serverReadiness('R001', { patrol_start: true }, { bootId: BOOT_A, evaluatedAt: now }))
    // and B's answer arrives late too → must never be applied to A
    pending[1].resolve(serverReadiness('R002', { patrol_start: true }, { bootId: BOOT_A, evaluatedAt: now }))
    await flushPromises()
    expect(ops.allowed('patrol_start')).toBe(false)
    expect(ops.readiness.value?.vehicle_id).toBe('R001')
  })

  it('overlapping polls on the same vehicle: an older response arriving last does not overwrite a newer one', async () => {
    const pending: Deferred[] = []
    getMock.mockImplementation(
      () => new Promise((resolve, reject) => pending.push({ resolve: (d) => resolve({ data: d }), reject })),
    )
    const { ops } = harness(ref(r('R001')), clock)
    void ops.poll() // second request
    expect(pending).toHaveLength(2)
    pending[1].resolve(
      serverReadiness(
        'R001',
        { stop_motion: false },
        { bootId: BOOT_A, evaluatedAt: now, frozen: ['DOWNLINK_FROZEN'] },
      ),
    )
    await flushPromises()
    pending[0].resolve(
      serverReadiness('R001', { stop_motion: true }, { bootId: BOOT_A, evaluatedAt: now - 500 }),
    )
    await flushPromises()
    expect(ops.allowed('stop_motion')).toBe(false)
    expect(ops.readiness.value?.control_frozen).toBe(true)
  })

  it('wrong vehicle / non-authoritative source never admits an action', async () => {
    getMock.mockResolvedValueOnce({
      data: serverReadiness('R002', { stop_motion: true }, { bootId: BOOT_A, evaluatedAt: now }),
    })
    const { ops } = harness(ref(r('R001')), clock)
    await flushPromises()
    expect(ops.allowed('stop_motion')).toBe(false)
    getMock.mockResolvedValueOnce({
      data: {
        ...serverReadiness('R001', { stop_motion: true }, { bootId: BOOT_A, evaluatedAt: now }),
        source: 'LEGACY_ROBOT_READINESS',
      },
    })
    await ops.poll()
    expect(ops.allowed('stop_motion')).toBe(false)
    expect(ops.failure.value).toBe('READINESS_NOT_AUTHORITATIVE')
  })
})
