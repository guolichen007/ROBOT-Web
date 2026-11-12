import type { AgentCommand } from '@/types'

// G2 车端诊断 / ROS 软件测试面板：所有阶段只来自服务端事实（Command 行），
// HTTP 202 = 已排队，Broker PUBACK = 已发布（≠ 车端收到），车端 ACK = 车端收到/执行诊断，
// ROS COMPLETED 反馈 = 软件执行完成。绝不把 202/PUBACK 当作执行成功。
export const diagnosticActions = [
  ['ping', 'Ping'],
  ['get_status', '获取状态'],
  ['get_telemetry', '获取遥测'],
] as const

export const softwareActions = [
  ['start', '软件启动', { with_navigation: true }],
  ['close', '软件停止', {}],
  ['patrol', 'ROS 软件巡检测试', {}],
  ['stop_patrol', '停止软件巡检', {}],
] as const

export type DiagnosticAction = (typeof diagnosticActions)[number][0] | (typeof softwareActions)[number][0]

export type StageState = 'done' | 'pending' | 'failed' | 'idle'

export interface CommandStages {
  queued: StageState
  published: StageState
  vehicleAck: StageState
  software: StageState | 'na'
  outcome: 'running' | 'succeeded' | 'failed' | 'timeout' | 'unconfirmed'
  outcomeLabel: string
  error: string
}

const ACTIVE = ['CREATED', 'QUEUED', 'PUBLISHED', 'ACK_ACCEPTED', 'EXECUTING']

export function isActive(row: AgentCommand | undefined): boolean {
  return Boolean(row && ACTIVE.includes(row.lifecycle_status))
}

export function commandStages(row: AgentCommand): CommandStages {
  const status = row.lifecycle_status
  const ack = row.ack_payload_json
  const software = row.payload_json?.execution_mode === 'SOFTWARE_TEST'
  const vehicleAck = ack?.ack_source === 'vehicle'
  const feedback = ack?.software_feedback ?? []
  const lastFeedback = feedback[feedback.length - 1]
  const published = Boolean(row.published_at) || !['CREATED', 'QUEUED', 'EXPIRED'].includes(status)
  const failed = ['ACK_REJECTED', 'ACK_UNSUPPORTED', 'FAILED', 'EXPIRED'].includes(status)
  let outcome: CommandStages['outcome'] = 'running'
  let outcomeLabel = '执行中'
  if (status === 'SUCCEEDED') {
    outcome = 'succeeded'
    outcomeLabel = software ? '软件执行完成（SOFTWARE_TEST，非真实底盘）' : '车端执行完成'
  } else if (status === 'PUBLISHED_UNCONFIRMED') {
    outcome = 'timeout'
    outcomeLabel = '超时：未收到车端回执，执行结果未确认'
  } else if (status === 'EXPIRED') {
    outcome = 'failed'
    outcomeLabel = '命令过期未发布'
  } else if (failed) {
    outcome = 'failed'
    outcomeLabel = '失败'
  } else if (status === 'QUEUED' || status === 'CREATED') {
    outcomeLabel = '已排队，等待发布'
  } else if (status === 'PUBLISHED') {
    outcomeLabel = '已发布到 Broker，等待车端回执'
  } else if (software && (status === 'ACK_ACCEPTED' || status === 'EXECUTING')) {
    outcomeLabel = '车端已接收，等待 ROS 软件反馈'
  }
  const error =
    outcome === 'failed' || outcome === 'timeout'
      ? [
          ack?.code && ack.code !== 'OK' ? ack.code : row.ack_reason,
          lastFeedback?.state === 'FAILED' || lastFeedback?.state === 'REJECTED'
            ? lastFeedback.message || lastFeedback.reason_code
            : ack?.message,
        ]
          .filter(Boolean)
          .join('：')
      : ''
  return {
    queued: 'done',
    published: published ? 'done' : status === 'EXPIRED' ? 'failed' : 'pending',
    vehicleAck: vehicleAck
      ? ack?.success
        ? 'done'
        : 'failed'
      : status === 'PUBLISHED_UNCONFIRMED'
        ? 'failed'
        : published && !failed
          ? 'pending'
          : 'idle',
    software: !software
      ? 'na'
      : lastFeedback?.state === 'COMPLETED'
        ? 'done'
        : lastFeedback && ['FAILED', 'REJECTED'].includes(lastFeedback.state)
          ? 'failed'
          : vehicleAck && ack?.success
            ? 'pending'
            : 'idle',
    outcome,
    outcomeLabel,
    error,
  }
}

export function durationLabel(row: AgentCommand): string {
  const feedback = row.ack_payload_json?.software_feedback ?? []
  const last = feedback[feedback.length - 1]
  const ms = last?.duration_ms ?? row.ack_payload_json?.duration_ms
  return typeof ms === 'number' && Number.isFinite(ms) ? `${ms} ms` : '--'
}
