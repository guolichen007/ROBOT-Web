import { mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { defineComponent, nextTick } from 'vue'
import { afterEach, describe, expect, it, vi } from 'vitest'
import MonitorView from '@/views/MonitorView.vue'
import OperationsCommandDock from '@/components/monitor/OperationsCommandDock.vue'
import { useMonitorStore } from '@/stores/monitor'
import type { OperationReadiness, RobotState } from '@/types'

// WEB-R1C console layout invariants (unit level; geometry is asserted in e2e/helpers/console-layout.ts)
const { getMock, postMock } = vi.hoisted(() => ({ getMock: vi.fn(), postMock: vi.fn() }))
vi.mock('@/lib/api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/api')>()),
  api: { get: getMock, post: postMock },
}))
vi.mock('@/stores/auth', () => ({ useAuthStore: () => ({ can: () => true, user: { id: 'u1' } }) }))

const Shell = defineComponent({
  components: { MonitorView },
  template: `
    <aside class="sidebar"><nav aria-label="主导航"><a>系统设置</a></nav><div id="sidebar-console-slot"></div></aside>
    <main class="workspace"><div id="workspace-alert"></div><section class="page"><MonitorView /></section></main>
  `,
})
const stubs = {
  MapCanvas: true,
  MapSelectionBar: true,
  VideoSurveillancePanel: {
    template:
      '<section class="video-surveillance"><div class="hmi-camera"/><div class="hmi-camera"/></section>',
  },
  PrimaryAlarmPanel: true,
  ProgressRingGate4: true,
  VehicleDiagnosticsPanel: true,
  't-button': {
    props: ['disabled', 'loading'],
    template: '<button :disabled="disabled || loading"><slot /></button>',
  },
}

function readiness(vehicle: string, actions: Record<string, boolean>, frozen = false): OperationReadiness {
  const now = new Date()
  return {
    vehicle_id: vehicle,
    environment: 'REAL',
    evaluated_at: now.toISOString(),
    valid_until: new Date(now.getTime() + 60_000).toISOString(),
    boot_id: '11111111-1111-4111-8111-111111111111',
    source: 'SERVER_OPERATION_READINESS',
    control_frozen: frozen,
    freeze_scope: frozen ? 'ALL' : null,
    freeze_reasons: frozen ? ['DOWNLINK_FROZEN'] : [],
    actions: Object.fromEntries(
      Object.entries(actions).map(([k, v]) => [
        k,
        {
          allowed: v,
          reasons: v ? [] : ['CONTROL_FROZEN'],
          required_permission: 'x.y',
          permitted_for_user: v,
        },
      ]),
    ),
  }
}

function robot(overrides: Partial<RobotState> = {}): RobotState {
  return {
    id: 'r1',
    vehicle_id: 'R001',
    enabled: true,
    online_state: 'ONLINE',
    estop_active: false,
    boot_id: '11111111-1111-4111-8111-111111111111',
    server_received_at: new Date().toISOString(),
    agent_protocol: 'CANONICAL_MQTT',
    readiness_reasons: [],
    integration: { source_kind: 'MOCK', stale_seconds: 3, offline_seconds: 10 } as RobotState['integration'],
    ...overrides,
  } as RobotState
}

const empty = () => ({
  snapshot_watermark: '0-0',
  site: null,
  map: null,
  map_version: null,
  parking_slots: [],
  inspection_points: [],
  extinguish_points: [],
  trajectories: [],
  robots: [],
  alarms: [],
  tasks: [],
  streams: [],
  navigation_presets: [],
  operation_contexts: {},
})

async function mountConsole(r: RobotState) {
  setActivePinia(createPinia())
  const store = useMonitorStore()
  store.snapshot = { ...empty(), robots: [r] } as never
  store.activeRobotId = r.id!
  store.connected = true
  getMock.mockImplementation((url: string) =>
    Promise.resolve({ data: url.endsWith('/operation-readiness') ? r.operation_readiness : null }),
  )
  const wrapper = mount(Shell, { attachTo: document.body, global: { stubs } })
  await nextTick()
  await nextTick()
  await Promise.resolve()
  await nextTick()
  return wrapper
}

