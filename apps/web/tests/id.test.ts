import { describe, expect, it } from 'vitest'
import { newUuid } from '@/lib/id'

describe('newUuid', () => {
  it('creates unique RFC 4122 version 4 identifiers without randomUUID', () => {
    const values = Array.from({ length: 100 }, () => newUuid())
    expect(new Set(values).size).toBe(values.length)
    for (const value of values) {
      expect(value).toMatch(/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/)
    }
  })
})
