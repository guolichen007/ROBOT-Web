import { mount } from '@vue/test-utils'
import { defineComponent, nextTick } from 'vue'
import { afterEach, describe, expect, it, vi } from 'vitest'
import SituationBanner from './SituationBanner.vue'

// 最小复现：Teleport 内挂 SituationBanner，state 从 OFFLINE_UNKNOWN（渲染 section.warning）
// 切到 NORMAL（渲染空 fragment），验证不产生 Vue runtime "emitsOptions" TypeError。
// Teleport target（.workspace-alert）必须像 App.vue shell 一样预存在于真实 DOM。
const Host = defineComponent({
  components: { SituationBanner },
  props: { state: { type: String, required: true } },
  template: `
    <Teleport to=".workspace-alert">
      <SituationBanner :state="state" />
    </Teleport>
  `,
})

afterEach(() => {
  document.body.innerHTML = ''
})

describe('SituationBanner teleport transition', () => {
  it('OFFLINE_UNKNOWN → NORMAL does not throw emitsOptions', async () => {
    const target = document.createElement('div')
    target.className = 'workspace-alert'
    document.body.appendChild(target)

    const appErrors: unknown[] = []
    const consoleErrors: string[] = []
    const spy = vi.spyOn(console, 'error').mockImplementation((...args: unknown[]) => {
      consoleErrors.push(args.map((a) => String(a)).join(' '))
    })

    const wrapper = mount(Host, {
      props: { state: 'OFFLINE_UNKNOWN' },
      global: {
        config: {
          errorHandler(err: unknown) {
            appErrors.push(err)
          },
        },
      },
    })
    // 初始 OFFLINE_UNKNOWN：Teleport 目标里应有一个 banner section
    expect(target.querySelector('.situation-banner')).toBeTruthy()

    await wrapper.setProps({ state: 'NORMAL' })
    await nextTick()
    await nextTick()

    // NORMAL：banner 应清空
    expect(target.querySelector('.situation-banner')).toBeNull()

    const fatal = [...appErrors, ...consoleErrors].filter((e) =>
      /emitsOptions|Cannot read properties of null/.test(String(e)),
    )
    expect(fatal).toEqual([])
    spy.mockRestore()
  })
})
