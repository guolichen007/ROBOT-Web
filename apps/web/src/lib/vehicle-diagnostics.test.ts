import { describe, expect, it } from 'vitest'
import { commandStages, durationLabel } from './vehicle-diagnostics'
import type { AgentCommand } from '@/types'

function row(patch: Partial<AgentCommand>): AgentCommand {
  return {
    id: '1',
    command_id: 'C1',
    robot_id: 'r',
    cmd: 'ping',
    lifecycle_status: 'QUEUED',
    issued_at: '2026-11-12T00:00:00Z',
    payload_json: { execution_mode: 'DIAGNOSTIC' },
    ...patch,
  }
}

describe('G2 车端诊断阶段（只认服务端事实）', () => {
  it('HTTP 202 排队与 Broker 发布都不是执行成功', () => {
    expect(commandStages(row({})).outcome).toBe('running')
    const published = commandStages(row({ lifecycle_status: 'PUBLISHED', published_at: 'x' }))
    expect(published.published).toBe('done')
    expect(published.vehicleAck).toBe('pending')
    expect(published.outcome).toBe('running')
  })

  it('诊断：车端 ACK 成功才算完成', () => {
    const done = commandStages(
      row({
        lifecycle_status: 'SUCCEEDED',
        published_at: 'x',
        ack_payload_json: { code: 'OK', message: '', success: true, ack_source: 'vehicle', duration_ms: 12 },
      }),
    )
    expect(done.vehicleAck).toBe('done')
    expect(done.software).toBe('na')
    expect(done.outcome).toBe('succeeded')
  })

  it('超时只显示未确认，不显示成功', () => {
    const timeout = commandStages(
      row({
        lifecycle_status: 'PUBLISHED_UNCONFIRMED',
        published_at: 'x',
        ack_payload_json: {
          code: 'TIMEOUT',
          message: '超时',
          success: false,
          ack_source: 'transport_timeout',
        },
      }),
    )
    expect(timeout.outcome).toBe('timeout')
    expect(timeout.vehicleAck).toBe('failed')
  })

  it('软件测试：ACK 接收与 ROS 完成分开呈现', () => {
    const received = commandStages(
      row({
        cmd: 'start',
        lifecycle_status: 'ACK_ACCEPTED',
        published_at: 'x',
        payload_json: { execution_mode: 'SOFTWARE_TEST' },
        ack_payload_json: {
          code: 'OK',
          message: '',
          success: true,
          ack_source: 'vehicle',
          software_feedback: [{ state: 'ACCEPTED' }],
        },
      }),
    )
    expect(received.vehicleAck).toBe('done')
    expect(received.software).toBe('pending')
    expect(received.outcome).toBe('running')
    const completed = row({
      cmd: 'start',
      lifecycle_status: 'SUCCEEDED',
      published_at: 'x',
      payload_json: { execution_mode: 'SOFTWARE_TEST' },
      ack_payload_json: {
        code: 'OK',
        message: '',
        success: true,
        ack_source: 'vehicle',
        duration_ms: 3,
        software_feedback: [{ state: 'ACCEPTED' }, { state: 'COMPLETED', duration_ms: 420 }],
      },
    })
    expect(commandStages(completed).software).toBe('done')
    expect(commandStages(completed).outcomeLabel).toContain('SOFTWARE_TEST')
    expect(durationLabel(completed)).toBe('420 ms')
  })

  it('ROS 拒绝显示失败与原因', () => {
    const failed = commandStages(
      row({
        cmd: 'patrol',
        lifecycle_status: 'FAILED',
        ack_reason: 'NOT_READY',
        published_at: 'x',
        payload_json: { execution_mode: 'SOFTWARE_TEST' },
        ack_payload_json: {
          code: 'OK',
          message: '',
          success: true,
          ack_source: 'vehicle',
          software_feedback: [
            { state: 'REJECTED', reason_code: 'NOT_READY', message: 'software not started' },
          ],
        },
      }),
    )
    expect(failed.outcome).toBe('failed')
    expect(failed.software).toBe('failed')
    expect(failed.error).toContain('NOT_READY')
  })
})
