import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { nextTick } from 'vue'
import VideoSurveillancePanel from '@/components/monitor/VideoSurveillancePanel.vue'
import VehicleAgentControls from '@/components/monitor/VehicleAgentControls.vue'
import VehicleAgentKpis from '@/components/monitor/VehicleAgentKpis.vue'
import { agentFreshness, agentTaskLabel, canSendAgent, metric } from '@/lib/vehicle-agent'
import { useMonitorStore } from '@/stores/monitor'
import { useAuthStore } from '@/stores/auth'
import type { ParkingSlot, RobotState } from '@/types'

const { post, get } = vi.hoisted(() => ({ post: vi.fn(), get: vi.fn() }))
vi.mock('@/lib/api', () => ({
  api: { post, get },
  errorMessage: String,
  clearAccessToken: vi.fn(),
  setAccessToken: vi.fn(),
  refreshAccessToken: vi.fn(),
}))
const mounted: Array<{ unmount: () => void }> = []
const slot = { id: 'slot-1', code: '1号车位', enabled: true } as ParkingSlot

function robot(): RobotState {
  return {
    id: 'robot-1',
    vehicle_id: 'MOCK_V1',
    agent_protocol: 'VEHICLE_AGENT_V1',
    online_state: 'ONLINE',
    vehicle_agent_ready: {
      start: true,
      close: true,
      patrol: true,
      stop_patrol: true,
      resume_patrol: true,
      homing: true,
      navigate: true,
      stop_navigate: true,
    },
    agent_state: { status: { online: true, programs: {}, task: { state: 'idle', patrol_paused: false } } },
  }
}

beforeEach(() => {
  setActivePinia(createPinia())
  const monitor = useMonitorStore()
  monitor.snapshot.robots = [robot()]
  monitor.activeRobotId = 'MOCK_V1'
  monitor.connected = true
  useAuthStore().user = {
    id: 'user-1',
    username: 'test',
    display_name: '测试',
    roles: [],
    must_change_password: false,
    permissions: ['robot.control.task', 'robot.control.stop', 'patrol.create'],
  }
  post.mockReset()
  get.mockReset()
})
afterEach(() => {
  mounted.splice(0).forEach((wrapper) => wrapper.unmount())
  vi.useRealTimers()
})

function controls() {
  const wrapper = mount(VehicleAgentControls, { props: { slots: [slot], selectedSlotId: slot.id } })
  mounted.push(wrapper)
  return wrapper
}

