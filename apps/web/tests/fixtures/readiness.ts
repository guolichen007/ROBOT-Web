import type { OperationReadiness } from '@/types'

/** A fresh, contract-shaped server OperationReadiness for unit tests (valid for 60 s). */
export function serverReadiness(
  vehicleId: string,
  allowed: Record<string, boolean>,
  opts: { bootId?: string | null; frozen?: string[]; validMs?: number; evaluatedAt?: number } = {},
): OperationReadiness {
  const at = opts.evaluatedAt ?? Date.now()
  const frozen = opts.frozen ?? []
  return {
    vehicle_id: vehicleId,
    environment: vehicleId.startsWith('firebot-sim-') ? 'SIM' : 'REAL',
    evaluated_at: new Date(at).toISOString(),
    valid_until: new Date(at + (opts.validMs ?? 60_000)).toISOString(),
    boot_id: opts.bootId === undefined ? null : opts.bootId,
    source: 'SERVER_OPERATION_READINESS',
    control_frozen: frozen.length > 0,
    freeze_scope: frozen.length ? 'ALL' : null,
    freeze_reasons: frozen,
    actions: Object.fromEntries(
      Object.entries(allowed).map(([k, v]) => [
        k,
        {
          allowed: v,
          reasons: v ? [] : frozen.length ? ['CONTROL_FROZEN'] : ['NOT_READY'],
          required_permission: 'robot.read',
          permitted_for_user: v,
        },
      ]),
    ),
  }
}

/** api.get mock implementation answering /operation-readiness from a per-vehicle table. */
export function readinessGet(table: Record<string, OperationReadiness | (() => OperationReadiness)>) {
  return (url: string) => {
    const m = /\/robots\/([^/]+)\/operation-readiness$/.exec(url)
    if (!m) return Promise.resolve({ data: null })
    const entry = table[m[1]]
    if (!entry) return Promise.reject(new Error('no readiness'))
    return Promise.resolve({ data: typeof entry === 'function' ? entry() : entry })
  }
}
