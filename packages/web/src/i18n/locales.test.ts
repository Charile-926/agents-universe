/**
 * 中文文案目录自检。
 *
 * 平台已固定简体中文，这里只需要保证：目录里每条文案都能被 t() 解析（不会
 * 出现空串或被原样回吐的 key），以及关键页面文案齐全，避免界面上出现空白。
 */
import { describe, expect, it } from 'vitest'
import zhCN from './locales/zh-CN'
import { i18n } from './index'

function flatten(obj: Record<string, unknown>, prefix = ''): [string, string][] {
  const out: [string, string][] = []
  for (const [k, v] of Object.entries(obj)) {
    const key = prefix ? `${prefix}.${k}` : k
    if (typeof v === 'string') out.push([key, v])
    else if (v && typeof v === 'object') out.push(...flatten(v as Record<string, unknown>, key))
  }
  return out
}

describe('中文文案目录', () => {
  it('每条文案都能解析且非空', () => {
    const bad: string[] = []
    for (const key of flatten(zhCN as Record<string, unknown>).map(([k]) => k)) {
      const resolved = i18n.global.t(key)
      if (!resolved || resolved === key || !resolved.trim()) bad.push(key)
    }
    expect(bad).toEqual([])
  })

  it('关键页面文案齐全', () => {
    expect(i18n.global.t('auth.title')).toBe('Agents Universe')
    expect(i18n.global.t('auth.submitLogin')).toBe('登录')
    expect(i18n.global.t('skillDistill.title')).toBe('炼化 Skill')
    expect(i18n.global.t('layout.tabConversations')).not.toBe('layout.tabConversations')
    expect(i18n.global.t('errors.UNKNOWN')).toContain('操作失败')
  })
})
