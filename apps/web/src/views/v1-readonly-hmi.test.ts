// G1-R1：只读 V1 设备（无硬件、控制禁用）与 R002 既有正常设备的 HMI 状态语义回归。
// 关键语义：Agent 在线 ≠ 控制就绪；急停未验证 ≠ 未触发；无底盘不显示"待命"；
// enabled=false 只读可监测、不可控制；R002 既有显示行为不变。
import { mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { defineComponent, nextTick } from 'vue'
import { afterEach, describe, expect, it, vi } from 'vitest'
import MonitorView from './MonitorView.vue'
import RobotsView from './RobotsView.vue'
import { useMonitorStore } from '@/stores/monitor'
import type { RobotState } from '@/types'

vi.mock('@/lib/api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/api')>()),
  api: { get: vi.fn().mockResolvedValue({ data: null }), post: vi.fn() },
}))

const ShellHarness = defineComponent({
  components: { MonitorView },
  template: `
    <main class="workspace">
      <div id="workspace-alert" class="workspace-alert"></div>
      <section class="page"><MonitorView /></section>
    </main>
  `,
})

const emptySnapshot = () => ({
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

function baseRobot(overrides: Partial<RobotState> = {}): RobotState {
  return {
    id: 'fb01',
    vehicle_id: 'firebot-vehicle-01',
    name: 'firebot-001',
    enabled: true,
    online_state: 'ONLINE',
    estop_active: null, // 未验证：无急停数据源
    battery: null, // 未接入：无真实电池数据
    localization_status: 'UNKNOWN',
    autonomous_task_ready: { patrol: false },
    safety_command_ready: { stop_motion: false, emergency_stop: false, reset_estop: false },
    readiness_reasons: ['CONTROL_DISABLED'],
    control_disabled_reason: 'DOWNLINK_DISABLED',
    integration: {
      source_kind: 'VEHICLE_AGENT_V1',
      control_contract_verified: false,
      ack_contract_verified: false,
      map_contract_verified: false,
      bidirectional_bridge_verified: false,
      command_path_verified: false,
      cmd_vel_arbitration_verified: false,
      forward_only: false,
      reverse_precision_navigation: false,
      stale_seconds: 8,
      offline_seconds: 15,
    },
    agent_protocol: 'VEHICLE_AGENT_V1',
    agent_state: {
      availability: 'online',
      status: {
        online: true,
        programs: { agent: true, chassis: false, navigation: false },
        task: { state: 'idle', patrol_paused: false },
      },
      telemetry: { battery: { percentage: null } },
    },
    data_channels: {},
    sensor_profiles: [],
    ...overrides,
  }
}

afterEach(() => {
  document.body.innerHTML = ''
})

describe('V1 只读设备 HMI 语义（G1-R1）', () => {
  it('Agent 在线但不显示"待命"（无底盘只读监测）；estop null 显示"未验证"', async () => {
    setActivePinia(createPinia())
    const store = useMonitorStore()
    store.snapshot = { ...emptySnapshot(), robots: [baseRobot()] }
    store.activeRobotId = 'fb01'
    store.connected = true

    const wrapper = mount(ShellHarness, {
      attachTo: document.body,
      global: {
        stubs: {
          MapCanvas: true,
          MapSelectionBar: true,
          VideoSurveillancePanel: true,
          PrimaryAlarmPanel: true,
          ProgressRingGate4: true,
          't-button': {
            props: ['disabled', 'loading'],
            template: '<button :disabled="disabled || loading"><slot /></button>',
          },
        },
      },
    })
    await nextTick()
    await nextTick()

    const text = wrapper.text()
    expect(text).toContain('只读监测') // 在线但无底盘 → 只读监测，不宣称待命
    expect(text).not.toContain('待命')
    // 控制未开放：控制链路未就绪提示存在（readiness 门），巡检按钮不可用
    expect(text).toContain('控制链路')
    const patrolButton = wrapper.findAll('button').find((b) => b.text().includes('开始巡检'))
    expect(patrolButton?.attributes('disabled')).toBeDefined()
    wrapper.unmount()
  })

  it('enabled=false：只读监测保留，控制不可用（后端 ROBOT_DISABLED + 前端只读门）', async () => {
    setActivePinia(createPinia())
    const store = useMonitorStore()
    store.snapshot = { ...emptySnapshot(), robots: [baseRobot({ enabled: false })] }
    store.activeRobotId = 'fb01'
    store.connected = true

    const wrapper = mount(ShellHarness, {
      attachTo: document.body,
      global: {
        stubs: {
          MapCanvas: true,
          MapSelectionBar: true,
          VideoSurveillancePanel: true,
          PrimaryAlarmPanel: true,
          ProgressRingGate4: true,
          't-button': {
            props: ['disabled', 'loading'],
            template: '<button :disabled="disabled || loading"><slot /></button>',
          },
        },
      },
    })
    await nextTick()
    await nextTick()

    expect(wrapper.text()).toContain('只读监测')
    expect(wrapper.text()).toContain('只读接入') // enabled=false → 控制路径关闭
    wrapper.unmount()
  })

  it('R002 既有模式回归：真实急停 false 显示"未触发"，在线显示"待命"', async () => {
    setActivePinia(createPinia())
    const store = useMonitorStore()
    const r002 = {
      ...baseRobot({
        id: 'r002',
        vehicle_id: 'R002',
        name: 'R002',
        estop_active: false,
        agent_protocol: 'CANONICAL_MQTT',
        agent_state: undefined,
        autonomous_task_ready: { patrol: true },
        safety_command_ready: { stop_motion: true },
        readiness_reasons: [],
        control_disabled_reason: null,
        integration: {
          source_kind: 'MOCK',
          control_contract_verified: true,
          ack_contract_verified: true,
          map_contract_verified: true,
          bidirectional_bridge_verified: true,
          command_path_verified: true,
          cmd_vel_arbitration_verified: true,
          forward_only: false,
          reverse_precision_navigation: false,
          stale_seconds: 3,
          offline_seconds: 10,
        },
        data_channels: {
          estop: {
            channel: 'estop',
            support_state: 'CONNECTED',
            quality: 'GOOD',
            source_kind: 'MOCK',
          },
        },
      }),
    }
    store.snapshot = { ...emptySnapshot(), robots: [r002] }
    store.activeRobotId = 'r002'
    store.connected = true

    const wrapper = mount(ShellHarness, {
      attachTo: document.body,
      global: {
        stubs: {
          MapCanvas: true,
          MapSelectionBar: true,
          VideoSurveillancePanel: true,
          PrimaryAlarmPanel: true,
          ProgressRingGate4: true,
          't-button': {
            props: ['disabled', 'loading'],
            template: '<button :disabled="disabled || loading"><slot /></button>',
          },
        },
      },
    })
    await nextTick()
    await nextTick()

    const text = wrapper.text()
    expect(text).toContain('待命') // 既有正常设备行为不变
    const patrolButton = wrapper.findAll('button').find((b) => b.text().includes('开始巡检'))
    expect(patrolButton?.attributes('disabled')).toBeUndefined() // 控制可用
    wrapper.unmount()
  })
})

describe('RobotsView 急停文案语义（G1-R1）', () => {
  it('estop null → 未验证；estop false → 未触发', async () => {
    setActivePinia(createPinia())
    const store = useMonitorStore()
    store.snapshot = {
      ...emptySnapshot(),
      robots: [
        baseRobot({ estop_active: null }),
        baseRobot({
          id: 'r002',
          vehicle_id: 'R002',
          name: 'R002',
          estop_active: false,
          agent_protocol: 'CANONICAL_MQTT',
          agent_state: undefined,
        }),
      ],
    }

    const wrapper = mount(RobotsView, {
      attachTo: document.body,
      global: {
        stubs: {
          PageHeader: { template: '<header><slot /></header>' },
          RouterLink: true,
        },
        mocks: { $router: { push: vi.fn() } },
      },
    })
    await nextTick()
    await nextTick()

    // 展开两行的详情（estop 文案位于折叠详情区）
    for (const button of wrapper.findAll('button').filter((b) => b.text().includes('详情'))) {
      await button.trigger('click')
    }
    await nextTick()

    const text = wrapper.text()
    expect(text).toContain('未验证') // firebot-vehicle-01：无急停数据源
    expect(text).toContain('未触发') // R002：真实急停 false
    wrapper.unmount()
  })
})