describe('vehicle agent HMI', () => {
  it('marks chassis state stale when telemetry continues but status stops', () => {
    const monitor = useMonitorStore()
    monitor.robot!.agent_state!.status_received_at = new Date(Date.now() - 9000).toISOString()
    monitor.robot!.agent_state!.status!.programs.chassis = true
    monitor.robot!.agent_state!.telemetry_received_at = new Date().toISOString()
    monitor.robot!.agent_state!.telemetry_online = true
    const wrapper = mount(VehicleAgentKpis)
    mounted.push(wrapper)
    const state = wrapper.findAll('.hmi-kpi')[0]
    expect(state.attributes('data-stale')).toBe('true')
    expect(state.text()).toContain('已启动')
    expect(state.text()).toContain('陈旧')
    expect(wrapper.findAll('.hmi-kpi')[1].attributes('data-stale')).toBe('false')
  })
  it('renders RGB and Thermal simultaneously with no bottom_ir or video tabs', () => {
    const wrapper = mount(VideoSurveillancePanel, {
      props: { streams: [] },
      global: {
        stubs: {
          VideoCard: { props: ['title'], template: '<article class="fake-card">{{ title }}</article>' },
        },
      },
    })
    mounted.push(wrapper)
    expect(wrapper.findAll('.fake-card')).toHaveLength(2)
    expect(wrapper.text()).toContain('监控相机')
    expect(wrapper.text()).toContain('红外热成像')
    expect(wrapper.text()).not.toContain('bottom_ir')
    expect(wrapper.find('[role="tablist"]').exists()).toBe(false)
  })

  it('maps all task states without claiming goal arrival on an ACK', () => {
    expect(agentTaskLabel('idle')).toBe('IDLE / 空闲')
    expect(agentTaskLabel('patrol')).toBe('PATROL / 巡逻中')
    expect(agentTaskLabel('homing')).toBe('HOMING / 返航中')
    expect(agentTaskLabel('spot')).toBe('NAVIGATING / 前往目标')
    expect(agentTaskLabel()).toBe('--')
  })

  it('requires a true paused status and server navigation readiness', () => {
    const current = robot()
    expect(canSendAgent(current, 'resume_patrol', true)).toBe(false)
    current.agent_state!.status!.task.patrol_paused = true
    expect(canSendAgent(current, 'resume_patrol', true)).toBe(true)
    current.vehicle_agent_ready!.navigate = false
    expect(canSendAgent(current, 'navigate', true)).toBe(false)
    expect(canSendAgent(current, 'start', false)).toBe(false)
  })

  it('emits dropdown slot selection and accepts map-selected slot props', async () => {
    const wrapper = controls()
    await wrapper.find('select').setValue('')
    expect(wrapper.emitted('update:selectedSlotId')?.[0]).toEqual([null])
    await wrapper.setProps({ selectedSlotId: null })
    await wrapper.setProps({ selectedSlotId: slot.id })
    expect((wrapper.find('select').element as HTMLSelectElement).value).toBe(slot.id)
  })

  it('keeps loading through ACK acceptance until server state convergence; shows ACK message/code', async () => {
    const wrapper = controls()
    post.mockResolvedValue({
      data: {
        id: 'cmd-1',
        command_id: 'C-1',
        robot_id: 'robot-1',
        cmd: 'start',
        lifecycle_status: 'QUEUED',
        issued_at: new Date().toISOString(),
      },
    })
    await wrapper.find('[data-action="start"]').trigger('click')
    await flushPromises()
    const monitor = useMonitorStore()
    expect(post).toHaveBeenCalledTimes(1)
    expect(wrapper.find('[data-action="start"]').attributes('aria-busy')).toBe('true')
    await wrapper.find('[data-action="start"]').trigger('click')
    expect(post).toHaveBeenCalledTimes(1)
    monitor.applyEvent({
      stream_id: '1-0',
      event_type: 'command.updated',
      data: { ...monitor.snapshot.commands![0], lifecycle_status: 'ACK_ACCEPTED' },
    })
    await nextTick()
    expect(wrapper.text()).toContain('等待车辆状态确认')
    monitor.applyEvent({
      stream_id: '2-0',
      event_type: 'command.updated',
      data: {
        ...monitor.snapshot.commands![0],
        lifecycle_status: 'ACK_REJECTED',
        ack_payload_json: { code: 'BUSY', success: false, message: '导航程序正忙' },
      },
    })
    await nextTick()
    expect(wrapper.get('[role="alert"]').text()).toBe('导航程序正忙')
    expect(wrapper.get('[role="alert"]').attributes('data-code')).toBe('BUSY')
  })

  it('unknown metrics are -- and offline values are marked stale', () => {
    expect(metric(null, '%')).toBe('--')
    expect(metric(Number.NaN)).toBe('--')
    expect(metric(0, '%')).toBe('0%')
    const monitor = useMonitorStore()
    monitor.connected = false
    const wrapper = mount(VehicleAgentKpis)
    mounted.push(wrapper)
    expect(wrapper.findAll('[data-stale="true"]')).toHaveLength(5)
    expect(wrapper.text()).toContain('--')
    expect(wrapper.text()).toContain('陈旧 / 离线')
    expect(agentFreshness(robot(), false, Date.now()).stale).toBe(true)
  })

  it('receives command deltas without changing active vehicle', () => {
    const monitor = useMonitorStore()
    monitor.applyEvent({
      stream_id: '1-0',
      event_type: 'command.updated',
      data: { id: 'other', robot_id: 'other-robot', command_id: 'C-OTHER' },
    })
    expect(monitor.robot?.vehicle_id).toBe('MOCK_V1')
  })

  it('keeps pre-disconnect telemetry stale after reconnect until a fresh online sample', () => {
    const current = robot()
    current.agent_state!.telemetry_received_at = new Date().toISOString()
    current.agent_state!.telemetry_online = false
    expect(agentFreshness(current, true, Date.now()).stale).toBe(true)
    current.agent_state!.telemetry_online = true
    expect(agentFreshness(current, true, Date.now()).stale).toBe(false)
    // The shared display clock ticks once a second; a just-received frame may
    // legitimately be newer than that clock value without being future-skewed.
    const received = Date.parse(current.agent_state!.telemetry_received_at!)
    expect(agentFreshness(current, true, received - 500).stale).toBe(false)
    expect(agentFreshness(current, true, received - 3000).stale).toBe(true)
  })

  it('allows a distinct stop while a motion command is awaiting ACK', () => {
    const monitor = useMonitorStore()
    monitor.snapshot.commands = [
      {
        id: 'motion',
        command_id: 'C-MOTION',
        robot_id: 'robot-1',
        cmd: 'patrol',
        lifecycle_status: 'PUBLISHED',
        issued_at: new Date().toISOString(),
      },
    ]
    const wrapper = controls()
    expect(wrapper.get('[data-action="stop_patrol"]').attributes('disabled')).toBeUndefined()
    expect(wrapper.get('[data-action="patrol"]').attributes('disabled')).toBeDefined()
  })
})
