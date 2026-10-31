import assert from 'node:assert/strict'
import { test } from 'node:test'
import { readFileSync, existsSync } from 'node:fs'
import { homedir } from 'node:os'
import { join } from 'node:path'
import { Model, MAIN, links, fold } from './model.mjs'

const base = { session_id: 's1', cwd: '/Users/me/proj', transcript_path: '/t.jsonl' }
const rec = (event, extra = {}) => ({ ...base, event, status: 'done', ts: '2026-10-30T10:00:00.000Z', agent_id: null, agent_type: null, ...extra })

test('spawn → start → tool → stop', () => {
  const m = new Model()
  m.ingest(rec('session_start', { status: 'running' }))
  m.ingest(rec('agent_start', { status: 'running', agent_type: 'web-researcher', description: 'Look up X', tool_use_id: 'toolu_1' }))
  const S = m.latest()
  assert.equal(S.nodes.get('toolu_1').status, 'starting')
  assert.equal(S.nodes.get('toolu_1').parent, MAIN)
  m.ingest(rec('agent_start', { status: 'running', agent_id: 'abc123def456', agent_type: 'web-researcher' }))
  assert.ok(!S.nodes.has('toolu_1'), 'spawn rekeyed to its agent id')
  assert.equal(S.nodes.get('abc123def456').status, 'running')
  assert.deepEqual(S.pending, [])
  m.ingest(rec('tool_end', { agent_id: 'abc123def456', agent_type: 'web-researcher', tool: 'WebSearch', duration_ms: 12 }))
  assert.equal(S.nodes.get('abc123def456').tools, 1)
  assert.equal(S.calls.at(-1).tool, 'WebSearch')
  m.ingest(rec('tool_end', { agent_id: 'ghost', tool: 'Read' }))
  assert.ok(!S.nodes.has('ghost'), 'engine fork never shown')
  assert.equal(links(S, 1e12).find(l => l.to === 'abc123def456').flowing, true)
  m.ingest(rec('agent_stop', { agent_id: 'abc123def456', tokens: { input_tokens: 5 } }))
  assert.equal(S.nodes.get('abc123def456').status, 'done')
  assert.equal(S.comms.at(-1).kind, 'return')
  m.ingest(rec('session_stop'))
  assert.equal(S.nodes.get(MAIN).status, 'idle')
})

test('failed spawn, message resolve, clear', () => {
  const m = new Model()
  m.ingest(rec('session_start', { status: 'running' }))
  m.ingest(rec('agent_start', { status: 'running', agent_type: 'x', tool_use_id: 'toolu_bad' }))
  m.ingest(rec('tool_end', { status: 'failed', tool: 'Agent', tool_use_id: 'toolu_bad' }))
  const S = m.latest()
  assert.equal(S.nodes.get('toolu_bad').status, 'failed')
  m.ingest(rec('agent_start', { status: 'running', agent_type: 'y', tool_use_id: 'toolu_2', name: 'bob' }))
  m.ingest(rec('agent_start', { status: 'running', agent_id: 'feedbeef0001', agent_type: 'y' }))
  m.ingest(rec('tool_end', { tool: 'SendMessage', to: 'bob' }))
  assert.equal(S.comms.at(-1).to.id, 'feedbeef0001')
  m.ingest(rec('tool_end', { tool: 'SendMessage', to: '0001' }))
  assert.equal(S.comms.at(-1).to.id, 'feedbeef0001')
  S.clear()
  assert.ok(!S.nodes.has('toolu_bad') && S.nodes.has('feedbeef0001'))
  m.ingest(rec('session_end'))
  assert.equal(S.nodes.get('feedbeef0001').status, 'gone')
})

test('real log ingests without throwing', { skip: !existsSync(join(homedir(), '.claude/agent-monitor/events.jsonl')) }, () => {
  const m = new Model()
  for (const line of readFileSync(join(homedir(), '.claude/agent-monitor/events.jsonl'), 'utf8').split('\n')) {
    if (!line) continue
    let r
    try { r = JSON.parse(line) } catch { continue }
    m.ingest(r)
  }
  assert.ok(m.latest().nodes.size >= 1)
})

test('targets, prompts, notes, replay, fold', () => {
  const m = new Model()
  m.ingest(rec('session_start', { status: 'running' }))
  m.ingest(rec('prompt', { text: 'build me a hud' }))
  m.ingest(rec('tool_end', { tool: 'Read', target: '/x/a.py', target_kind: 'file', ts: '2026-10-30T10:00:01.000Z' }))
  m.ingest(rec('tool_end', { tool: 'WebFetch', target: 'docs.example.com', target_kind: 'host', ts: '2026-10-30T10:00:02.000Z' }))
  m.ingest(rec('tool_end', { tool: 'Read', target: '/x/a.py', target_kind: 'file', ts: '2026-10-30T10:00:03.000Z' }))
  m.ingest(rec('notification', { kind: 'permission_prompt', ts: '2026-10-30T10:00:04.000Z' }))
  const S = m.latest()
  assert.equal(S.prompts[0].text, 'build me a hud')
  assert.equal(S.targets.get('/x/a.py').count, 2)
  assert.equal(S.targets.get('docs.example.com').kind, 'host')
  assert.equal(S.hits.length, 3)
  assert.equal(S.notes[0].kind, 'permission_prompt')
  const past = m.replayTo('s1', Date.parse('2026-10-30T10:00:01.500Z') / 1000).latest()
  assert.equal(past.hits.length, 1, 'replay stops at the cutoff')
  for (const i of [1, 2, 3]) {
    m.ingest(rec('agent_start', { status: 'running', agent_type: 'Explore', tool_use_id: `toolu_${i}` }))
    m.ingest(rec('agent_start', { status: 'running', agent_id: `aaaa000${i}`, agent_type: 'Explore' }))
    m.ingest(rec('agent_stop', { agent_id: `aaaa000${i}` }))
  }
  assert.deepEqual(S.rekeyed.map(r => r[1]), ['aaaa0001', 'aaaa0002', 'aaaa0003'])
  const f = fold(S, links(S, 1e12))
  assert.equal(f.nodes.length, 2, 'main plus one fold node')
  assert.equal(f.nodes[1].alias, '3 done')
  assert.equal(f.extra[0].to, 'main·done')
  const live = fold(S, links(S, S.nodes.get('aaaa0003').end + 1)) // one still flowing back: stays itself
  assert.equal(live.nodes.length, 4)
})

test('state records belong to no session', () => {
  const m = new Model()
  m.ingest(rec('session_start', { status: 'running' }))
  m.ingest({ ts: '2026-10-30T10:00:05.000Z', event: 'state', status: 'running', session_id: null, state: 'listening' })
  assert.equal(m.state.name, 'listening')
  assert.equal(m.sessions.size, 1, 'no session created for it')
})

test('an ended newer session gives the screen back to the running one', () => {
  const m = new Model()
  m.ingest(rec('session_start', { status: 'running', session_id: 'term', ts: '2026-10-30T10:00:00.000Z' }))
  m.ingest(rec('session_start', { status: 'running', session_id: 'jarvis', ts: '2026-10-30T10:00:05.000Z' }))
  assert.equal(m.latest().id, 'jarvis', 'the newer running session shows while it runs')
  m.ingest(rec('session_end', { session_id: 'jarvis', ts: '2026-10-30T10:00:09.000Z' }))
  assert.equal(m.latest().id, 'term', 'once it ends, the terminal session is back')
})
