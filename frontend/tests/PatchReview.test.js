// M0-08: PatchReview.vue 最小回归测试
// 只覆盖纯方法 (formatTime / formatSuggestion / confidenceClass),
// 不依赖后端,避免 e2e 的 flake。

import { describe, it, expect, vi } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import PatchReview from '../src/components/PatchReview.vue'

describe('PatchReview.vue 纯方法', () => {
  function getMethods() {
    // 直接从 SFC options 中取出 methods
    return PatchReview.methods
  }

  describe('formatTime', () => {
    it('空值返回空串', () => {
      const { formatTime } = getMethods()
      expect(formatTime('')).toBe('')
      expect(formatTime(null)).toBe('')
      expect(formatTime(undefined)).toBe('')
    })

    it('合法 ISO 时间戳返回 mm/dd hh:mm 形状(中文区域)', () => {
      const { formatTime } = getMethods()
      const out = formatTime('2026-07-21T10:30:00Z')
      // 不强断言具体文字(依赖时区),只断言非空且非原值
      expect(out).toBeTruthy()
      expect(out).not.toBe('2026-07-21T10:30:00Z')
    })

    it('非法时间返回 Invalid Date(try/catch 不会捕获,因 new Date 不抛错)', () => {
      const { formatTime } = getMethods()
      // new Date('not-a-date') 不抛异常,而是返回 Invalid Date 对象,
      // 其 toLocaleString 返回 'Invalid Date' 字符串
      expect(formatTime('not-a-date')).toBe('Invalid Date')
    })
  })

  describe('formatSuggestion', () => {
    it('字符串原样返回', () => {
      const { formatSuggestion } = getMethods()
      expect(formatSuggestion('do something')).toBe('do something')
    })

    it('对象 JSON 序列化', () => {
      const { formatSuggestion } = getMethods()
      const out = formatSuggestion({ type: 'improve_skill', target: 'demo' })
      expect(out).toContain('"type": "improve_skill"')
      expect(out).toContain('"target": "demo"')
    })

    it('数组也走 JSON 路径', () => {
      const { formatSuggestion } = getMethods()
      expect(formatSuggestion([1, 2, 3])).toBe('[\n  1,\n  2,\n  3\n]')
    })
  })

  describe('confidenceClass', () => {
    it('>=0.9 → high', () => {
      const { confidenceClass } = getMethods()
      expect(confidenceClass(0.9)).toBe('high')
      expect(confidenceClass(0.95)).toBe('high')
      expect(confidenceClass(1.0)).toBe('high')
    })

    it('>=0.7 且 <0.9 → medium', () => {
      const { confidenceClass } = getMethods()
      expect(confidenceClass(0.7)).toBe('medium')
      expect(confidenceClass(0.85)).toBe('medium')
    })

    it('<0.7 → low', () => {
      const { confidenceClass } = getMethods()
      expect(confidenceClass(0.0)).toBe('low')
      expect(confidenceClass(0.69)).toBe('low')
    })
  })
})

describe('PatchReview.vue 组件挂载', () => {
  it('挂载时显示"暂无待审阅"占位并尝试拉取(stats 失败也不抛错)', async () => {
    // stub fetch —— 返回空列表,避免 mounted 抛错
    global.fetch = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({}),
    })

    const wrapper = await mount(PatchReview)
    // mounted 触发两个 fetch,等所有 promise 完成
    await flushPromises()

    expect(wrapper.text()).toContain('改进建议')
    // 没有数据时显示 empty 占位
    expect(wrapper.text()).toContain('暂无待审阅的改进建议')
  })
})
