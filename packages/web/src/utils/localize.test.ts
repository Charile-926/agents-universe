import { describe, it, expect } from 'vitest'
import { resolveLocalized, localizedText, searchBlob } from './localize'

describe('resolveLocalized', () => {
  it('returns the canonical value when overrides are absent', () => {
    expect(resolveLocalized('技术负责人', null)).toBe('技术负责人')
    expect(resolveLocalized('技术负责人', undefined)).toBe('技术负责人')
  })

  it('returns the zh-CN override when present', () => {
    const overrides = { 'zh-CN': '技术负责人（正式）' }
    expect(resolveLocalized('技术负责人', overrides)).toBe('技术负责人（正式）')
  })

  it('falls back to canonical when the locale has no override', () => {
    const overrides = { 'en-US': 'Tech Lead' }
    // 平台固定简体中文 — 映射里没有 zh-CN，canonical 胜出
    expect(resolveLocalized('技术负责人', overrides)).toBe('技术负责人')
  })

  it('ignores overrides of languages the platform no longer supports', () => {
    const overrides = { 'zh-CN': '技术负责人', 'en-US': 'Tech Lead' }
    expect(resolveLocalized('技术负责人', overrides)).toBe('技术负责人')
  })

  it('resolves non-string values (e.g. starter prompt lists)', () => {
    const prompts = ['帮我评审代码']
    const overrides = { 'zh-CN': ['帮我评审代码（正式）'] }
    expect(resolveLocalized(prompts, overrides)).toEqual(['帮我评审代码（正式）'])
    expect(resolveLocalized(prompts, null)).toEqual(prompts)
  })
})

describe('localizedText', () => {
  it('returns the localized string', () => {
    const overrides = { 'zh-CN': '技术负责人（正式）' }
    expect(localizedText('技术负责人', overrides)).toBe('技术负责人（正式）')
  })

  it('applies the fallback when both canonical and overrides are empty', () => {
    expect(localizedText(null, null, 'fallback')).toBe('fallback')
    expect(localizedText(undefined, undefined, 'fallback')).toBe('fallback')
  })

  it('prefers canonical over the fallback when no locale override exists', () => {
    expect(localizedText('canonical', null, 'fallback')).toBe('canonical')
  })
})

describe('searchBlob', () => {
  it('includes canonical and every locale value, lowercased', () => {
    const blob = searchBlob('技术负责人', { 'en-US': 'Tech Lead' })
    expect(blob).toContain('技术负责人')
    expect(blob).toContain('tech lead')
  })

  it('handles absent canonical and overrides', () => {
    expect(searchBlob(null, null)).toBe('')
    expect(searchBlob(undefined, undefined)).toBe('')
  })

  it('matches a query in either language', () => {
    const overrides = { 'en-US': 'Tech Lead' }
    const blob = searchBlob('技术负责人', overrides)
    expect(blob.includes('tech lead')).toBe(true)
    expect(blob.includes('技术负责人')).toBe(true)
    expect(blob.includes('design')).toBe(false)
  })
})
