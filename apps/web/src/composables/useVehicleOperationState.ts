import { computed, type Ref } from 'vue'
import type { RobotState, StopOperation, Task } from '@/types'

export type VehicleOperationState =
  | 'IDLE'
  | 'PATROL_STARTING'
  | 'PATROLLING'
  | 'STOPPING'
  | 'STOPPED_RESUMABLE'
  | 'RETURN_STARTING'
  | 'RETURNING'
  | 'ESTOPPING'
  | 'ESTOPPED'
  | 'RESETTING'
  | 'ERROR'

export function useVehicleOperationState(input: {
  robot: Ref<RobotState | undefined>
  activeTask: Ref<Task | undefined>
  stopOperation: Ref<StopOperation | null>
  requestBusy: Ref<string>
  resumeTaskId: Ref<string | null>
}) {
  const state = computed<VehicleOperationState>(() => {
    const robot = input.robot.value
    if (robot?.estop_active) return 'ESTOPPED'

    const stop = input.stopOperation.value
    if (stop) {
      const terminal = ['VEHICLE_STATIONARY_CONFIRMED', 'PARTIAL_UNCONFIRMED', 'UNCONFIRMED', 'FAILED'].includes(
        stop.state || '',
      )
      if (!terminal) return 'STOPPING'
      return 'STOPPED_RESUMABLE'
    }

    const task = input.activeTask.value
    if (input.requestBusy.value === 'patrol') return 'PATROL_STARTING'
    if (input.requestBusy.value === 'home') return 'RETURN_STARTING'
    if (input.requestBusy.value === 'estop' || input.requestBusy.value === 'reset-estop') return 'ESTOPPING'

    if (task) {
      if (task.type === 'RETURN_DOCK') return 'RETURNING'
      if (task.type === 'PATROL') return 'PATROLLING'
      if (task.type === 'EXTINGUISH') return 'PATROLLING'
    }

    if (input.resumeTaskId.value) return 'STOPPED_RESUMABLE'
    return 'IDLE'
  })

  const atWaitingArea = computed(() => {
    const robot = input.robot.value
    if (!robot || robot.x == null || robot.y == null) return false
    // REMOTE_WAITING_AREA on the demo map.
    return Math.hypot(robot.x - 1.2, robot.y - 1.2) < 0.6
  })

  return { state, atWaitingArea }
}
