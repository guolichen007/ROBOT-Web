// G3：模拟来源标识只由服务端 simulation 字段驱动；真实车辆（含 MOCK 元数据）不显示。
// G3-R2：硬件不配置独立红外测温传感器 → 首页固定四项指标，任何情况下都不显示温度卡片。
import { mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { describe, expect, it } from 'vitest'
import VehicleAgentKpis from './VehicleAgentKpis.vue'
import { useMonitorStore } from '@/stores/monitor'
import type { RobotState } from '@/types'

function mountWith(robot: Partial<RobotState>) {
  setActivePinia(createPinia())
  const monitor = useMonitorStore()
  monitor.snapshot.robots = [{ id: 'x', name: 'x', online_state: 'ONLINE', ...robot } as RobotState]
  monitor.activeRobotId = 'x'
  monitor.connected = true
  return mount(VehicleAgentKpis)
}

const FOUR_KPIS = ['智能车状态', '电池', '烟雾', '通信状态 / 接收延迟']

function expectNoTemperature(wrapper: ReturnType<typeof mountWith>) {
  expect(wrapper.find('[data-kpi="temperature"]').exists()).toBe(false)
  expect(wrapper.text()).not.toContain('温度')
  expect(wrapper.text()).not.toContain('℃')
  // WEB-R1A：未知值现明确显示“未知”；此处只禁止出现温度相关的未知卡片（G3-R2 原意）
  expect(wrapper.text()).not.toMatch(/温度\s*未知/)
  // WEB-R1A：Canonical 烟雾标签带“（单位待验证）”后缀，四项指标集合不变
  expect(wrapper.findAll('.hmi-kpi').map((c) => c.find('span').text().replace('（单位待验证）', ''))).toEqual(
    FOUR_KPIS,
  )
}

describe('VehicleAgentKpis SIMULATED 标识', () => {
  it('服务端登记的模拟来源显示 SIMULATED 横幅，但不显示温度卡片', () => {
    const wrapper = mountWith({
      vehicle_id: 'firebot-sim-ir-01',
      agent_protocol: 'VEHICLE_AGENT_V1',
      simulation: { simulated: true, label: 'SIMULATED', source: 'simulated_vehicle_ids' },
      agent_state: { telemetry: {} },
    })
    expect(wrapper.find('[data-testid="simulated-source-banner"]').text()).toContain('SIMULATED')
    expectNoTemperature(wrapper)
  })

  it('真实车辆 / MOCK 元数据车辆不显示模拟标识', () => {
    for (const vehicle_id of ['firebot-vehicle-01', 'R001']) {
      const wrapper = mountWith({
        vehicle_id,
        agent_protocol: 'VEHICLE_AGENT_V1',
        simulation: null,
        integration: { source_kind: 'MOCK' } as RobotState['integration'],
      })
      expect(wrapper.find('[data-testid="simulated-source-banner"]').exists()).toBe(false)
      expect(wrapper.text()).not.toContain('模拟')
    }
  })
})

describe('G3-R2 无独立温度传感器：不显示温度卡片', () => {
  const v1 = { vehicle_id: 'firebot-vehicle-01', agent_protocol: 'VEHICLE_AGENT_V1' as const }

  it('无温度传感器（telemetry 不含 thermal / 尚无遥测）时固定四项指标', () => {
    expectNoTemperature(mountWith({ ...v1, agent_state: { telemetry: {} } }))
    expectNoTemperature(mountWith({ ...v1 }))
  })

  it('即使收到历史 / 兼容温度数据（thermal.max_temp、thermal=null、top_ir）也不显示温度卡片', () => {
    for (const thermal of [{ max_temp: 67.8 }, { max_temp: 0 }, { max_temp: null }, null]) {
      expectNoTemperature(mountWith({ ...v1, agent_state: { telemetry: { thermal } } }))
    }
    // 旧协议（非 V1）的 top_ir / bottom_ir 字段同样不展示
    expectNoTemperature(
      mountWith({ vehicle_id: 'R002', agent_protocol: 'CANONICAL_MQTT', top_ir: 36.5, bottom_ir: 31 }),
    )
  })

  it('四项指标数值来源不变：电池 / 烟雾 / 延迟', () => {
    const wrapper = mountWith({
      ...v1,
      agent_state: {
        status: { online: true, programs: { chassis: true }, task: { state: 'idle', patrol_paused: false } },
        status_received_at: new Date().toISOString(),
        telemetry_received_at: new Date().toISOString(),
        telemetry_online: true,
        receive_latency_ms: 42,
        telemetry: { battery: { percentage: 73 }, smog: { percentage: 2 }, thermal: { max_temp: 55 } },
      },
    })
    expect(wrapper.find('[data-kpi="battery"] strong').text()).toBe('73%')
    expect(wrapper.find('[data-kpi="smoke"] strong').text()).toBe('2%')
    expect(wrapper.find('[data-kpi="state"]').exists()).toBe(true)
    expect(wrapper.find('[data-kpi="link"]').exists()).toBe(true)
    expectNoTemperature(wrapper)
  })
})
