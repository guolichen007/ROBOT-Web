import { mount } from '@vue/test-utils'
import { computed, defineComponent, nextTick, ref } from 'vue'
import { afterEach, describe, expect, it, vi } from 'vitest'
import SituationBanner from './SituationBanner.vue'

// 复现 MonitorView 的稳定 Teleport host 结构：
// Teleport 永远挂载，direct child 是稳定 div.monitor-situation-host，
// SituationBanner 的条件挂载发生在普通 div 下（不再让 Teleport 生命周期动态 mount/unmount）。
const Host = defineComponent({
  components: { SituationBanner },
  setup() {
    const state = ref('OFFLINE_UNKNOWN')
    const show = computed(() => state.value !== 'NORMAL')
    return { state, show }
  },
  template: `
    <Teleport to=".workspace-alert">
      <div class="monitor-situation-host">
        <SituationBanner v-if="show" :state="state" />
      </div>
    </Teleport>
  `,
})

afterEach(() => {
  document.body.innerHTML = ''
})

describe('SituationBanner teleport host stability', () => {
  it('keeps host stable across OFF → NORMAL → OFF without emitsOptions', async () => {
    const target = document.createElement('div')
    target.className = 'workspace-alert'
    document.body.appendChild(target)

    const appErrors: unknown[] = []
    const consoleErrors: string[] = []
    const spy = vi.spyOn(console, 'error').mockImplementation((...args: unknown[]) => {
      consoleErrors.push(args.map((a) => String(a)).join(' '))
    })

    const wrapper = mount(Host, {
      global: {
        config: {
          errorHandler(err: unknown) {
            appErrors.push(err)
          },
        },
      },
    })

    const host = () => target.querySelector('.monitor-situation-host')
    const banner = () => target.querySelector('.situation-banner')

    // OFFLINE_UNKNOWN：host 存在 + banner 可见
    expect(host()).toBeTruthy()
    expect(banner()).toBeTruthy()

    // → NORMAL：host 仍存在 + banner 消失
    ;(wrapper.vm as unknown as { state: string }).state = 'NORMAL'
    await nextTick()
    await nextTick()
    expect(host()).toBeTruthy()
    expect(banner()).toBeNull()

    // → 回 OFF：host 同一结构 + banner 恢复
    ;(wrapper.vm as unknown as { state: string }).state = 'OFFLINE_UNKNOWN'
    await nextTick()
    await nextTick()
    expect(host()).toBeTruthy()
    expect(banner()).toBeTruthy()

    const fatal = [...appErrors, ...consoleErrors].filter((e) =>
      /emitsOptions|Cannot read properties of null/.test(String(e)),
    )
    expect(fatal).toEqual([])
    spy.mockRestore()
  })
})
