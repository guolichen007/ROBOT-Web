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
  const published =
    Boolean(row.published_at) || !['CREATED', 'QUEUED', 'EXPIRED', 'GATE_REJECTED'].includes(status)
  const failed = ['ACK_REJECTED', 'ACK_UNSUPPORTED', 'FAILED', 'EXPIRED', 'GATE_REJECTED'].includes(status)
  let outcome: CommandStages['outcome'] = 'running'
  let outcomeLabel = '执行中'
  if (status === 'CANCELLED' && software) {
    outcome = 'succeeded'
    outcomeLabel = '软件巡检已停止（STOPPED，SOFTWARE_TEST）'
  } else if (status === 'SUCCEEDED') {
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
      : lastFeedback?.state === 'COMPLETED' || lastFeedback?.state === 'STOPPED'
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

// 门禁原因码 → 操作员可读说明（只读呈现；门禁本身在服务端，不在前端放宽）
const REASON_TEXT: Record<string, string> = {
  VEHICLE_AGENT_DOWNLINK_DISABLED: '下行已关闭（只读，可查看历史）',
  VEHICLE_NOT_IN_DOWNLINK_ALLOWLIST: '该车不在下行白名单',
  ROBOT_OFFLINE: '车辆离线',
  VEHICLE_AGENT_ADAPTER_UNAVAILABLE: '专属 Adapter 不可用',
  ROUTING_CONFIG_MISMATCH: '路由配置不一致',
  INTEGRATION_PROFILE_MISSING: '集成档案缺失',
  ROBOT_ESTOP_ACTIVE: '急停已触发',
  EXECUTION_PROFILE_DENIED: '执行档不允许',
  AGENT_PROTOCOL_MISMATCH: '未绑定 vehicle_agent v1',
}

export function reasonText(code: string | null | undefined): string {
  if (!code) return ''
  return REASON_TEXT[code] ?? code
}

export const SOFTWARE_PATROL_MAX_ROUNDS = 20

/** 软件巡检 rounds：整数 1..20（合同上限 1000，Web 受限更小）；非法值回落到 1。 */
export function clampRounds(value: unknown): number {
  const n = typeof value === 'number' ? value : Number.parseInt(String(value ?? ''), 10)
  if (!Number.isFinite(n)) return 1
  return Math.min(SOFTWARE_PATROL_MAX_ROUNDS, Math.max(1, Math.trunc(n)))
}

/** 失败退避：2s 起指数增长，封顶 30s（API 重建 502 期间不每秒打满请求）。 */
export function pollBackoff(failures: number): number {
  return Math.min(30_000, 2_000 * 2 ** Math.max(0, Math.min(failures, 4)))
}
