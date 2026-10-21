import type { RobotState } from '@/types'

export const agentActions = [
  ['start', '启动'],
  ['close', '关闭'],
  ['patrol', '开始巡逻'],
  ['stop_patrol', '停止巡逻'],
  ['resume_patrol', '恢复巡逻'],
  ['homing', '返航'],
  ['navigate', '前往目标'],
  ['stop_navigate', '停止目标'],
] as const
export type AgentAction = (typeof agentActions)[number][0]

export function agentTaskLabel(state?: string): string {
  return (
    {
      idle: 'IDLE / 空闲',
      patrol: 'PATROL / 巡逻中',
      homing: 'HOMING / 返航中',
      spot: 'NAVIGATING / 前往目标',
    }[state || ''] || '--'
  )
}

export function metric(value: unknown, unit = ''): string {
  return typeof value === 'number' && Number.isFinite(value) ? `${Number(value.toFixed(1))}${unit}` : '--'
}

export function agentFreshness(robot: RobotState | undefined, connected: boolean, now: number) {
  const online = connected && robot?.online_state === 'ONLINE'
  const received = robot?.agent_state?.telemetry_received_at || robot?.server_received_at
  const age = received ? now - Date.parse(received) : Infinity
  return {
    online,
    stale:
      !online ||
      (robot?.agent_protocol === 'VEHICLE_AGENT_V1' && robot.agent_state?.telemetry_online !== true) ||
      !Number.isFinite(age) ||
      age < 0 ||
      age > (robot?.integration?.stale_seconds ?? 3) * 1000,
  }
}

export function canSendAgent(
  robot: RobotState | undefined,
  action: AgentAction,
  connected: boolean,
): boolean {
  return Boolean(
    connected &&
      robot?.agent_protocol === 'VEHICLE_AGENT_V1' &&
      robot.online_state === 'ONLINE' &&
      robot.vehicle_agent_ready?.[action] === true &&
      (action !== 'resume_patrol' || robot.agent_state?.status?.task?.patrol_paused === true),
  )
}
