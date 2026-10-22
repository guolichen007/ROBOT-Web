// Preserve full audit evidence; only production Critical vulnerabilities block this gate.
const { spawnSync } = require('node:child_process')
const fs = require('node:fs')
const path = require('node:path')

const root = path.resolve(__dirname, '../apps/web')
const directory = path.resolve(root, '../../artifacts')
fs.mkdirSync(directory, { recursive: true })

function audit(name, args) {
  const npmArgs = ['audit', ...args, '--json']
  // Arguments are fixed internally; invoke cmd explicitly on Windows without shell:true.
  const windows = process.platform === 'win32'
  const commandArgs = windows ? ['/d', '/s', '/c', `npm.cmd ${npmArgs.join(' ')}`] : npmArgs
  const result = spawnSync(windows ? 'cmd.exe' : 'npm', commandArgs, {
    cwd: root,
    encoding: 'utf8',
    maxBuffer: 20 * 1024 * 1024,
  })
  fs.writeFileSync(path.join(directory, `npm-audit-${name}.json`), result.stdout || '{}')
  if (result.error) throw result.error
  const report = JSON.parse(result.stdout)
  const counts = report.metadata?.vulnerabilities
  if (report.error || !counts || !Number.isInteger(counts.critical) || ![0, 1].includes(result.status)) {
    throw new Error(`npm ${name} audit unavailable; gate fails closed`)
  }
  return { counts, status: result.status }
}

const production = audit('production', ['--omit=dev', '--audit-level=critical'])
const full = audit('full', [])
console.log(`NPM_PRODUCTION_CRITICAL_COUNT=${production.counts.critical}`)
console.log(`NPM_PRODUCTION_AUDIT=${JSON.stringify(production.counts)}`)
console.log(`NPM_FULL_AUDIT=${JSON.stringify(full.counts)}`)
if (production.counts.critical !== 0 || production.status !== 0) {
  console.error('NPM_PRODUCTION_CRITICAL=FAIL')
  process.exitCode = 1
} else {
  console.log('NPM_PRODUCTION_CRITICAL=PASS')
}
