export type OnlineState = 'ONLINE' | 'STALE' | 'OFFLINE'

export interface UserProfile {
  id: string
  username: string
  display_name: string
  must_change_password: boolean
  roles: string[]
  permissions: string[]
}

export interface RobotState {
  id?: string
  vehicle_id: string
  name?: string
  model?: string
  enabled?: boolean
  online_state?: OnlineState
  last_seen_at?: string | null
  current_map_id?: string | null
  current_map_version?: string | null
  x?: number
  y?: number
  theta?: number
  linear?: number
  angular?: number
  linear_x?: number
  linear_y?: number
  angular_z?: number
  planar_speed?: number
  battery?: number | null
  mode?: string
  current_mode?: string
  estop_active?: boolean | null
  map_version?: string
  smoke?: number
  bottom_ir?: number
  top_ir?: number
  server_received_at?: string
  localization_status?: string
  supported_commands?: string[]
  sensors?: string[]
  media?: string[]
  control_enabled?: boolean
  monitor_ready?: boolean
  safety_command_ready?: Record<string, boolean>
  manual_control_ready?: boolean
  autonomous_task_ready?: Record<string, boolean>
  readiness_reasons?: string[]
  motion_profile?: MotionProfile | null
  control_disabled_reason?: string | null
  integration?: IntegrationProfile | null
  data_channels?: Record<string, DataChannel>
  sensor_profiles?: SensorProfile[]
}

export type DataSupportState = 'CONNECTED' | 'STALE' | 'NOT_CONNECTED' | 'ERROR' | 'UNSUPPORTED'

export interface DataChannel {
  channel: string
  support_state: DataSupportState
  quality: string
  source_kind: string
  last_received_at?: string | null
}

export interface IntegrationProfile {
  source_kind: 'CANONICAL_MQTT' | 'ROS_COMPAT' | 'MOCK'
  upstream_protocol?: string | null
  control_contract_verified: boolean
  ack_contract_verified: boolean
  map_contract_verified: boolean
  bidirectional_bridge_verified: boolean
  command_path_verified: boolean
  cmd_vel_arbitration_verified: boolean
  ros_control_mode?: number | null
  read_only_reason?: string | null
  forward_only: boolean
  reverse_precision_navigation: boolean
  stale_seconds?: number | null
  offline_seconds?: number | null
  reported_site_code?: string | null
  reported_map_code?: string | null
  reported_map_version?: string | null
  reported_map_checksum?: string | null
}

export interface MotionProfile {
  max_manual_forward_mps: number | null
  max_manual_reverse_mps: number | null
  max_manual_angular_radps: number | null
  manual_watchdog_verified: boolean
  reverse_allowed: boolean
  reverse_precision_verified: boolean
}

export interface SensorProfile {
  channel: string
  support_state: DataSupportState
  nominal_side: string
  sensor_mount_x_m: number
  sensor_mount_y_m: number
  sensor_mount_yaw_rad: number
  coverage_range_m: number
  coverage_fov_rad: number
}

export interface ParkingSlot {
  id: string
  code: string
  polygon_json: { points: Array<{ x: number; y: number }> } | Array<{ x: number; y: number }>
  center_pose_json: { x: number; y: number; theta: number }
  enabled: boolean
}

export interface MapPoint {
  id: string
  parking_slot_id?: string
  pose_json: { x: number; y: number; theta?: number }
}

export interface MapVersion {
  id: string
  version: string
  status: 'DRAFT' | 'PUBLISHED' | 'ARCHIVED'
  semantic_revision: number
  width_m: number
  height_m: number
  origin_x: number
  origin_y: number
  rotation_rad: number
  resolution_m_per_pixel: number
  frame_id: string
}

export interface Alarm {
  id: string
  robot_id: string
  event_code: string
  parking_slot_id?: string
  state: string
  severity: string
  fire_type: string
  occurrence_count: number
  detection_method: string
  last_seen_at: string
  first_seen_at?: string
  confidence?: number | null
  ack_at?: string | null
  confirmed_at?: string | null
  assigned_task_id?: string | null
  resolved_at?: string | null
  source_position_json?: Record<string, unknown>
  sensor_snapshot_json?: Record<string, unknown>
  media_snapshot_json?: Record<string, unknown>
  note?: string | null
}

export interface Task {
  id: string
  robot_id: string
  task_code: string
  type: string
  status: string
  phase: string
  progress: number
  target_parking_slot_id?: string
  created_at: string
  parameters_json?: Record<string, any>
}

export interface StreamInfo {
  stream_id: string
  id: string
  robot_id: string
  camera_type: string
  state: 'DISABLED' | 'OFFLINE' | 'CONNECTING' | 'LIVE' | 'ERROR'
  playback_url?: string
  codec?: string
}

export interface DetectionCoverage {
  state: DataSupportState
  polygon: Array<{ x: number; y: number }>
  covered_parking_slot_ids: string[]
  sensor_origin?: { x: number; y: number; yaw: number }
  reason?: string
  configuration?: SensorProfile
}

export interface NavigationPreset {
  id: string
  code: string
  name: string
  category: 'INSPECTION' | 'EXTINGUISH' | 'WAITING_AREA' | 'DOCK'
  pose_json: { x: number; y: number; theta: number }
  requires_reverse: boolean
  enabled: boolean
  parking_slot_id?: string | null
}

export interface AlarmTimelineItem {
  occurred_at: string
  source_type: 'ALARM' | 'TASK' | 'COMMAND' | 'OPERATION'
  state: string
  label: string
  task_id?: string
  command_id?: string
  correlation_id?: string
}

export interface StopOperation {
  id: string
  state: string
  stationary_frames: number
  failure_reason?: string | null
  motion_stop_state: string
  mission_cancel_state: string
}

export interface OperationContext {
  state: 'IDLE' | 'RUNNING' | 'PAUSED' | 'ESTOPPED'
  kind: 'PATROL' | 'RETURN' | null
  task_id: string | null
  patrol_plan_id: string | null
  last_completed_waypoint_index: number | null
  target_waypoint_index: number | null
  waypoint_total: number | null
  checkpoint_index: number | null
  checkpoint_total: number | null
  current_slot_code: string | null
  next_slot_code: string | null
  interrupted_reason: string | null
  can_continue: boolean
  can_return: boolean
}

export interface MonitorSnapshot {
  snapshot_watermark: string
  site: Record<string, any> | null
  map: Record<string, any> | null
  map_version: MapVersion | null
  parking_slots: ParkingSlot[]
  inspection_points: MapPoint[]
  extinguish_points: MapPoint[]
  trajectories: Array<{ id: string; code: string; path_json: Array<{ x: number; y: number }> }>
  robots: RobotState[]
  alarms: Alarm[]
  tasks: Task[]
  streams: StreamInfo[]
  navigation_presets?: NavigationPreset[]
  operation_context?: OperationContext | null
  operation_contexts?: Record<string, OperationContext>
}

export interface RealtimeEvent {
  stream_id: string
  event_type: string
  data: Record<string, any>
}
