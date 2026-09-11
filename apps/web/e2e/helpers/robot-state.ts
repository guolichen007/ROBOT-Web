import { expect, type APIRequestContext } from '@playwright/test'
import { ensurePasswordReady } from './auth'

// 统一 robot-state 语义：所有 active-task 判断必须走 robot 内部 UUID（robot.id），
// 绝不能把 vehicle_id("R001") 当 task.robot_id 用。task.robot_id 是 Robot internal UUID。

export const ACTIVE_TASK_STATUSES = ['CREATED', 'QUEUED', 'ACCEPTED', 'EXECUTING'] as const

export interface RobotRef {
  id: string
  vehicle_id: string
}

export interface TaskRef {
  id: string
  robot_id: string
  status: string
  type: string
}

interface MonitorSnapshot {
  robots: Array<{ id?: string; vehicle_id: string }>
  tasks: TaskRef[]
  streams: Array<{ stream_id: string; robot_id: string }>
  trajectories: Array<{ id: string }>
  parking_slots: Array<{ id: string }>
}

async function authHeaders(request: APIRequestContext): Promise<{ Authorization: string }> {
  const accessToken = await ensurePasswordReady(request)
  return { Authorization: `Bearer ${accessToken}` }
}

export async function snapshot(request: APIRequestContext): Promise<MonitorSnapshot> {
  const headers = await authHeaders(request)
  const response = await request.get('/api/v1/monitor/snapshot', { headers })
  expect(response.ok()).toBeTruthy()
  return (await response.json()) as MonitorSnapshot
}

export async function resolveRobot(request: APIRequestContext, vehicleId: string): Promise<RobotRef> {
  const snap = await snapshot(request)
  const robot = snap.robots.find((r) => r.vehicle_id === vehicleId)
  expect(robot, `${vehicleId} not found in /monitor/snapshot`).toBeTruthy()
  expect(robot!.id, `${vehicleId} has no internal robot id`).toBeTruthy()
  return { id: robot!.id as string, vehicle_id: robot!.vehicle_id }
}

export async function getActiveTasksForRobot(
  request: APIRequestContext,
  vehicleId: string,
): Promise<TaskRef[]> {
  const headers = await authHeaders(request)
  const robot = await resolveRobot(request, vehicleId)
  const response = await request.get('/api/v1/tasks?limit=1000', { headers })
  expect(response.ok()).toBeTruthy()
  const tasks = (await response.json()) as TaskRef[]
  // 关键：按 robot internal UUID 过滤，不是 vehicle_id。
  return tasks.filter(
    (task) => task.robot_id === robot.id && (ACTIVE_TASK_STATUSES as readonly string[]).includes(task.status),
  )
}

export async function ensureRobotIdle(
  request: APIRequestContext,
  vehicleId: string,
  timeout = 20_000,
): Promise<void> {
  await expect
    .poll(async () => (await getActiveTasksForRobot(request, vehicleId)).length, {
      timeout,
      intervals: [500, 1_000],
    })
    .toBe(0)
}

export async function forceReleaseManualLease(request: APIRequestContext, vehicleId: string): Promise<void> {
  const headers = await authHeaders(request)
  const response = await request.post(`/api/v1/robots/${vehicleId}/manual-lease/force-release`, {
    headers,
  })
  expect(response.ok()).toBeTruthy()
}

export async function cleanupRobot(request: APIRequestContext, vehicleId: string): Promise<void> {
  // 禁止无脑 STOP：生产 stop-operation 即使没有 active task 也会发独立 stop_motion safety command。
  // 只有检测到 active task 才发 stop-operation。
  const active = await getActiveTasksForRobot(request, vehicleId)
  if (active.length === 0) return
  const headers = await authHeaders(request)
  const stopResponse = await request.post(`/api/v1/robots/${vehicleId}/stop-operation`, {
    headers: { ...headers, 'Idempotency-Key': crypto.randomUUID() },
  })
  expect(stopResponse.ok(), `cleanupRobot stop-operation failed`).toBeTruthy()
  await expect
    .poll(async () => (await getActiveTasksForRobot(request, vehicleId)).length, {
      timeout: 20_000,
      intervals: [500, 1_000],
    })
    .toBe(0)
}
