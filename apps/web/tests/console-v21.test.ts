import { mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import ConsoleStatusBar from '@/components/monitor/ConsoleStatusBar.vue'
import ManualControl from '@/components/ManualControl.vue'
import { useMonitorStore } from '@/stores/monitor'
import type { RobotState } from '@/types'

const post = vi.fn()
vi.mock('@/lib/api', () => ({
  api: { get: vi.fn(), post: (...args: unknown[]) => post(...args), delete: vi.fn() },
  errorMessage: (e: unknown) => String(e),
  keepaliveRequest: vi.fn(),
}))
let permitted = true
vi.mock('@/stores/auth', () => ({ useAuthStore: () => ({ can: () => permitted }) }))

function robot(over: Partial<RobotState> = {}): RobotState {
  return {
    id: 'r-sim',
    vehicle_id: 'firebot-sim-r002-01',
    enabled: true,
    online_state: 'ONLINE',
    estop_active: false,
    supported_commands: ['manual_control', 'stop_motion'],
    manual_control_ready: true,
    safety_command_ready: { stop_motion: true },
    ...over,
  } as RobotState
}

function withRobot(r?: RobotState, connected = true) {
  const monitor = useMonitorStore()
  monitor.connected = connected
  if (r) {
    monitor.snapshot.robots = [r]
    monitor.selectRobot(r.vehicle_id)
  }
  return mount(ConsoleStatusBar)
}

beforeEach(() => {
  setActivePinia(createPinia())
  post.mockReset()
  permitted = true
})

describe('ConsoleStatusBar — unknown is explicit, dimensions are independent', () => {
  it('with no vehicle every dimension and the pose say 未知', () => {
    const w = withRobot(undefined)
    expect(w.get('[data-testid="console-env"]').text()).toBe('车辆来源未知')
    expect(w.get('[data-testid="console-deploy"]').text()).toContain('未知')
    expect(w.get('[data-testid="console-pose"]').text()).toContain('未知')
    for (const dim of ['link', 'ready', 'task', 'safety']) {
      expect(w.get(`[data-dim="${dim}"]`).attributes('data-level')).toBe('unknown')
    }
  })

  it('e-stop state without a CONNECTED channel is unknown, never "not stopped"', () => {
    const w = withRobot(
      robot({
        server_received_at: new Date().toISOString(),
        data_channels: { estop: { support_state: 'NOT_CONNECTED' } },
      } as unknown as Partial<RobotState>),
    )
    const safety = w.get('[data-dim="safety"]')
    expect(safety.attributes('data-level')).toBe('unknown')
    expect(safety.text()).toContain('急停状态未知')
  })

  it('a reported e-stop latch is never downgraded to unknown (safety direction)', () => {
    const w = withRobot(
      robot({
        estop_active: true,
        server_received_at: new Date().toISOString(),
        data_channels: { estop: { support_state: 'NOT_CONNECTED' } },
      } as unknown as Partial<RobotState>),
    )
    const safety = w.get('[data-dim="safety"]')
    expect(safety.attributes('data-level')).toBe('bad')
    expect(safety.text()).toContain('软件急停锁存')
  })

  it('SIM namespace vehicle is badged SIM; real id is REAL', () => {
    expect(withRobot(robot()).get('[data-testid="console-env"]').attributes('data-env')).toBe('SIM')
    setActivePinia(createPinia())
    const real = withRobot(robot({ id: 'r2', vehicle_id: 'R002' }))
    expect(real.get('[data-testid="console-env"]').attributes('data-env')).toBe('REAL')
  })

  it('shows X / Y / yaw(deg) only for live data and marks stale pose', () => {
    const live = withRobot(
      robot({
        x: 3.2,
        y: -1.5,
        theta: Math.PI / 2,
        localization_status: 'OK',
        server_received_at: new Date().toISOString(),
      }),
    )
    const pose = live.get('[data-testid="console-pose"]')
    expect(pose.text()).toContain('3.20')
    expect(pose.text()).toContain('-1.50')
    expect(pose.text()).toContain('90.0°')
    expect(pose.attributes('data-stale')).toBe('false')
    setActivePinia(createPinia())
    const offline = withRobot(
      robot({ x: 3.2, y: -1.5, theta: 0, server_received_at: new Date(Date.now() - 60_000).toISOString() }),
    )
    expect(offline.get('[data-testid="console-pose"]').text()).toContain('未知')
    expect(offline.get('[data-dim="link"]').text()).toContain('离线')
  })

  it('platform link loss is reported as such, independent of the vehicle', () => {
    const w = withRobot(robot({ server_received_at: new Date().toISOString() }), false)
    expect(w.get('[data-dim="platform"]').text()).toContain('连接中断')
    expect(w.get('[data-dim="link"]').attributes('data-level')).toBe('ok') // vehicle data still live
  })
})

describe('ManualControl — no lease, no frame without authorisation', () => {
  async function forcePress(r: RobotState) {
    const w = mount(ManualControl, { props: { robot: r, showSafety: false } })
    const forward = w.get('button.direction.forward').element as HTMLButtonElement
    forward.disabled = false // bypass the UI guard: the code path itself must refuse
    forward.dispatchEvent(new Event('pointerdown', { cancelable: true }))
    await new Promise((resolve) => setTimeout(resolve, 0))
    return w
  }
  const leaseCalls = () => post.mock.calls.filter(([url]) => String(url).includes('manual-lease'))
  const frameCalls = () => post.mock.calls.filter(([url]) => String(url).includes('commands/manual'))

  it('server readiness false -> no lease request, no frame', async () => {
    const w = await forcePress(robot({ manual_control_ready: false }))
    expect(leaseCalls()).toHaveLength(0)
    expect(frameCalls()).toHaveLength(0)
    expect(w.emitted('notice')?.[0]?.[0]).toContain('无法手动控制')
  })

  it('missing permission -> no lease request, no frame (even with server readiness)', async () => {
    permitted = false
    const w = await forcePress(robot())
    expect(leaseCalls()).toHaveLength(0)
    expect(frameCalls()).toHaveLength(0)
    expect(w.emitted('notice')?.[0]?.[0]).toContain('无手动控制权限')
  })

  it('direction buttons render disabled when not authorised', () => {
    permitted = false
    const w = mount(ManualControl, { props: { robot: robot(), showSafety: false } })
    expect(w.get('button.direction.forward').attributes('disabled')).toBeDefined()
  })

  it('positive control: authorised + ready -> exactly one lease then frames', async () => {
    post.mockImplementation(async (url: string) => {
      if (url.includes('manual-lease'))
        return { data: { lease_id: 'l', control_session_id: 's', expires_at: '' } }
      return { data: { accepted: true } }
    })
    await forcePress(robot())
    expect(leaseCalls()).toHaveLength(1)
    expect(frameCalls().length).toBeGreaterThanOrEqual(1)
  })
})

describe('ConsoleStatusBar — WEB-R1B semantics', () => {
  function mountWith(r: RobotState, props: Record<string, unknown> = {}) {
    const monitor = useMonitorStore()
    monitor.connected = true
    monitor.snapshot.robots = [r]
    monitor.selectRobot(r.vehicle_id)
    return mount(ConsoleStatusBar, { props })
  }
  const live = { server_received_at: new Date().toISOString() }

  it('online is not ready: without a fresh server authority, readiness is 未知', () => {
    const w = mountWith(robot(live))
    expect(w.get('[data-dim="ready"]').text()).toContain('未知')
    const frozen = mountWith(robot(live), {
      readinessAuthority: 'SERVER',
      readiness: { control_frozen: true, actions: {}, freeze_reasons: ['DOWNLINK_FROZEN'] },
    })
    expect(frozen.get('[data-dim="ready"]').text()).toContain('已冻结')
  })

  it('no active task record is not "idle" unless the vehicle reports IDLE on live data', () => {
    expect(mountWith(robot(live)).get('[data-dim="task"]').text()).toContain('车端状态未知')
    setActivePinia(createPinia())
    const idle = mountWith(robot({ ...live, current_mode: 'IDLE' } as Partial<RobotState>))
    expect(idle.get('[data-dim="task"]').text()).toContain('空闲（车端上报）')
  })

  it('future-dated vehicle data is an anomaly and the pose is unknown', () => {
    const w = mountWith(
      robot({ x: 1, y: 2, theta: 0, server_received_at: new Date(Date.now() + 600_000).toISOString() }),
    )
    expect(w.get('[data-dim="link"]').text()).toContain('时间异常')
    expect(w.get('[data-dim="link"]').attributes('data-level')).toBe('bad')
    expect(w.get('[data-testid="console-pose"]').text()).toContain('未知')
  })

  it('deployment environment and vehicle source are shown separately', () => {
    const w = mountWith(robot({ ...live, deployment_environment: 'REAL' }))
    expect(w.get('[data-testid="console-deploy"]').attributes('data-env')).toBe('REAL')
    expect(w.get('[data-testid="console-env"]').attributes('data-env')).toBe('SIM')
  })
})

describe('demo waiting-area coordinates never apply to a real vehicle', () => {
  it('only MOCK fixtures may derive "at waiting area" from (1.2, 1.2)', async () => {
    const { useVehicleOperationState } = await import('@/composables/useVehicleOperationState')
    const { ref } = await import('vue')
    const make = (source: string) =>
      useVehicleOperationState({
        robot: ref(robot({ x: 1.2, y: 1.2, integration: { source_kind: source } } as Partial<RobotState>)),
        activeTask: ref(undefined),
        stopOperation: ref(null),
        requestBusy: ref(''),
        resumeTaskId: ref(null),
        interruptedKind: ref(null),
      } as never).atWaitingArea.value
    expect(make('MOCK')).toBe(true)
    expect(make('CANONICAL_MQTT')).toBe(false)
  })
})