afterEach(() => {
  document.body.innerHTML = ''
  getMock.mockReset()
  postMock.mockReset()
})

describe('WEB-R1C console layout', () => {
  it('teleports the control panel into the sidebar slot below the main nav, with 设备/手动/监测 tabs', async () => {
    const w = await mountConsole(
      robot({
        operation_readiness: readiness('R001', {
          stop_motion: true,
          emergency_stop: true,
          protective_stop: false,
        }),
      }),
    )
    const slot = document.querySelector('#sidebar-console-slot')!
    const panel = slot.querySelector('[data-testid="console-control-panel"]')
    expect(panel).toBeTruthy()
    // the slot comes after the main nav in the sidebar (DOM order = visual order in a column)
    const sidebar = document.querySelector('.sidebar')!
    expect([...sidebar.children].indexOf(slot)).toBeGreaterThan(
      [...sidebar.children].indexOf(sidebar.querySelector('nav')!),
    )
    expect(
      [...panel!.querySelectorAll('.console-panel-tabs button')].map((b) => b.textContent!.trim()),
    ).toEqual(['设备', '手动', '监测'])
    // no right-hand control rail any more; the page core holds map + videos only
    expect(document.querySelector('.hmi-control-rail')).toBeNull()
    expect(document.querySelectorAll('.console-side > *')).toHaveLength(1)
    w.unmount()
  })

  it('device tab shows server reasons for start/close and fire shield; feedback never claims a position', async () => {
    const w = await mountConsole(robot({ operation_readiness: readiness('R001', { stop_motion: true }) }))
    const device = document.querySelector('[data-testid="device-fire-shield"]')!
    const buttons = [...device.querySelectorAll('button')]
    expect(buttons.map((b) => b.textContent!.trim())).toEqual(['展开', '停止', '回收'])
    for (const b of buttons) expect(b.hasAttribute('disabled')).toBe(true)
    // the server did not provide fire_shield_* → explicit reason, not a silent grey button
    expect(device.textContent).toContain('服务端未提供该动作资格')
    expect(device.querySelector('.device-feedback')!.textContent).toContain('未知')
    expect(device.textContent).not.toMatch(/已展开|已回收|已到位/)
    const power = document.querySelector('[data-testid="device-power"]')!
    expect([...power.querySelectorAll('button')].every((b) => b.hasAttribute('disabled'))).toBe(true)
    expect(power.textContent).toContain('服务端未提供该动作资格')
    w.unmount()
  })

  it('bottom bar holds patrol / pause-resume / goal / return; stop + e-stop + protective stop always rendered', async () => {
    const w = await mountConsole(
      robot({
        operation_readiness: readiness('R001', {
          patrol_start: true,
          stop_motion: true,
          emergency_stop: true,
        }),
      }),
    )
    const task = w.find('[data-testid="console-task-group"]')
    const text = task.text()
    for (const label of ['开始巡检', '暂停巡检', '目标导航', '返回等待区']) expect(text).toContain(label)
    // goal without a selected slot: disabled, and the reason is visible in the bar (not tooltip-only)
    expect(w.find('[data-testid="dock-navigate"]').attributes('disabled')).toBeDefined()
    expect(w.find('[data-testid="dock-hint"]').exists() || w.find('.dock-status').text().length > 0).toBe(
      true,
    )
    const safety = w.find('[data-testid="console-safety-group"]')
    expect(safety.find('.dock-stop').exists()).toBe(true)
    expect(safety.find('.hold-estop').exists()).toBe(true)
    expect(safety.find('button.protective-stop').exists()).toBe(true)
    // switching left tabs never removes any safety entry
    for (const tab of ['manual', 'monitor', 'device']) {
      ;(document.querySelector(`.console-panel-tabs button[data-tab="${tab}"]`) as HTMLButtonElement).click()
      await nextTick()
      expect(w.find('[data-testid="console-safety-group"] .dock-stop').exists()).toBe(true)
      expect(w.find('[data-testid="console-safety-group"] .hold-estop').exists()).toBe(true)
      expect(w.find('button.protective-stop').exists()).toBe(true)
    }
    w.unmount()
  })

  it('frozen server readiness keeps every task button disabled (moving the panel adds no bypass)', async () => {
    const w = await mountConsole(
      robot({
        operation_readiness: readiness(
          'R001',
          {
            patrol_start: false,
            return_home: false,
            stop_motion: false,
            emergency_stop: false,
            patrol_pause: false,
          },
          true,
        ),
        autonomous_task_ready: { patrol: true, return_dock: true },
        safety_command_ready: { stop_motion: true, emergency_stop: true, reset_estop: true },
      }),
    )
    const buttons = w.findAll('[data-testid="console-action-bar"] button')
    expect(buttons.length).toBeGreaterThanOrEqual(7)
    for (const b of buttons) expect(b.attributes('disabled'), b.text()).toBeDefined()
    expect(w.find('[data-testid="console-action-bar"]').text()).toContain('控制已冻结')
    w.unmount()
  })

  it('V1 vehicle: start/close in the device tab and task buttons in the bar come from ONE command tracker', async () => {
    const w = await mountConsole(
      robot({
        agent_protocol: 'VEHICLE_AGENT_V1',
        vehicle_agent_ready: { start: true, close: true, patrol: true },
        agent_state: {
          status: {
            online: true,
            programs: { chassis: true },
            task: { state: 'idle', patrol_paused: false },
          },
        } as RobotState['agent_state'],
        operation_readiness: readiness('R001', {
          start_vehicle: true,
          shutdown_vehicle: true,
          patrol_start: true,
          stop_motion: false,
          emergency_stop: false,
        }),
      }),
    )
    postMock.mockResolvedValue({
      data: { command_id: 'c-1', robot_id: 'r1', cmd: 'start', lifecycle_status: 'QUEUED' },
    })
    const start = document.querySelector(
      '[data-testid="device-power"] [data-action="start"]',
    ) as HTMLButtonElement
    expect(start).toBeTruthy()
    expect(document.querySelectorAll('[data-action="start"]')).toHaveLength(1)
    expect(document.querySelectorAll('[data-action="patrol"]')).toHaveLength(1)
    start.click()
    await nextTick()
    await Promise.resolve()
    await nextTick()
    expect(postMock).toHaveBeenCalledTimes(1)
    expect(postMock.mock.calls[0][0]).toBe('/robots/R001/agent/commands/start')
    // the bar sees the SAME pending command: patrol is locked while start is pending
    const patrol = document.querySelector(
      '[data-testid="console-task-group"] [data-action="patrol"]',
    ) as HTMLButtonElement
    expect(patrol.disabled).toBe(true)
    w.unmount()
  })
})

