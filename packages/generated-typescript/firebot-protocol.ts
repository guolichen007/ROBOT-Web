export interface VehicleEnvelope {
  schema_version: '1.2'
  message_id: string
  type: string
  vehicle_id: string
  boot_id: string
  timestamp: string
  seq: number
}

export type CommandName =
  | 'manual_control'
  | 'stop_motion'
  | 'emergency_stop'
  | 'reset_estop'
  | 'return_dock'
  | 'patrol'
  | 'extinguish'
  | 'cancel_task'

export interface CommandMessage {
  schema_version: '1.2'
  message_id: string
  type: 'command'
  vehicle_id: string
  target_boot_id: string | null
  command_id: string
  correlation_id: string
  task_id?: string | null
  lease_id?: string | null
  control_session_id?: string | null
  seq?: number
  issued_at: string
  expires_at: string
  ttl_ms: number
  priority: number
  source: 'WEB'
  operator_id: string
  cmd: CommandName
  params: Record<string, unknown>
}

export interface CommandAck extends VehicleEnvelope {
  type: 'command_ack'
  command_id: string
  task_id?: string | null
  status: 'accepted' | 'rejected' | 'unsupported'
  reason_code: string | null
  reason?: string | null
}

export type RobotOnlineState = 'ONLINE' | 'STALE' | 'OFFLINE'
export type CommandLifecycle =
  | 'CREATED'
  | 'VALIDATED'
  | 'QUEUED'
  | 'PUBLISHED'
  | 'ACK_ACCEPTED'
  | 'EXECUTING'
  | 'SUCCEEDED'
  | 'VALIDATION_REJECTED'
  | 'ACK_REJECTED'
  | 'ACK_UNSUPPORTED'
  | 'PUBLISHED_UNCONFIRMED'
  | 'EXPIRED'
  | 'FAILED'
  | 'CANCELLED'
