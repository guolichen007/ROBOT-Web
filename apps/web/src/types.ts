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
  online_state?: OnlineState
  x?: number
  y?: number
  theta?: number
  linear?: number
  angular?: number
  battery?: number
  mode?: string
  estop_active?: boolean
  map_version?: string
  smoke?: number
  bottom_ir?: number
  top_ir?: number
  server_received_at?: string
  supported_commands?: string[]
  sensors?: string[]
  media?: string[]
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
  event_code: string
  parking_slot_id?: string
  state: string
  severity: string
  fire_type: string
  occurrence_count: number
  detection_method: string
  last_seen_at: string
}

export interface Task {
  id: string
  task_code: string
  type: string
  status: string
  phase: string
  progress: number
  target_parking_slot_id?: string
  created_at: string
}

export interface StreamInfo {
  id: string
  camera_type: string
  state: 'DISABLED' | 'OFFLINE' | 'CONNECTING' | 'LIVE' | 'ERROR'
  playback_url?: string
  codec?: string
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
}

export interface RealtimeEvent {
  stream_id: string
  event_type: string
  data: Record<string, any>
}
