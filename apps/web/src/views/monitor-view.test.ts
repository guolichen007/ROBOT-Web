import { mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { nextTick } from 'vue'
import { afterEach, describe, expect, it, vi } from 'vitest'
import MonitorView from './MonitorView.vue'
import { useMonitorStore } from '@/stores/monitor'
import type { RobotState } from '@/types'

// Run69 renderer regression：monitor snapshot 从空/离线 → R001 就绪的转换，
// 不得产生 Vue runtime "emitsOptions" TypeError，且 Patrol UI 正确投影就绪状态。

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
  localization_status: 'LOCALIZED',
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
  data_channels: {},
  sensor_profiles: [],
})

afterEach(() => {
  document.body.innerHTML = ''
})

describe('MonitorView renderer regression', () => {
  it('empty → R001 ready renders device/patrol and enables patrol without runtime error', async () => {
    setActivePinia(createPinia())
    const store = useMonitorStore()
    store.snapshot = emptySnapshot()
    store.activeRobotId = null

    const target = document.createElement('div')
    target.className = 'workspace-alert'
    document.body.appendChild(target)

    const appErrors: unknown[] = []
    const consoleErrors: string[] = []
    const spy = vi.spyOn(console, 'error').mockImplementation((...args: unknown[]) => {
      consoleErrors.push(args.map((a) => String(a)).join(' '))
    })

    const wrapper = mount(MonitorView, {
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

    store.snapshot = { ...emptySnapshot(), robots: [readyRobot()] }
    store.activeRobotId = 'R001'
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
