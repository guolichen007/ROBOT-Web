export interface Envelope {
  schema_version: '1.1'
  message_id: string
  type: string
  vehicle_id: string
  boot_id: string
  timestamp: string
  seq: number
}

export type CommandName = 'manual_control' | 'stop_motion' | 'emergency_stop' |
  'reset_estop' | 'return_dock' | 'patrol' | 'extinguish' | 'cancel_task'

export interface CommandMessage extends Envelope {
  type: 'command'
  command_id: string
  correlation_id: string
  task_id?: string | null
  lease_id?: string | null
  control_session_id?: string | null
  issued_at: string
  expires_at: string
  ttl_ms: number
  priority: number
  source: 'WEB'
  operator_id: string
  cmd: CommandName
  params: Record<string, unknown>
}

export type RobotOnlineState = 'ONLINE' | 'STALE' | 'OFFLINE'
export type CommandLifecycle = 'CREATED' | 'VALIDATED' | 'QUEUED' | 'PUBLISHED' |
  'ACK_ACCEPTED' | 'EXECUTING' | 'SUCCEEDED' | 'VALIDATION_REJECTED' |
  'ACK_REJECTED' | 'ACK_UNSUPPORTED' | 'PUBLISHED_UNCONFIRMED' | 'EXPIRED' |
  'FAILED' | 'CANCELLED'
