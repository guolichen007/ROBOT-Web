const fs = require('node:fs')
const text = fs.readFileSync(process.argv[2], 'utf8')
const result = JSON.parse(text)
const stats = result.stats
if (stats.expected < 1 || stats.skipped !== 0 || stats.unexpected !== 0 || stats.flaky !== 0) {
  throw new Error(`Vehicle agent E2E did not execute cleanly: ${JSON.stringify(stats)}`)
}
console.log(`VEHICLE_AGENT_CONTAINER_E2E=PASS RUN=${stats.expected} SKIPPED=${stats.skipped}`)
