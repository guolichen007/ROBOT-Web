import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import DataTable from '@/components/DataTable.vue'
import { detectionDataLabel, detectionDataState, popupQueue } from '@/lib/alarms'
import { canExport, cleanParams, scheduleLabel } from '@/lib/records'
import type { Alarm } from '@/types'

// WEB-R1-MIN 前端反证：每条只证明一个缺陷已被关闭。

const apiGet = vi.fn()
const apiPost = vi.fn()
vi.mock('@/lib/api', () => ({
  api: {
    get: (...args: unknown[]) => apiGet(...args),
    post: (...args: unknown[]) => apiPost(...args),
    put: vi.fn(),
    patch: vi.fn(),
  },
  errorMessage: (e: any) => e?.message || String(e),
  downloadAuthenticated: vi.fn(),
}))
const can = vi.fn((p?: string) => p !== 'robot.control.task')
vi.mock('@/stores/auth', () => ({
  useAuthStore: () => ({
    can,
    user: { id: 'u1', roles: ['operator'], permissions: ['robot.read', 'patrol.create'] },
  }),
}))

const alarm = (over: Partial<Alarm>): Alarm =>
  ({
    id: 'a1',
    robot_id: 'r1',
    event_code: 'FE-1',
    state: 'NEW',
    severity: 'HIGH',
    fire_type: 'smoke',
    occurrence_count: 1,
    detection_method: 'AUTO',
    last_seen_at: '2026-11-22T01:00:00Z',
    ...over,
  }) as Alarm

describe('全局火警弹窗队列', () => {
  it('只弹未确认火警；关闭提示按 id+合并计数隐藏，再次上报重新弹出', () => {
    const rows = [alarm({}), alarm({ id: 'a2', state: 'ACKNOWLEDGED' })]
    expect(popupQueue(rows, new Set()).map((a) => a.id)).toEqual(['a1'])
    expect(popupQueue(rows, new Set(['a1:1']))).toEqual([])
    expect(popupQueue([alarm({ occurrence_count: 2 })], new Set(['a1:1'])).map((a) => a.id)).toEqual(['a1'])
  })
})

describe('没有真实检测数据时不得显示“监测正常”', () => {
  it('无在线检测通道 → 检测数据未接入', () => {
    const state = detectionDataState([{ online_state: 'ONLINE', data_channels: {} }], 0)
    expect(state).toBe('NO_DATA')
    expect(detectionDataLabel(state, 0)).toBe('检测数据未接入')
    expect(detectionDataLabel(state, 0)).not.toContain('正常')
  })
  it('离线车辆的 CONNECTED 通道不算实时数据', () => {
    const robots = [{ online_state: 'OFFLINE', data_channels: { smoke: { support_state: 'CONNECTED' } } }]
    expect(detectionDataState(robots, 0)).toBe('NO_DATA')
  })
  it('有实时检测通道且无活动火警 → 暂无活动火警（仍不写“正常”）', () => {
    const robots = [{ online_state: 'ONLINE', data_channels: { smoke: { support_state: 'CONNECTED' } } }]
    expect(detectionDataLabel(detectionDataState(robots, 0), 0)).toBe('暂无活动火警')
    expect(detectionDataState(robots, 2)).toBe('ALARM')
  })
})

describe('记录筛选与导出参数', () => {
  it('导出与查询共用参数：空值剔除，结束日期取当天末尾', () => {
    const params = cleanParams({ robot_id: 'R001', status: '', end: '2026-11-22' })
    expect(params.robot_id).toBe('R001')
    expect('status' in params).toBe(false)
    expect(new Date(params.end!).getHours()).toBe(23)
  })
  it('只读用户不能导出；审计员可导出审计', () => {
    expect(canExport(['robot.read', 'alarm.read'], 'tasks')).toBe(false)
    expect(canExport(['audit.read', 'robot.read'], 'audit')).toBe(true)
    expect(canExport(['robot.read', 'patrol.create'], 'audit')).toBe(false)
  })
  it('周期显示', () => {
    expect(scheduleLabel({ frequency: 'WEEKLY', time: '08:00', weekdays: ['MON', 'FRI'] })).toBe(
      '每周一、五 08:00',
    )
    expect(scheduleLabel(null)).toBe('未设置')
  })
})

