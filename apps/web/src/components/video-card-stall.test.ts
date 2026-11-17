// G3-R1：生产 VideoCard 首帧门控 + 画面停滞看门狗 + 回调取消/旧会话隔离。
import { mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { nextTick } from 'vue'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import VideoCard from './VideoCard.vue'
import { api } from '@/lib/api'

vi.mock('@/lib/api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/api')>()),
  api: { get: vi.fn(), post: vi.fn() },
}))

class FakePeer {
  static last: FakePeer
  connectionState = 'new'
  iceGatheringState = 'complete'
  localDescription = { sdp: 'v=0' }
  ontrack: ((event: unknown) => void) | null = null
  onconnectionstatechange: (() => void) | null = null
  constructor() {
    FakePeer.last = this
  }
  addTransceiver() {}
  async createOffer() {
    return { type: 'offer', sdp: 'v=0' }
  }
  async setLocalDescription() {}
  async setRemoteDescription() {}
  addEventListener() {}
  removeEventListener() {}
  close() {
    this.connectionState = 'closed'
  }
}

async function flush() {
  for (let i = 0; i < 6; i += 1) await Promise.resolve()
  await nextTick()
}

interface FrameApi {
  requestVideoFrameCallback: (cb: () => void) => number
  cancelVideoFrameCallback: (handle: number) => void
}

/** 可控的 rVFC：记录挂起回调、注册次数与取消。 */
function fakeFrames(el: Element) {
  const video = el as unknown as FrameApi
  const pending = new Map<number, () => void>()
  let next = 1
  const stats = { registered: 0, cancelled: 0 }
  video.requestVideoFrameCallback = (cb) => {
    stats.registered += 1
    const id = next++
    pending.set(id, cb)
    return id
  }
  video.cancelVideoFrameCallback = (id) => {
    if (pending.delete(id)) stats.cancelled += 1
  }
  return {
    stats,
    pending,
    /** 浏览器解码出一帧：触发全部挂起回调（真实 rVFC 语义：一次性，回调内自行续订） */
    frame() {
      const cbs = [...pending.values()]
      pending.clear()
      cbs.forEach((cb) => cb())
    },
  }
}

const stream = { stream_id: 's', state: 'ONLINE', camera_type: 'roof_rgb' }

function mountCard(props: Record<string, unknown> = {}) {
  return mount(VideoCard, {
    props: { title: 'RGB', stream: stream as never, ...props },
    global: { stubs: { 't-button': { template: '<button @click="$emit(\'click\')"><slot /></button>' } } },
  })
}

async function connectAndTrack(wrapper: ReturnType<typeof mountCard>) {
  await wrapper.find('button').trigger('click')
  await flush()
  const peer = FakePeer.last
  peer.connectionState = 'connected'
  peer.ontrack?.({ streams: [{}] })
  peer.onconnectionstatechange?.()
  await flush()
  return peer
}

describe('VideoCard 首帧门控 / 停滞看门狗 / 回调隔离', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.useFakeTimers()
    vi.stubGlobal('RTCPeerConnection', FakePeer)
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: true,
        status: 201,
        headers: new Headers({ Location: '/media/s/whep/session' }),
        text: async () => 'v=0',
      }),
    )
    vi.mocked(api.post).mockResolvedValue({ data: { ticket: 't', playback_url: '/media/s/whep' } })
  })
  afterEach(() => {
    vi.useRealTimers()
    vi.unstubAllGlobals()
  })

  it('connected + ontrack 但无解码帧 → 不显示「实时」；首帧到达后才 LIVE', async () => {
    const wrapper = mountCard()
    const frames = fakeFrames(wrapper.find('video').element)
    await connectAndTrack(wrapper)
    expect(wrapper.find('.video-live-tag').exists()).toBe(false)
    expect(wrapper.find('.video-placeholder').text()).toContain('等待首帧')
    expect(wrapper.find('video').attributes('data-first-frame')).toBe('no')
    frames.frame()
    await nextTick()
    expect(wrapper.find('.video-live-tag').exists()).toBe(true)
    expect(wrapper.find('video').attributes('data-first-frame')).toBe('yes')
  })

  it('connected 后 10s 仍无首帧 → ERROR（不停留在连接中、不伪装实时）', async () => {
    const wrapper = mountCard()
    fakeFrames(wrapper.find('video').element)
    await connectAndTrack(wrapper)
    vi.advanceTimersByTime(11_000)
    await nextTick()
    expect(wrapper.find('.video-live-tag').exists()).toBe(false)
    expect(wrapper.find('.video-placeholder').text()).toContain('未收到画面')
  })

  it('ontrack 与 connectionstatechange 重复调用 watchFrames：只保留一条 rVFC 链', async () => {
    const wrapper = mountCard()
    const frames = fakeFrames(wrapper.find('video').element)
    const peer = await connectAndTrack(wrapper)
    peer.onconnectionstatechange?.()
    peer.ontrack?.({ streams: [{}] })
    await flush()
    frames.frame()
    frames.frame()
    expect(frames.pending.size).toBe(1)
  })

  it('3s 无新帧 → 画面中断；新帧到达恢复', async () => {
    const wrapper = mountCard()
    const frames = fakeFrames(wrapper.find('video').element)
    await connectAndTrack(wrapper)
    frames.frame()
    vi.advanceTimersByTime(1500)
    frames.frame()
    vi.advanceTimersByTime(1000)
    await nextTick()
    expect(wrapper.find('[data-video-stalled]').exists()).toBe(false)
    vi.advanceTimersByTime(4000)
    await nextTick()
    expect(wrapper.find('.video-live-tag').exists()).toBe(false)
    expect(wrapper.find('[data-video-stalled]').text()).toContain('画面中断')
    frames.frame()
    await nextTick()
    expect(wrapper.find('[data-video-stalled]').exists()).toBe(false)
    expect(wrapper.find('.video-live-tag').exists()).toBe(true)
  })

  it('切换流（切车）后旧会话的帧回调不得改写新卡片状态；旧回调被取消', async () => {
    const wrapper = mountCard()
    const frames = fakeFrames(wrapper.find('video').element)
    await connectAndTrack(wrapper)
    frames.frame()
    await nextTick()
    expect(wrapper.find('.video-live-tag').exists()).toBe(true)
    const stale = [...frames.pending.values()]
    await wrapper.setProps({ stream: { stream_id: 'other', state: 'ONLINE', camera_type: 'roof_rgb' } })
    await flush()
    expect(frames.stats.cancelled).toBeGreaterThanOrEqual(1)
    stale.forEach((cb) => cb()) // 浏览器仍投递已排队的旧回调
    await nextTick()
    expect(wrapper.find('.video-live-tag').exists()).toBe(false)
    expect(wrapper.find('.video-placeholder').text()).toContain('视频未连接')
  })

  it('组件销毁：取消 rVFC 与定时器，WHEP 会话 DELETE', async () => {
    const wrapper = mountCard()
    const frames = fakeFrames(wrapper.find('video').element)
    await connectAndTrack(wrapper)
    frames.frame()
    wrapper.unmount()
    expect(frames.stats.cancelled).toBeGreaterThanOrEqual(1)
    expect(frames.pending.size).toBe(0)
    const calls = vi.mocked(fetch).mock.calls.map((c) => (c[1] as RequestInit | undefined)?.method)
    expect(calls).toContain('DELETE')
    expect(vi.getTimerCount()).toBe(0)
  })
})
