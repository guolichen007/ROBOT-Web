// G2：车端诊断面板只调用真实鉴权 API（带 Idempotency-Key），202 只显示“已排队”。
import { enableAutoUnmount, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { nextTick } from 'vue'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import VehicleDiagnosticsPanel from './VehicleDiagnosticsPanel.vue'
import { useMonitorStore } from '@/stores/monitor'
import { useAuthStore } from '@/stores/auth'
import { api } from '@/lib/api'
import type { AgentCommand, RobotState } from '@/types'

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

// 每个用例后卸载组件，停止其历史轮询定时器（避免跨用例计数）
enableAutoUnmount(afterEach)

describe('VehicleDiagnosticsPanel', () => {
  beforeEach(() => {
    vi.useRealTimers()
    vi.mocked(api.post).mockReset()
    vi.mocked(api.get).mockReset()
    vi.mocked(api.get).mockResolvedValue({ data: [] })
  })

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

  it('历史从服务端按当前车辆读取：刷新后恢复，含 API/其他用户发起的命令', async () => {
    const monitor = setup(robot())
    vi.mocked(api.get).mockResolvedValue({
      data: [
        cmd('API-2', 'patrol', 'SUCCEEDED', {
          ack_payload_json: {
            code: 'OK',
            message: '',
            success: true,
            ack_source: 'vehicle',
            software_feedback: [{ state: 'ACCEPTED' }, { state: 'EXECUTING' }, { state: 'COMPLETED' }],
          },
          payload_json: { execution_mode: 'SOFTWARE_TEST' },
        }),
        cmd('API-1', 'ping', 'SUCCEEDED', {
          ack_payload_json: { code: 'OK', message: '', success: true, ack_source: 'vehicle' },
        }),
      ],
    })
    const wrapper = mount(VehicleDiagnosticsPanel)
    await flush()
    expect(vi.mocked(api.get).mock.calls[0]![0]).toBe('/robots/firebot-vehicle-01/agent/commands')
    const ids = wrapper.findAll('[data-command-id]').map((row) => row.attributes('data-command-id'))
    expect(ids).toEqual(['API-2', 'API-1'])
    const patrol = wrapper.find('[data-command-id="API-2"]')
    expect(patrol.find('[data-col="ack"]').text()).toContain('OK')
    expect(patrol.find('[data-col="feedback"]').text()).toBe('ACCEPTED → EXECUTING → COMPLETED')
    expect(patrol.find('[data-lifecycle]').attributes('data-lifecycle')).toBe('SUCCEEDED')
    expect(wrapper.find('[data-command-id="API-1"] [data-col="feedback"]').text()).toBe('N/A')
    expect(monitor.snapshot.commands?.map((row) => row.command_id)).toContain('API-1')
  })

  it('连点只产生一次 POST（提交中同步加锁）', async () => {
    setup(robot())
    let resolve: (value: unknown) => void = () => {}
    vi.mocked(api.post).mockReturnValue(new Promise((r) => (resolve = r)) as never)
    const wrapper = mount(VehicleDiagnosticsPanel)
    const button = wrapper.find('[data-diag-action="ping"]')
    await button.trigger('click')
    await button.trigger('click')
    await button.trigger('click')
    expect(api.post).toHaveBeenCalledTimes(1)
    resolve({ data: cmd('CMD-9', 'ping', 'QUEUED') })
    await flush()
  })

  it('同类命令执行中时禁用并显示原因', async () => {
    const monitor = setup(robot())
    monitor.snapshot.commands = [cmd('RUN-1', 'patrol', 'EXECUTING')]
    const wrapper = mount(VehicleDiagnosticsPanel)
    expect(wrapper.find('[data-diag-action="patrol"]').attributes('disabled')).toBeDefined()
    expect(wrapper.find('[data-reason-for="patrol"]').text()).toContain('RUN-1')
  })

  it('软件巡检 rounds 受限 1..20 并随请求发送', async () => {
    setup(robot())
    vi.mocked(api.post).mockResolvedValue({ data: cmd('P-1', 'patrol', 'QUEUED') })
    const wrapper = mount(VehicleDiagnosticsPanel)
    await wrapper.find('[data-testid="patrol-rounds"]').setValue('999')
    await wrapper.find('[data-testid="patrol-rounds"]').trigger('change')
    await wrapper.find('[data-diag-action="patrol"]').trigger('click')
    expect(vi.mocked(api.post).mock.calls[0]![1]).toEqual({ params: { rounds: 20 } })
  })

  it('下行关闭：按钮禁用并显示原因，历史仍只读可见', async () => {
    const closed = Object.fromEntries(
      Object.entries(robot().vehicle_agent_execution!.commands).map(([key, value]) => [
        key,
        { ...value, ready: false, reason: 'VEHICLE_AGENT_DOWNLINK_DISABLED' },
      ]),
    )
    setup(robot({ vehicle_agent_execution: { ...robot().vehicle_agent_execution!, commands: closed } }))
    vi.mocked(api.get).mockResolvedValue({ data: [cmd('OLD-1', 'ping', 'SUCCEEDED')] })
    const wrapper = mount(VehicleDiagnosticsPanel)
    await flush()
    expect(wrapper.find('[data-testid="downlink-closed"]').exists()).toBe(true)
    expect(wrapper.find('[data-diag-action="ping"]').attributes('disabled')).toBeDefined()
    expect(wrapper.find('[data-reason-for="ping"]').text()).toContain('下行已关闭')
    expect(wrapper.find('[data-command-id="OLD-1"]').exists()).toBe(true)
    await wrapper.find('[data-diag-action="ping"]').trigger('click')
    expect(api.post).not.toHaveBeenCalled()
  })

  it('模拟车辆显示数据来源为 SIMULATED', () => {
    setup(robot({ simulation: { simulated: true, label: 'SIMULATED', source: 'simulated_vehicle_ids' } }))
    const wrapper = mount(VehicleDiagnosticsPanel)
    expect(wrapper.find('[data-testid="data-source"]').text()).toContain('SIMULATED')
  })

  it('API 不可用：历史轮询指数退避（不每秒请求），恢复后自动刷新', async () => {
    vi.useFakeTimers()
    setup(robot())
    vi.mocked(api.get).mockRejectedValue({ response: { status: 502 } })
    const wrapper = mount(VehicleDiagnosticsPanel)
    await vi.advanceTimersByTimeAsync(60_000)
    const failedCalls = vi.mocked(api.get).mock.calls.length
    // 0s,2s,4s,8s,16s,30s… 60 秒内最多 6 次（每秒轮询会是 60 次）
    expect(failedCalls).toBeLessThanOrEqual(6)
    expect(wrapper.find('[data-testid="history-error"]').exists()).toBe(true)
    vi.mocked(api.get).mockResolvedValue({ data: [cmd('BACK-1', 'ping', 'SUCCEEDED')] })
    await vi.advanceTimersByTimeAsync(30_000)
    expect(wrapper.find('[data-command-id="BACK-1"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="history-error"]').exists()).toBe(false)
  })
})

function cmd(id: string, action: string, status: string, extra: Partial<AgentCommand> = {}): AgentCommand {
  return {
    id,
    command_id: id,
    robot_id: 'fb01',
    cmd: action,
    lifecycle_status: status,
    issued_at: new Date().toISOString(),
    payload_json: { execution_mode: action === 'ping' ? 'DIAGNOSTIC' : 'SOFTWARE_TEST' },
    ...extra,
  }
}

async function flush() {
  for (let i = 0; i < 5; i += 1) await new Promise((r) => setTimeout(r, 0))
}
