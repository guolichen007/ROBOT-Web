import { mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import OperationsCommandDock from '@/components/monitor/OperationsCommandDock.vue'
import ManualControl from '@/components/ManualControl.vue'
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

const TButtonStub = {
  props: ['disabled', 'loading', 'variant', 'theme'],
  template: '<button :disabled="disabled"><slot /></button>',
}
const IconStub = { template: '<span class="icon-stub" />' }

function mountDock(reason: string) {
  return mount(OperationsCommandDock, {
    props: {
      busy: '',
      reason,
      estopActive: false,
      vehicleState: 'IDLE',
      atWaitingArea: false,
      resumeOptions: { canContinuePatrol: false, canReturnWaiting: false, atWaitingArea: false, interruptedKind: null },
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

  it('OperationsCommandDock disables patrol/stop/home/estop when reason is non-empty', () => {
    const wrapper = mountDock('当前为只读接入，控制未开放')
    const buttons = wrapper.findAll('button')
    // patrol + stop + home + estop
    expect(buttons.length).toBeGreaterThanOrEqual(4)
    for (const btn of buttons) {
      expect(btn.attributes('disabled')).toBeDefined()
    }
  })

  it('OperationsCommandDock keeps buttons enabled when reason is empty and vehicle is idle', () => {
    const wrapper = mountDock('')
    const patrol = wrapper.findAll('button')[0]
    expect(patrol.attributes('disabled')).toBeUndefined()
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
})
