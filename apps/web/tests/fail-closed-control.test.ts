import { mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import OperationsCommandDock from '@/components/monitor/OperationsCommandDock.vue'
import ManualControl from '@/components/ManualControl.vue'
import ExtinguishActionCards from '@/components/monitor/ExtinguishActionCards.vue'
import type { RobotState } from '@/types'

vi.mock('@/lib/api', () => ({
  api: { get: vi.fn(), post: vi.fn(), delete: vi.fn() },
  errorMessage: (e: unknown) => String(e),
  keepaliveRequest: vi.fn(),
}))
vi.mock('@/stores/auth', () => ({ useAuthStore: () => ({ can: () => true }) }))
vi.mock('@/composables/useHoldToConfirm', () => ({
  useHoldToConfirm: () => ({ start: vi.fn(), cancel: vi.fn(), progress: { value: 0 } }),
}))
vi.mock('@/assets/yd/actions/icon_extinguish_blanket_v4.svg', () => ({ default: '' }))
vi.mock('@/assets/yd/actions/icon_spray_agent_v4.svg', () => ({ default: '' }))
vi.mock('@/assets/yd/actions/icon_joint_extinguish_v4.svg', () => ({ default: '' }))

const TButtonStub = {
  props: ['disabled', 'loading', 'variant', 'theme'],
  template: '<button :disabled="disabled"><slot /></button>',
}
const IconStub = { template: '<span class="icon-stub" />' }

interface DockOverrides {
  reason?: string
  vehicleState?: string
  patrolReady?: boolean
  returnReady?: boolean
  stopReady?: boolean
  estopReady?: boolean
  resetEstopReady?: boolean
}

function mountDock(overrides: DockOverrides = {}) {
  return mount(OperationsCommandDock, {
    props: {
      busy: '',
      reason: overrides.reason ?? '',
      estopActive: false,
      vehicleState: (overrides.vehicleState ?? 'IDLE') as never,
      atWaitingArea: false,
      resumeOptions: { canContinuePatrol: false, canReturnWaiting: false, atWaitingArea: false, interruptedKind: null },
      patrolReady: overrides.patrolReady ?? false,
      returnReady: overrides.returnReady ?? false,
      stopReady: overrides.stopReady ?? false,
      estopReady: overrides.estopReady ?? false,
      resetEstopReady: overrides.resetEstopReady ?? false,
    },
    global: {
      stubs: { TButton: TButtonStub, ControlPlatformIcon: IconStub, HomeIcon: IconStub, StopCircleIcon: IconStub },
    },
  })
}

function realVehicle(): RobotState {
  return {
    id: 'robot-real',
    vehicle_id: 'firebot-vehicle-01',
    enabled: true,
    online_state: 'ONLINE',
    estop_active: false,
    supported_commands: ['manual_control', 'stop_motion', 'emergency_stop', 'reset_estop'],
    manual_control_ready: false,
    safety_command_ready: { stop_motion: false, emergency_stop: false, reset_estop: false },
  }
}

describe('fail-closed control (ROS_COMPAT real vehicle)', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('OperationsCommandDock disables patrol/stop/home/estop when no readiness is available', () => {
    const wrapper = mountDock()
    const buttons = wrapper.findAll('button')
    expect(buttons.length).toBeGreaterThanOrEqual(4)
    for (const btn of buttons) {
      expect(btn.attributes('disabled')).toBeDefined()
    }
  })

  it('OperationsCommandDock does not lock estop when another command is not ready', () => {
    // reason 非空（例如地图合同未验证）只影响 patrol/return，不应禁掉已就绪的软件急停。
    const wrapper = mountDock({
      reason: '地图合同未验证',
      vehicleState: 'PATROLLING',
      estopReady: true,
      resetEstopReady: true,
    })
    expect(wrapper.findAll('button')[0].attributes('disabled')).toBeDefined() // patrol
    expect(wrapper.find('button.hold-estop').attributes('disabled')).toBeUndefined() // estop
  })

  it('ManualControl disables stop/estop/reset when safety_command_ready is false', () => {
    const wrapper = mount(ManualControl, {
      props: { robot: realVehicle(), showSafety: true },
      global: { stubs: { IconStub } },
    })
    expect(wrapper.find('button.direction.stop').attributes('disabled')).toBeDefined()
    expect(wrapper.find('button.estop-button').attributes('disabled')).toBeDefined()
  })

  it('ManualControl enables stop when safety_command_ready.stop_motion is true', () => {
    const ready = realVehicle()
    ready.manual_control_ready = true
    ready.safety_command_ready = { stop_motion: true, emergency_stop: true, reset_estop: true }
    const wrapper = mount(ManualControl, {
      props: { robot: ready, showSafety: true },
      global: { stubs: { IconStub } },
    })
    expect(wrapper.find('button.direction.stop').attributes('disabled')).toBeUndefined()
    expect(wrapper.find('button.estop-button').attributes('disabled')).toBeUndefined()
  })

  it('ExtinguishActionCards disables actions when disabledReason is non-empty', () => {
    const wrapper = mount(ExtinguishActionCards, {
      props: { disabledReason: '当前为只读接入，控制未开放', busyMode: '' },
    })
    for (const btn of wrapper.findAll('button')) {
      expect(btn.attributes('disabled')).toBeDefined()
    }
  })

  it('ExtinguishActionCards enables actions when disabledReason is empty', () => {
    const wrapper = mount(ExtinguishActionCards, {
      props: { disabledReason: '', busyMode: '' },
    })
    expect(wrapper.findAll('button')[0].attributes('disabled')).toBeUndefined()
  })
})
