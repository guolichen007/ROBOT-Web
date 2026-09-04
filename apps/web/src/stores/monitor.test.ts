import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('@/lib/api', () => ({
  api: {
    get: vi.fn(),
    post: vi.fn(),
  },
}))

import { api } from '@/lib/api'
import { useMonitorStore } from './monitor'

class FakeWebSocket {
  static instances: FakeWebSocket[] = []
  onopen: (() => void) | null = null
  onmessage: ((_event: { data: string }) => void) | null = null
  onerror: (() => void) | null = null
  onclose: (() => void) | null = null
  constructor() {
    FakeWebSocket.instances.push(this)
  }
  close(): void {
    this.onclose = null
  }
}

describe('monitor store lifecycle', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
    FakeWebSocket.instances = []
    vi.stubGlobal('WebSocket', FakeWebSocket)
    vi.mocked(api.get).mockResolvedValue({
      data: {
        snapshot_watermark: '0-0',
        site: null,
        map: null,
        map_version: null,
        parking_slots: [],
        inspection_points: [],
        extinguish_points: [],
        trajectories: [],
        robots: [],
        alarms: [],
        tasks: [],
        streams: [],
        navigation_presets: [],
        operation_contexts: {},
      },
    } as never)
    vi.mocked(api.post).mockResolvedValue({ data: { ticket: 'ticket' } } as never)
  })

  it('start() is idempotent: concurrent calls share one snapshot load', async () => {
    const store = useMonitorStore()
    await Promise.all([store.start(), store.start()])
    expect(api.get).toHaveBeenCalledTimes(1)
  })

  it('disconnect() clears the connected flag', async () => {
    const store = useMonitorStore()
    await store.start()
    FakeWebSocket.instances[0]?.onopen?.()
    expect(store.connected).toBe(true)
    store.disconnect()
    expect(store.connected).toBe(false)
  })
})
