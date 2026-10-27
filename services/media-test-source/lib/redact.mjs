// secret redaction（P0-B）：合成 SRT/FFmpeg 输出脱敏。
// 与 scripts/redact_secrets.py 等价（Python 侧单测 scripts/test_secret_redaction.py）。
// 背景：ffmpeg/libSRT 的 stderr 会把完整 SRT URL（streamid 内发布 token + passphrase
// 查询值）原样写入 console/持久日志；-loglevel fatal 无法抑制 SRT 库告警。
// 规则（顺序敏感）：
//   - SRT streamid：publish:<path>:<user>:<cred> → 保留 path/user，cred 掩码 ***
//   - URL 查询：passphrase=/passwd=/password=/token= 的值 → ***
//   - Authorization/Bearer 头 → ***
// 保留：hostname、port、vehicle_id、stream path、错误类型。

const STREAMID_CRED = /(publish:[^:\s&'"()]+:[^:\s&'"()]+:)([^&\s'"]+)/g
const QUERY_CRED = /((?:passphrase|passwd|password|token|pwd)=)([^&\s'"]+)/gi
const BEARER_CRED = /((?:authorization|bearer)[\s:=]+(?:bearer[\s:=]+)?)([A-Za-z0-9._~+/-]{12,})/gi

export function redact(text) {
  if (typeof text !== 'string' || !text) return text
  return text
    .replace(STREAMID_CRED, '$1***')
    .replace(QUERY_CRED, '$1***')
    .replace(BEARER_CRED, '$1***')
}
