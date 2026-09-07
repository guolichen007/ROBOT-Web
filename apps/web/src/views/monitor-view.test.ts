import { mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { defineComponent, nextTick } from 'vue'
import { afterEach, describe, expect, it, vi } from 'vitest'
import MonitorView from './MonitorView.vue'
import { useMonitorStore } from '@/stores/monitor'
import type { RobotState } from '@/types'

// 真实 App shell mount 拓扑：target(#workspace-alert) 与 MonitorView 在同一个父级 mount tick 创建。
// 不提前 document.body.appendChild(target)，避免掩盖 Teleport target 尚未入 document 的根因。
const ShellHarness = defineComponent({
  components: { MonitorView },
  template: `
    <main class="workspace">
      <div id="workspace-alert" class="workspace-alert"></div>
      <section class="page">
        <MonitorView />
      </section>
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

const readyRobot = (): RobotState => ({
  id: 'r001',
  vehicle_id: 'R001',
  name: 'R001',
  enabled: true,
  online_state: 'ONLINE',
  estop_active: false,
  battery: 95,
  localization_status: 'VALID',
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
    estop: { channel: 'estop', support_state: 'CONNECTED', quality: 'GOOD', source_kind: 'MOCK' },
  },
  sensor_profiles: [],
})

afterEach(() => {
  document.body.innerHTML = ''
})

describe('MonitorView renderer regression', () => {
  it('teleport target resolves in same mount tick and projects R001 without runtime error', async () => {
    setActivePinia(createPinia())
    const store = useMonitorStore()
    store.snapshot = emptySnapshot()
    store.activeRobotId = null

    const appErrors: unknown[] = []
    const consoleErrors: string[] = []
    const spy = vi.spyOn(console, 'error').mockImplementation((...args: unknown[]) => {
      consoleErrors.push(args.map((a) => String(a)).join(' '))
    })

    const wrapper = mount(ShellHarness, {
      attachTo: document.body,
      global: {
        config: {
          errorHandler(err: unknown) {
            appErrors.push(err)
          },
        },
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

    // 关键：target 与 MonitorView 同 mount tick 创建，defer 后 host 必须已进入 target
    expect(document.querySelector('#workspace-alert .monitor-situation-host')).toBeTruthy()

    store.snapshot = { ...emptySnapshot(), robots: [readyRobot()] }
    store.activeRobotId = 'R001'
    store.connected = true
    await nextTick()
    await nextTick()

    const text = wrapper.text()
    expect(text).toContain('R001')
    expect(text).toContain('待命')

    const patrolButton = wrapper.findAll('button').find((b) => b.text().includes('开始巡检'))
    expect(patrolButton).toBeTruthy()
    expect(patrolButton!.attributes('disabled')).toBeUndefined()

    const fatal = [...appErrors, ...consoleErrors].filter((e) =>
      /emitsOptions|Cannot read properties of null/.test(String(e)),
    )
    expect(fatal).toEqual([])
    spy.mockRestore()
  })
})
