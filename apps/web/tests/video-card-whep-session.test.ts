import { flushPromises, mount } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { nextTick, ref } from 'vue'
import type { StreamInfo } from '@/types'

// ---- hoisted mocks -------------------------------------------------------
const { ticketPost, fetchMock, createdPeers } = vi.hoisted(() => ({
  ticketPost: vi.fn(),
  fetchMock: vi.fn(),
  createdPeers: [] as Array<Record<string, unknown>>,
}))

vi.mock('@/lib/api', () => ({
  api: { post: (...args: unknown[]) => ticketPost(...args) },
  errorMessage: (error: unknown) => String(error),
}))

vi.mock('@/composables/useSystemClock', () => ({
  useSystemClock: () => ({ now: ref(new Date('2026-10-13T00:00:00Z')) }),
}))

import VideoCard from '@/components/VideoCard.vue'

// ---- WebRTC / fetch fakes -----------------------------------------------
class FakePeerConnection {
  connectionState = 'new'
  iceGatheringState = 'complete'
  ontrack: ((event: unknown) => void) | null = null
  onconnectionstatechange: (() => void) | null = null
  constructor() {
    createdPeers.push(this as unknown as Record<string, unknown>)
  }
  addTransceiver(): void {
    /* recvonly transceiver is a no-op in the fake */
  }
  async createOffer(): Promise<{ type: string; sdp: string }> {
    return { type: 'offer', sdp: 'local-sdp' }
  }
  async setLocalDescription(): Promise<void> {
    /* no-op */
  }
  async setRemoteDescription(): Promise<void> {
    this.connectionState = 'connected'
    this.onconnectionstatechange?.()
  }
  close(): void {
    this.connectionState = 'closed'
  }
}

function whepPosts(): number {
  return fetchMock.mock.calls.filter(
    ([url, init]) => (init as RequestInit | undefined)?.method === 'POST' && String(url).includes('/whep'),
  ).length
}

function deleteCalls(): number {
  return fetchMock.mock.calls.filter(([, init]) => (init as RequestInit | undefined)?.method === 'DELETE')
    .length
}

function makeStream(overrides: Partial<StreamInfo> = {}): StreamInfo {
  return {
    id: 'stream-1',
    stream_id: 'firebot-vehicle-01-roof_rgb',
    robot_id: 'robot-1',
    camera_type: 'roof_rgb',
    state: 'OFFLINE',
    playback_url: '/media/firebot-vehicle-01-roof_rgb/whep',
    codec: 'H264',
    ...overrides,
  }
}

async function mountCard(stream: StreamInfo, active = true) {
  const wrapper = mount(VideoCard, {
    props: { stream, title: '车顶实时相机', active },
    global: {
      stubs: {
        't-button': { template: '<button class="connect-btn"><slot /></button>' },
        FullscreenIcon: true,
        VideoCameraIcon: true,
      },
    },
  })
  await nextTick()
  return wrapper
}