describe('OperationsCommandDock pause / resume', () => {
  const base = {
    busy: '',
    reason: '',
    estopActive: false,
    atWaitingArea: false,
    patrolReady: true,
    returnReady: true,
    stopReady: true,
    estopReady: true,
    resetEstopReady: true,
  }
  const g = { stubs: { 't-button': stubs['t-button'] } }
  it('shows 继续巡检 in the pause slot when a paused patrol can continue, and emits resume', async () => {
    const w = mount(OperationsCommandDock, {
      props: {
        ...base,
        vehicleState: 'PAUSED_SAFE',
        resumeReady: true,
        resumeOptions: {
          canContinuePatrol: true,
          canReturnWaiting: false,
          atWaitingArea: false,
          interruptedKind: 'patrol',
        },
      },
      global: g,
    })
    const btn = w.find('[data-testid="dock-pause-resume"]')
    expect(btn.text()).toContain('继续巡检')
    expect(btn.attributes('disabled')).toBeUndefined()
    await btn.trigger('click')
    expect(w.emitted('resume')).toHaveLength(1)
    // 开始巡检 does not double as continue
    expect(w.findAll('button')[0].attributes('disabled')).toBeDefined()
  })
  it('pause is disabled without server pause readiness even while patrolling', () => {
    const w = mount(OperationsCommandDock, {
      props: {
        ...base,
        vehicleState: 'PATROLLING',
        pauseReady: false,
        resumeOptions: {
          canContinuePatrol: false,
          canReturnWaiting: false,
          atWaitingArea: false,
          interruptedKind: null,
        },
      },
      global: g,
    })
    expect(w.find('[data-testid="dock-pause-resume"]').attributes('disabled')).toBeDefined()
  })
})