describe('通用表格三态与分页', () => {
  const columns = [{ key: 'name', label: '名称' }]
  it('加载 / 错误（可重试）/ 空态互斥', async () => {
    const loading = mount(DataTable, { props: { columns, rows: [], loading: true } })
    expect(loading.find('[data-state="loading"]').exists()).toBe(true)
    expect(loading.find('[data-state="empty"]').exists()).toBe(false)
    const failed = mount(DataTable, { props: { columns, rows: [], error: '网络错误' } })
    expect(failed.text()).toContain('加载失败：网络错误')
    await failed.find('button').trigger('click')
    expect(failed.emitted('retry')).toHaveLength(1)
    const empty = mount(DataTable, { props: { columns, rows: [], empty: '暂无' } })
    expect(empty.find('[data-state="empty"]').text()).toBe('暂无')
  })
  it('超过页大小时分页', async () => {
    const rows = Array.from({ length: 25 }, (_, i) => ({ id: String(i), name: `n${i}` }))
    const table = mount(DataTable, { props: { columns, rows, pageSize: 10 } })
    expect(table.findAll('tbody tr')).toHaveLength(10)
    expect(table.text()).toContain('第 1 / 3 页')
    await table.findAll('.table-pager button')[1]!.trigger('click')
    expect(table.find('tbody td').text()).toBe('n10')
  })
})

describe('巡检计划页', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    apiGet.mockReset()
    apiPost.mockReset()
  })

  async function mountPatrol(robots: unknown[]) {
    apiGet.mockImplementation((url: string) => {
      if (url === '/plans') return Promise.resolve({ data: [] })
      if (url === '/plans/options') return Promise.resolve({ data: { robots, lateral_range_m: [0.2, 0.3] } })
      return Promise.resolve({ data: [] })
    })
    const { default: PatrolView } = await import('@/views/PatrolView.vue')
    const wrapper = mount(PatrolView, { global: { stubs: { RouterLink: true } } })
    await flushPromises()
    return wrapper
  }

  it('“新建计划”不再永久禁用；无车辆时禁用并说明原因', async () => {
    const empty = await mountPatrol([])
    const button = empty.get('[data-testid="plan-create"]')
    expect(button.attributes('disabled')).toBeDefined()
    expect(empty.text()).toContain('尚未登记车辆')
    const withRobot = await mountPatrol([
      {
        id: 'r1',
        vehicle_id: 'R001',
        name: '一号',
        online_state: 'ONLINE',
        routes: [],
        right_side_mechanism: {},
      },
    ])
    expect(withRobot.get('[data-testid="plan-create"]').attributes('disabled')).toBeUndefined()
  })

  it('车辆无路线时显示“尚未配置路线”，不自动选择第一条路线，也不能保存', async () => {
    const wrapper = await mountPatrol([
      {
        id: 'r1',
        vehicle_id: 'R001',
        name: '一号',
        online_state: 'ONLINE',
        routes: [],
        right_side_mechanism: {},
      },
      {
        id: 'r2',
        vehicle_id: 'R002',
        name: '二号',
        online_state: 'ONLINE',
        routes: [{ id: 't1', code: 'ROUTE-1', map_version: '1', point_count: 4 }],
        right_side_mechanism: {},
      },
    ])
    await wrapper.get('[data-testid="plan-create"]').trigger('click')
    await wrapper.get('select[name="robot_id"]').setValue('r1')
    expect(wrapper.get('[data-testid="no-route"]').text()).toContain('尚未配置路线')
    expect(wrapper.get('[data-testid="plan-save"]').attributes('disabled')).toBeDefined()
    await wrapper.get('select[name="robot_id"]').setValue('r2')
    // 有路线也必须由用户选择，不自动取第一条
    expect((wrapper.get('select[name="trajectory_id"]').element as HTMLSelectElement).value).toBe('')
    expect(wrapper.get('[data-testid="form-blocker"]').text()).toBe('请选择巡检路线')
    expect(apiPost).not.toHaveBeenCalled()
  })
})

describe('任务页', () => {
  it('不再存在“默认取第一个车位和第一条轨迹”的创建巡检入口', async () => {
    setActivePinia(createPinia())
    apiGet.mockImplementation((url: string) =>
      Promise.resolve({
        data:
          url === '/monitor/snapshot'
            ? { robots: [], alarms: [], tasks: [], commands: [], parking_slots: [], trajectories: [] }
            : [],
      }),
    )
    const { default: TasksView } = await import('@/views/TasksView.vue')
    const wrapper = mount(TasksView)
    await flushPromises()
    expect(wrapper.text()).not.toContain('创建巡检任务')
    expect(apiPost).not.toHaveBeenCalledWith('/tasks/patrol', expect.anything(), expect.anything())
  })
})
