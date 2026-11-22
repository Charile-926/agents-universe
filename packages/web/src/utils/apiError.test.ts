import { describe, expect, it } from 'vitest'
import { ApiError } from '@/api/client'
import { t } from '@/i18n'
import { translateApiError, translateApiErrorWith } from './apiError'

describe('translateApiError', () => {
  it('把已知错误码映射为中文提示', () => {
    expect(translateApiError(new ApiError(401, 'Invalid credentials', 'AUTH_INVALID_CREDENTIALS')))
      .toBe('用户名或密码错误。')
    expect(translateApiError(new ApiError(404, 'No history', 'DISTILL_NO_HISTORY')))
      .toBe('当前账号还没有可用于炼化的聊天记录。')
  })

  it('未知错误码回落到通用中文提示（不暴露英文原文）', () => {
    const e = new ApiError(400, 'Something brand new', 'FANCY_NEW_CODE')
    expect(translateApiError(e)).toBe('操作失败，请稍后重试。')
  })

  it('缺少错误码时同样回落到通用提示', () => {
    const e = new ApiError(500, 'plain message')
    expect(translateApiError(e)).toBe('操作失败，请稍后重试。')
  })

  it('非 Error 值也返回通用中文提示', () => {
    expect(translateApiError('boom')).toBe('操作失败，请稍后重试。')
    expect(translateApiError(undefined)).toBe('操作失败，请稍后重试。')
  })
})

describe('translateApiErrorWith', () => {
  it('uses the fallback for non-Error values', () => {
    expect(translateApiErrorWith(null, 'fallback')).toBe('fallback')
  })

  it('translates Error values through translateApiError', () => {
    expect(translateApiErrorWith(new ApiError(401, 'x', 'AUTH_REQUIRED'), 'fallback'))
      .toBe('请先登录。')
  })
})

describe('文案插值', () => {
  it('替换 {name} 占位符', () => {
    expect(t('skillDistill.selectedCount', { count: 3 })).toBe('已选 3 个会话')
    expect(t('skillDistill.stats', { conversations: 2, messages: 10, toolCalls: 4 }))
      .toBe('汇总了 2 个会话、10 条消息、4 次工具调用。')
  })

  it('缺少文案时返回 key 本身，不抛错', () => {
    expect(t('not.exist')).toBe('not.exist')
  })
})
