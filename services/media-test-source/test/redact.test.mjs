// secret redaction 单测（P0-B，node --test）。
// 全部使用合成测试值（test-*）；不得把任何真实 secret 写入本文件或快照。
import test from 'node:test'
import assert from 'node:assert/strict'
import { redact } from '../lib/redact.mjs'

const FAKE_TOKEN = 'test-token-abcd1234'
const FAKE_PASSPHRASE = 'test-passphrase-WxYz+123/456'

test('SRT URL token/passphrase 掩码且保留 host/port/path', () => {
  const url =
    `srt://firebot.youdaozd.com:8890?streamid=publish:R001-roof_rgb:R001:${FAKE_TOKEN}` +
    `&pkt_size=1316&passphrase=${FAKE_PASSPHRASE}`
  const out = redact(url)
  assert.ok(!out.includes(FAKE_TOKEN), out)
  assert.ok(!out.includes(FAKE_PASSPHRASE), out)
  assert.ok(out.includes('firebot.youdaozd.com:8890'), out)
  assert.ok(out.includes('R001-roof_rgb:R001:***'), out)
  assert.ok(out.includes('passphrase=***'), out)
})

test('percent-encoded passphrase 掩码', () => {
  const encoded = 'test-passphrase-WxYz%2B123%2F456'
  const out = redact(`srt://h:8890?streamid=x&passphrase=${encoded}`)
  assert.ok(!out.includes(encoded), out)
  assert.ok(out.includes('passphrase=***'), out)
})

test('RTSP token 掩码', () => {
  const out = redact(`rtsp://mediamtx:8554/R001-roof_rgb?token=${FAKE_TOKEN}`)
  assert.ok(!out.includes(FAKE_TOKEN), out)
  assert.ok(out.includes('mediamtx:8554/R001-roof_rgb'), out)
})

test('libSRT 错误日志行脱敏且保留错误类型', () => {
  const line =
    `[out#0/mpegts] Error opening output srt://firebot.youdaozd.com:8890` +
    `?streamid=publish:R001-roof_thermal:R001:${FAKE_TOKEN}` +
    `&pkt_size=1316&passphrase=${FAKE_PASSPHRASE}: I/O error`
  const out = redact(line)
  assert.ok(!out.includes(FAKE_TOKEN), out)
  assert.ok(!out.includes(FAKE_PASSPHRASE), out)
  assert.ok(out.includes('I/O error'), out)
  assert.ok(out.includes('R001-roof_thermal:R001:***'), out)
})

test('Authorization/Bearer 掩码', () => {
  for (const text of [
    'Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.abcdefg',
    'authorization=bearer eyJhbGciOiJIUzI1NiJ9.abcdefg',
  ]) {
    const out = redact(text)
    assert.ok(!out.includes('eyJhbGciOiJIUzI1NiJ9.abcdefg'), out)
  }
})

test('password 查询掩码', () => {
  const out = redact('?user=R001&password=test-mqtt-password-xyz&x=1')
  assert.ok(!out.includes('test-mqtt-password-xyz'), out)
  assert.ok(out.includes('password=***'), out)
})

test('redact 幂等且非字符串原样返回', () => {
  const url =
    `srt://h:8890?streamid=publish:R001-roof_rgb:R001:${FAKE_TOKEN}` +
    `&passphrase=${FAKE_PASSPHRASE}`
  const once = redact(url)
  assert.equal(redact(once), once)
  assert.equal(redact(null), null)
  assert.equal(redact(42), 42)
})
