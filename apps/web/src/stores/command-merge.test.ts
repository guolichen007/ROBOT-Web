// G2G3-R1：诊断历史（HTTP）与 WS 增量合并竞态反证。
import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { useMonitorStore } from './monitor'
import type { AgentCommand } from '@/types'

vi.mock('@/lib/api', () => ({ api: { get: vi.fn(), post: vi.fn() } }))

const T0 = '2026-11-17T05:00:00.000Z'
const T1 = '2026-11-17T05:00:01.000Z'
const T2 = '2026-11-17T05:00:02.000Z'
const T3 = '2026-11-17T05:00:03.000Z'

function row(status: string, extra: Partial<AgentCommand> = {}): AgentCommand {
  return {
    id: 'cmd-1',
    command_id: 'C-1',
    robot_id: 'fb01',
    cmd: 'patrol',
    lifecycle_status: status,
    issued_at: T0,
    ...extra,
  }
}

function ws(store: ReturnType<typeof useMonitorStore>, stream: string, data: AgentCommand) {
  store.applyEvent({ stream_id: stream, event_type: 'command.updated', data })
}

describe('命令行 HTTP/WS 合并竞态', () => {
  beforeEach(() => setActivePinia(createPinia()))

  it('旧 HTTP 响应晚于 WS 终态到达：不得把 SUCCEEDED 改回 EXECUTING', () => {
    const store = useMonitorStore()
    ws(store, '100-0', row('SUCCEEDED', { published_at: T1, ack_at: T1, terminal_at: T3 }))
    const changed = store.mergeCommands([row('EXECUTING', { published_at: T1, ack_at: T1 })])
    expect(changed).toBe(0)
    expect(store.snapshot.commands?.[0]?.lifecycle_status).toBe('SUCCEEDED')
  })

  it('HTTP 合并不推进/改写 WS watermark', () => {
    const store = useMonitorStore()
    ws(store, '100-0', row('PUBLISHED', { published_at: T1 }))
    store.mergeCommands([row('SUCCEEDED', { published_at: T1, ack_at: T1, terminal_at: T2 })])
    expect(store.lastStreamId).toBe('100-0')
    expect(store.snapshot.snapshot_watermark).toBe('100-0')
  })

  it('HTTP 带来更新的事实（WS 丢失增量）时正常前进', () => {
    const store = useMonitorStore()
    ws(store, '100-0', row('PUBLISHED', { published_at: T1 }))
    expect(store.mergeCommands([row('SUCCEEDED', { published_at: T1, ack_at: T2, terminal_at: T3 })])).toBe(1)
    expect(store.snapshot.commands?.[0]?.lifecycle_status).toBe('SUCCEEDED')
  })

  it('重复终态（WS 重放 / HTTP 再次返回同一终态）幂等', () => {
    const store = useMonitorStore()
    const done = row('CANCELLED', { published_at: T1, ack_at: T1, terminal_at: T2, ack_reason: 'STOPPED' })
    ws(store, '100-0', done)
    expect(store.mergeCommands([done, done])).toBe(0)
    ws(store, '101-0', done)
    expect(store.snapshot.commands).toHaveLength(1)
    expect(store.snapshot.commands?.[0]?.ack_reason).toBe('STOPPED')
  })

  it('WS 重放旧的非终态不得撤销已确认终态', () => {
    const store = useMonitorStore()
    ws(store, '100-0', row('FAILED', { published_at: T1, terminal_at: T2 }))
    ws(store, '101-0', row('ACK_ACCEPTED', { published_at: T1, ack_at: T1 }))
    expect(store.snapshot.commands?.[0]?.lifecycle_status).toBe('FAILED')
  })

  it('PUBLISHED_UNCONFIRMED 之后迟到 ACK（服务端合法回转）可前进', () => {
    const store = useMonitorStore()
    ws(store, '100-0', row('PUBLISHED_UNCONFIRMED', { published_at: T1 }))
    expect(store.mergeCommands([row('ACK_ACCEPTED', { published_at: T1, ack_at: T2 })])).toBe(1)
    expect(store.snapshot.commands?.[0]?.lifecycle_status).toBe('ACK_ACCEPTED')
  })

  it('ACK_ACCEPTED → EXECUTING（时间戳不变）可前进，反向不回退', () => {
    const store = useMonitorStore()
    ws(store, '100-0', row('ACK_ACCEPTED', { published_at: T1, ack_at: T2 }))
    expect(store.mergeCommands([row('EXECUTING', { published_at: T1, ack_at: T2 })])).toBe(1)
    expect(store.mergeCommands([row('ACK_ACCEPTED', { published_at: T1, ack_at: T2 })])).toBe(0)
    expect(store.snapshot.commands?.[0]?.lifecycle_status).toBe('EXECUTING')
  })
})
