// G2：车端诊断面板只调用真实鉴权 API（带 Idempotency-Key），202 只显示“已排队”。
import { mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { nextTick } from 'vue'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import VehicleDiagnosticsPanel from './VehicleDiagnosticsPanel.vue'
import { useMonitorStore } from '@/stores/monitor'
import { useAuthStore } from '@/stores/auth'
import { api } from '@/lib/api'
import type { RobotState } from '@/types'

vi.mock('@/lib/api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/api')>()),
  api: { get: vi.fn(), post: vi.fn() },
}))

function robot(overrides: Partial<RobotState> = {}): RobotState {
  return {
    id: 'fb01',
    vehicle_id: 'firebot-vehicle-01',
    name: 'firebot-001',
    enabled: false,
    online_state: 'ONLINE',
    agent_protocol: 'VEHICLE_AGENT_V1',
    vehicle_agent_execution: {
      profile: 'ros-software-test',
      hardware: 'NO_HARDWARE',
      commands: {
        ping: { mode: 'DIAGNOSTIC', ready: true },
        get_status: { mode: 'DIAGNOSTIC', ready: true },
        get_telemetry: { mode: 'DIAGNOSTIC', ready: true },
        start: { mode: 'SOFTWARE_TEST', ready: true },
        close: { mode: 'SOFTWARE_TEST', ready: true },
        patrol: { mode: 'SOFTWARE_TEST', ready: true },
        stop_patrol: { mode: 'SOFTWARE_TEST', ready: true },
      },
    },
    ...overrides,
  } as RobotState
}

function setup(state: RobotState) {
  setActivePinia(createPinia())
  const monitor = useMonitorStore()
  monitor.snapshot.robots = [state]
  monitor.activeRobotId = state.id!
  monitor.connected = true
  const auth = useAuthStore()
  vi.spyOn(auth, 'can').mockReturnValue(true)
  return monitor
}

describe('VehicleDiagnosticsPanel', () => {
  beforeEach(() => vi.mocked(api.post).mockReset())

  it('未授予执行档的车辆（如 002）不显示面板', () => {
    setup(robot({ id: 'r2', vehicle_id: 'firebot-vehicle-02', vehicle_agent_execution: undefined }))
    const wrapper = mount(VehicleDiagnosticsPanel)
    expect(wrapper.find('[data-testid="vehicle-diagnostics"]').exists()).toBe(false)
  })

  it('Ping 调用真实 API + Idempotency-Key；202 只呈现排队，不呈现成功', async () => {
    const monitor = setup(robot())
    vi.mocked(api.post).mockResolvedValue({
      data: {
        id: '1',
        command_id: 'CMD-1',
        robot_id: 'fb01',
        cmd: 'ping',
        lifecycle_status: 'QUEUED',
        issued_at: new Date().toISOString(),
        payload_json: { execution_mode: 'DIAGNOSTIC' },
      },
    })
    const wrapper = mount(VehicleDiagnosticsPanel)
    expect(wrapper.find('[data-testid="software-test-badge"]').text()).toContain('NO_HARDWARE')
    await wrapper.find('[data-diag-action="ping"]').trigger('click')
    await nextTick()
    await nextTick()
    const [url, body, config] = vi.mocked(api.post).mock.calls[0]!
    expect(url).toBe('/robots/firebot-vehicle-01/agent/commands/ping')
    expect(body).toEqual({ params: {} })
    expect((config as { headers: Record<string, string> }).headers['Idempotency-Key']).toMatch(
      /^[0-9a-f-]{36}$/,
    )
    const outcome = wrapper.find('[data-command-id="CMD-1"] [data-outcome]')
    expect(outcome.attributes('data-outcome')).toBe('running')
    expect(outcome.text()).toContain('已排队')
    expect(monitor.snapshot.commands?.[0]?.command_id).toBe('CMD-1')
  })

  it('离线车辆按钮禁用（不发送）', async () => {
    setup(robot({ online_state: 'OFFLINE' }))
    const wrapper = mount(VehicleDiagnosticsPanel)
    expect(wrapper.find('[data-diag-action="ping"]').attributes('disabled')).toBeDefined()
    await wrapper.find('[data-diag-action="ping"]').trigger('click')
    expect(api.post).not.toHaveBeenCalled()
  })
})