describe('VideoCard WHEP session stability', () => {
  beforeEach(() => {
    ticketPost.mockReset()
    fetchMock.mockReset()
    createdPeers.length = 0
    ticketPost.mockResolvedValue({
      data: {
        ticket: 'test-ticket',
        playback_url: '/media/firebot-vehicle-01-roof_rgb/whep',
      },
    })
    fetchMock.mockImplementation(async (url: string, init?: RequestInit) => {
      if (init?.method === 'DELETE') {
        return { ok: true, status: 200, headers: { get: () => null }, text: async () => '' }
      }
      if (String(url).includes('/media/tickets')) {
        return { ok: true, status: 200, headers: { get: () => null }, text: async () => '{}' }
      }
      return {
        ok: true,
        status: 201,
        headers: { get: () => '/media/firebot-vehicle-01-roof_rgb/whep/session-1' },
        text: async () => 'answer-sdp',
      }
    })
    vi.stubGlobal('RTCPeerConnection', FakePeerConnection)
    vi.stubGlobal('fetch', fetchMock)
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('CASE 1: one click → 1 ticket + 1 WHEP; snapshot reload (same stream_id/state) does not reconnect', async () => {
    const wrapper = await mountCard(makeStream())
    await wrapper.find('button').trigger('click')
    await flushPromises()

    expect(ticketPost).toHaveBeenCalledTimes(1)
    expect(whepPosts()).toBe(1)
    expect(deleteCalls()).toBe(0)

    // 模拟 snapshot reload：同 stream_id、同 state，但对象引用是新的
    await wrapper.setProps({ stream: makeStream() })
    await flushPromises()
    await nextTick()

    expect(whepPosts()).toBe(1)
    expect(ticketPost).toHaveBeenCalledTimes(1)
    expect(deleteCalls()).toBe(0)
  })

  it('CASE 2: parent re-render does not trigger another WHEP', async () => {
    const wrapper = await mountCard(makeStream())
    await wrapper.find('button').trigger('click')
    await flushPromises()
    expect(whepPosts()).toBe(1)

    await wrapper.setProps({ title: '车顶实时相机（同一卡片）' })
    await flushPromises()
    await nextTick()

    expect(whepPosts()).toBe(1)
    expect(deleteCalls()).toBe(0)
  })

  it('CASE 3: real stream_id change DELETEs old session but does NOT auto-connect the new one', async () => {
    const wrapper = await mountCard(makeStream())
    await wrapper.find('button').trigger('click')
    await flushPromises()
    expect(whepPosts()).toBe(1)

    await wrapper.setProps({
      stream: makeStream({
        stream_id: 'firebot-vehicle-01-roof_thermal',
        camera_type: 'roof_thermal',
        playback_url: '/media/firebot-vehicle-01-roof_thermal/whep',
      }),
    })
    await flushPromises()
    await nextTick()

    expect(deleteCalls()).toBe(1)
    expect(whepPosts()).toBe(1)
  })

  it('CASE 4: active=false DELETEs the live session', async () => {
    const wrapper = await mountCard(makeStream(), true)
    await wrapper.find('button').trigger('click')
    await flushPromises()
    expect(whepPosts()).toBe(1)

    await wrapper.setProps({ active: false })
    await flushPromises()
    await nextTick()

    expect(deleteCalls()).toBe(1)
    expect(whepPosts()).toBe(1)
  })

  it('CASE 5: stream state=DISABLED DELETEs the live session', async () => {
    const wrapper = await mountCard(makeStream())
    await wrapper.find('button').trigger('click')
    await flushPromises()
    expect(whepPosts()).toBe(1)

    await wrapper.setProps({ stream: makeStream({ state: 'DISABLED' }) })
    await flushPromises()
    await nextTick()

    expect(deleteCalls()).toBe(1)
    expect(whepPosts()).toBe(1)
  })

  it('CASE 6: server-side state LIVE (media source online) does not make the player LIVE by itself', async () => {
    const wrapper = await mountCard(makeStream())
    expect(wrapper.text()).toContain('视频未连接')

    await wrapper.setProps({ stream: makeStream({ state: 'LIVE' }) })
    await flushPromises()
    await nextTick()

    // 媒体源 LIVE 不等于播放器 LIVE：未点击连接前，播放器仍为空闲态
    expect(wrapper.text()).toContain('视频未连接')
    expect(whepPosts()).toBe(0)
    expect(deleteCalls()).toBe(0)
  })

  it('guards rapid repeated clicks while the ticket request is pending', async () => {
    let resolveTicket!: (value: unknown) => void
    ticketPost.mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveTicket = resolve
        }),
    )
    const wrapper = await mountCard(makeStream())
    const button = wrapper.find('button')
    await Promise.all([button.trigger('click'), button.trigger('click')])
    expect(ticketPost).toHaveBeenCalledTimes(1)
    resolveTicket({ data: { ticket: 'test-ticket', playback_url: '/media/test/whep' } })
    await flushPromises()
    expect(whepPosts()).toBe(1)
  })

  it('cannot create a peer after becoming inactive during ticket acquisition', async () => {
    let resolveTicket!: (value: unknown) => void
    ticketPost.mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveTicket = resolve
        }),
    )
    const wrapper = await mountCard(makeStream())
    await wrapper.find('button').trigger('click')
    await wrapper.setProps({ active: false })
    resolveTicket({ data: { ticket: 'test-ticket', playback_url: '/media/test/whep' } })
    await flushPromises()
    expect(createdPeers).toHaveLength(0)
    expect(whepPosts()).toBe(0)
  })

  it('OFFLINE/CONNECTING/LIVE updates preserve an existing peer and session', async () => {
    const wrapper = await mountCard(makeStream())
    await wrapper.find('button').trigger('click')
    await flushPromises()
    for (const state of ['LIVE', 'CONNECTING', 'OFFLINE'] as const) {
      await wrapper.setProps({ stream: makeStream({ state }) })
      await flushPromises()
    }
    expect(createdPeers).toHaveLength(1)
    expect(whepPosts()).toBe(1)
    expect(deleteCalls()).toBe(0)
  })
})
