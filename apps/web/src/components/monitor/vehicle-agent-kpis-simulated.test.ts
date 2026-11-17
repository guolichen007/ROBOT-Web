// G3：模拟来源标识只由服务端 simulation 字段驱动；真实车辆（含 MOCK 元数据）不显示。
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

describe('VehicleAgentKpis SIMULATED 标识', () => {
  it('服务端登记的模拟来源显示 SIMULATED 横幅与“温度（模拟）”', () => {
    const wrapper = mountWith({
      vehicle_id: 'firebot-sim-ir-01',
      agent_protocol: 'VEHICLE_AGENT_V1',
      simulation: { simulated: true, label: 'SIMULATED', source: 'simulated_vehicle_ids' },
      agent_state: { telemetry: { thermal: { max_temp: 42 } } },
    })
    expect(wrapper.find('[data-testid="simulated-source-banner"]').text()).toContain('SIMULATED')
    expect(wrapper.text()).toContain('温度（模拟）')
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

  it('thermal=null 显示“未知”，数值保留一位小数', () => {
    const base = {
      vehicle_id: 'firebot-sim-ir-01',
      agent_protocol: 'VEHICLE_AGENT_V1' as const,
      simulation: { simulated: true as const, label: 'SIMULATED' as const, source: 'simulated_vehicle_ids' },
    }
    const unknown = mountWith({ ...base, agent_state: { telemetry: { thermal: { max_temp: null } } } })
    expect(unknown.find('[data-kpi="temperature"] strong').text()).toBe('未知')
    const nullObject = mountWith({ ...base, agent_state: { telemetry: { thermal: null } } })
    expect(nullObject.find('[data-kpi="temperature"] strong').text()).toBe('未知')
    const known = mountWith({ ...base, agent_state: { telemetry: { thermal: { max_temp: 67.8 } } } })
    expect(known.find('[data-kpi="temperature"] strong').text()).toBe('67.8℃')
    const none = mountWith({ ...base, agent_state: { telemetry: {} } })
    expect(none.find('[data-kpi="temperature"] strong').text()).toBe('--')
  })
})
