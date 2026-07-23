// M0-08: EvolutionDashboard.vue 最小回归测试
// 覆盖纯方法 (formatTime / formatTrigger) + 挂载时刷新链路 (fetch stub)

import { describe, it, expect, vi } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import EvolutionDashboard from '../src/components/EvolutionDashboard.vue'

describe('EvolutionDashboard.vue 纯方法', () => {
  function getMethods() {
    return EvolutionDashboard.methods
  }

  describe('formatTime', () => {
    it('空值返回空串', () => {
      const { formatTime } = getMethods()
      expect(formatTime('')).toBe('')
      expect(formatTime(null)).toBe('')
      expect(formatTime(undefined)).toBe('')
    })

    it('合法时间戳返回非空格式化字符串', () => {
      const { formatTime } = getMethods()
      const out = formatTime('2026-07-21T08:00:00Z')
      expect(out).toBeTruthy()
      expect(out).not.toBe('2026-07-21T08:00:00Z')
    })

    it('非法时间返回 Invalid Date(new Date 不抛异常,只返回 Invalid Date 对象)', () => {
      const { formatTime } = getMethods()
      expect(formatTime('not-a-date')).toBe('Invalid Date')
    })
  })

  describe('formatTrigger', () => {
    it('空值返回空串', () => {
      const { formatTrigger } = getMethods()
      expect(formatTrigger('')).toBe('')
      expect(formatTrigger(null)).toBe('')
    })

    it('same_scenario_* 前缀统一映射为"同场景重复失败"', () => {
      const { formatTrigger } = getMethods()
      expect(formatTrigger('same_scenario_3')).toBe('同场景重复失败')
      expect(formatTrigger('same_scenario_x')).toBe('同场景重复失败')
    })

    it('已知 reason 关键字映射为中文标签', () => {
      const { formatTrigger } = getMethods()
      expect(formatTrigger('high_failure_count')).toBe('失败过多')
      expect(formatTrigger('user_request')).toBe('用户请求')
    })

    it('未知 reason 原样返回', () => {
      const { formatTrigger } = getMethods()
      expect(formatTrigger('custom_trigger')).toBe('custom_trigger')
    })
  })
})

describe('EvolutionDashboard.vue 组件挂载', () => {
  it('挂载触发 features + stats + reflections 三个 fetch,空数据显示占位', async () => {
    const fetchCalls = []
    global.fetch = vi.fn((url) => {
      fetchCalls.push(url)
      let body = {}
      if (String(url).endsWith('/api/features')) {
        body = { self_evolution_enabled: false }
      } else if (String(url).endsWith('/api/memory/stats')) {
        body = { total_failures: 0, total_successes: 0, pending_patches: 0 }
      } else if (String(url).endsWith('/api/reflections')) {
        body = { reflections: [] }
      }
      return Promise.resolve({
        ok: true,
        json: async () => body,
      })
    })

    const wrapper = await mount(EvolutionDashboard)
    // mounted 内有多次 await,等所有 promise 完成
    await flushPromises()

    // 至少调用了 features 和 stats(自我进化关时不会拉 reflections)
    expect(fetchCalls.some((u) => String(u).endsWith('/api/features'))).toBe(true)
    expect(fetchCalls.some((u) => String(u).endsWith('/api/memory/stats'))).toBe(true)

    // 关闭自我进化时,显示"暂无复盘报告"
    expect(wrapper.text()).toContain('暂无复盘报告')
  })
})
