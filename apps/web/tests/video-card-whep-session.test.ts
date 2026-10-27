import { flushPromises, mount } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { nextTick, ref } from 'vue'
import type { StreamInfo } from '@/types'

// ---- hoisted mocks -------------------------------------------------------
const { ticketPost, fetchMock, createdPeers, fakeConfig } = vi.hoisted(() => ({
  ticketPost: vi.fn(),
  fetchMock: vi.fn(),
  createdPeers: [] as Array<Record<string, unknown>>,
  fakeConfig: {
    iceGathering: 'complete' as 'complete' | 'gathering',
    whepFail: false,
  },
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
  iceGatheringState = fakeConfig.iceGathering
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
  fail(): void {
    this.connectionState = 'disconnected'
    this.onconnectionstatechange?.()
  }
  deliverTrack(): void {
    this.ontrack?.({ streams: [{ id: 'fake-media-stream' }] })
    this.connectionState = 'connected'
    this.onconnectionstatechange?.()
  }
  addEventListener(): void {
    /* waitIce 的 icegatheringstatechange 监听在 fake 中无需真正触发（gathering 模式走 3s 兜底） */
  }
  removeEventListener(): void {
    /* no-op */
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
    fakeConfig.iceGathering = 'complete'
    fakeConfig.whepFail = false
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
      if (fakeConfig.whepFail) throw new Error('WHEP unavailable')
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

describe('VideoCard bounded auto-reconnect（R2-15）', () => {
  beforeEach(() => {
    ticketPost.mockReset()
    fetchMock.mockReset()
    createdPeers.length = 0
    fakeConfig.iceGathering = 'complete'
    fakeConfig.whepFail = false
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
      if (fakeConfig.whepFail) throw new Error('WHEP unavailable')
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
    vi.useRealTimers()
  })

  async function establishLive(wrapper: Awaited<ReturnType<typeof mountCard>>) {
    await wrapper.find('button').trigger('click')
    await flushPromises()
    const peer = createdPeers[createdPeers.length - 1] as unknown as FakePeerConnection
    peer.deliverTrack()
    await flushPromises()
    return peer
  }

  it('未建立 LIVE 会话时的断连不触发自动重连（用户未看过视频不得拉流）', async () => {
    vi.useFakeTimers()
    const wrapper = await mountCard(makeStream())
    await wrapper.find('button').trigger('click')
    await flushPromises()
    const peer = createdPeers[0] as unknown as FakePeerConnection
    peer.fail() // 从未 LIVE（srcObject 未就绪）
    await vi.advanceTimersByTimeAsync(10_000)
    await flushPromises()
    expect(whepPosts()).toBe(1)
    expect(wrapper.text()).toContain('视频连接异常')
  })

  it('短暂断开后有界自动重连成功并恢复 LIVE', async () => {
    vi.useFakeTimers()
    const wrapper = await mountCard(makeStream())
    const peer = await establishLive(wrapper)
    expect(wrapper.find('.video-live-tag').exists()).toBe(true)
    peer.fail()
    await vi.advanceTimersByTimeAsync(1_100) // 第一次退避 1s
    await flushPromises()
    expect(whepPosts()).toBe(2)
    expect(ticketPost).toHaveBeenCalledTimes(2)
    const peer2 = createdPeers[createdPeers.length - 1] as unknown as FakePeerConnection
    peer2.deliverTrack()
    await flushPromises()
    expect(wrapper.find('.video-live-tag').exists()).toBe(true)
  })

  it('重连耗尽（3 次）后进入 ERROR 且不再自动重试', async () => {
    vi.useFakeTimers()
    const wrapper = await mountCard(makeStream())
    const peer = await establishLive(wrapper)
    fakeConfig.whepFail = true
    peer.fail()
    // 退避 1s / 2s / 4s 共 3 次重试
    await vi.advanceTimersByTimeAsync(1_100)
    await flushPromises()
    await vi.advanceTimersByTimeAsync(2_100)
    await flushPromises()
    await vi.advanceTimersByTimeAsync(4_100)
    await flushPromises()
    expect(whepPosts()).toBe(4) // 1 初始 + 3 重试
    expect(wrapper.text()).toContain('自动重连失败')
    await vi.advanceTimersByTimeAsync(60_000)
    await flushPromises()
    expect(whepPosts()).toBe(4)
  })

  it('重连等待期间 active=false 取消挂起重连', async () => {
    vi.useFakeTimers()
    const wrapper = await mountCard(makeStream())
    const peer = await establishLive(wrapper)
    peer.fail()
    await wrapper.setProps({ active: false })
    await flushPromises()
    await vi.advanceTimersByTimeAsync(10_000)
    await flushPromises()
    expect(whepPosts()).toBe(1)
  })

  it('重连等待期间 stream_id 变化取消挂起重连（不自动连接新流）', async () => {
    vi.useFakeTimers()
    const wrapper = await mountCard(makeStream())
    const peer = await establishLive(wrapper)
    peer.fail()
    await wrapper.setProps({
      stream: makeStream({ stream_id: 'firebot-vehicle-01-roof_thermal', camera_type: 'roof_thermal' }),
    })
    await flushPromises()
    await vi.advanceTimersByTimeAsync(10_000)
    await flushPromises()
    expect(whepPosts()).toBe(1)
    expect(deleteCalls()).toBe(1) // 旧会话被销毁
  })

  it('unmount 清理挂起重连定时器', async () => {
    vi.useFakeTimers()
    const wrapper = await mountCard(makeStream())
    const peer = await establishLive(wrapper)
    peer.fail()
    wrapper.unmount()
    await vi.advanceTimersByTimeAsync(10_000)
    await flushPromises()
    expect(whepPosts()).toBe(1)
  })

  it('ICE gathering 3s 兜底后继续 WHEP（定时器清理不泄漏）', async () => {
    vi.useFakeTimers()
    fakeConfig.iceGathering = 'gathering'
    const wrapper = await mountCard(makeStream())
    await wrapper.find('button').trigger('click')
    await flushPromises()
    expect(whepPosts()).toBe(0) // waitIce 尚未超时
    await vi.advanceTimersByTimeAsync(3_100)
    await flushPromises()
    expect(whepPosts()).toBe(1)
  })
})
