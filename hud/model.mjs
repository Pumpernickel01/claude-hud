// Event model for the HUD: a port of the Model/Session/Node classes in
// ../activity-pane/viewer/shell_tabs.py so both views agree on who spawned whom.
// Pure: no DOM, no fs. Imported by index.html (browser) and model.test.mjs (node).

export const MAIN = 'main'
export const FLOW_AFTER_RETURN = 3.0 // seconds a finished agent's edge flows back to its parent
export const FLOW_AFTER_MESSAGE = 8.0

export const parseTs = s => (s ? Date.parse(s) / 1000 : 0)

class Node {
  constructor(id, kind, start) {
    this.id = id
    this.kind = kind
    this.start = start
    this.end = null
    this.status = 'running'
    this.task = ''
    this.alias = null
    this.tools = 0
    this.lastTool = ''
    this.tokens = null
    this.parent = null
  }
  get isLive() {
    return this.status === 'running' || this.status === 'starting'
  }
}

class Session {
  constructor(sid, ts) {
    this.id = sid
    this.start = ts
    this.last = ts
    this.ended = false
    this.cwd = ''
    this.pending = [] // spawns (by tool_use_id) waiting for their SubagentStart, oldest first
    this.nodes = new Map([[MAIN, new Node(MAIN, 'session', ts)]])
    this.comms = [] // {ts, kind, from: Node, to: Node | string}
    this.names = new Map() // Agent(name=...) -> agent id
    this.calls = [] // tool_end records, newest last, capped
    this.rekeyed = [] // [spawn tool_use_id, agent id] pairs the view drains to carry positions over
    this.prompts = [] // {ts, text}
    this.notes = [] // {ts, kind}
    this.targets = new Map() // file path / host / mcp server -> {id, kind, count, last, from}
    this.hits = [] // {ts, from: agent id, target id}, capped
    this.counts = { calls: 0, hits: 0, prompts: 0, notes: 0 } // totals, so a view can see what is new past the caps
  }
  node(nid, ts) {
    if (!this.nodes.has(nid)) this.nodes.set(nid, new Node(nid, 'agent', ts))
    return this.nodes.get(nid)
  }
  rekey(oldId, newId) {
    const n = this.nodes.get(oldId)
    this.nodes.delete(oldId)
    n.id = newId
    this.nodes.set(newId, n)
    for (const m of this.nodes.values()) if (m.parent === oldId) m.parent = newId
    this.rekeyed.push([oldId, newId])
  }
  resolve(to) {
    if (this.nodes.has(to)) return this.nodes.get(to)
    if (this.names.has(to)) return this.nodes.get(this.names.get(to))
    if (to === 'main') return this.nodes.get(MAIN)
    for (const [nid, n] of this.nodes) if (nid !== MAIN && to.length >= 4 && nid.endsWith(to)) return n
    return null
  }
  retire(ts) {
    for (const [nid, n] of this.nodes) if (nid !== MAIN && n.isLive) (n.status = 'gone'), (n.end = ts)
    this.pending = []
  }
  clear() {
    const gone = new Set([...this.nodes].filter(([nid, n]) => nid !== MAIN && !n.isLive).map(([nid]) => nid))
    for (const nid of gone) this.nodes.delete(nid)
    for (const n of this.nodes.values()) if (gone.has(n.parent)) n.parent = MAIN
    this.comms = this.comms.filter(c => !gone.has(c.from.id) && !gone.has(c.to?.id))
  }
}

const push = (arr, item, cap) => { arr.push(item); if (arr.length > cap) arr.shift(); return arr.length }

export class Model {
  constructor() {
    this.sessions = new Map()
    this.events = [] // every record, for replay
  }
  /** A fresh model of one session as it stood at `ts` (seconds), for the history scrubber. */
  replayTo(sid, ts) {
    const m = new Model()
    for (const r of this.events) if (r.session_id === sid && parseTs(r.ts) <= ts) m.ingest(r)
    return m
  }
  /** The session to show: the newest still running, else the newest of all (a short headless session,
   *  such as Jarvis answering, takes the screen while it runs and gives it back when it ends). */
  latest() {
    let best = null, live = null
    for (const s of this.sessions.values()) {
      if (!best || s.last > best.last) best = s
      if (!s.ended && (!live || s.last > live.last)) live = s
    }
    return live || best
  }
  ingest(rec) {
    const ev = rec.event
    const ts = parseTs(rec.ts)
    if (ev === 'state') { this.state = { ts, name: rec.state || '' }; return } // listening | thinking | speaking | idle: Jarvis, not a session
    this.events.push(rec)
    const sid = rec.session_id || 'unknown'
    if (!this.sessions.has(sid)) this.sessions.set(sid, new Session(sid, ts))
    const S = this.sessions.get(sid)
    S.last = Math.max(S.last, ts)
    if (rec.cwd) S.cwd = rec.cwd
    const aid = rec.agent_id
    const main = S.nodes.get(MAIN)
    if (ev === 'session_start') {
      S.retire(ts)
      main.status = 'running'
      main.end = null
      S.ended = false
      main.task = (rec.cwd || '').split('/').pop()
    } else if (ev === 'agent_start' && aid == null) {
      // PreToolUse Agent: shown at once, as starting, under its tool_use_id until it has an agent id
      const tid = rec.tool_use_id || `spawn·${ts}`
      const n = S.node(tid, ts)
      n.kind = rec.agent_type || 'agent'
      n.task = rec.description || rec.task || ''
      n.parent = rec.parent_agent_id || MAIN
      n.alias = rec.name || null
      n.status = 'starting'
      S.pending.push(tid)
      S.comms.push({ ts, kind: 'spawn', from: S.node(n.parent, ts), to: n })
    } else if (ev === 'agent_start') {
      // SubagentStart: joined to the oldest waiting spawn of its type; otherwise shown on its own
      const kind = rec.agent_type
      const tid = S.pending.find(t => !kind || S.nodes.get(t).kind === kind)
      if (tid) {
        S.pending.splice(S.pending.indexOf(tid), 1)
        S.rekey(tid, aid)
      }
      const n = S.node(aid, ts)
      if (!tid) (n.kind = kind || n.kind), (n.parent = n.parent || MAIN)
      n.start = ts
      n.status = 'running'
      n.end = null
      if (n.alias) S.names.set(n.alias, aid)
    } else if (ev === 'tool_end') {
      // a tool call of an agent never seen starting is a fork the engine runs itself: not shown
      const n = aid ? S.nodes.get(aid) : main
      if (!n) return
      n.tools += 1
      n.lastTool = rec.tool || ''
      if (aid == null && !S.ended) main.status = 'running'
      if (n.lastTool === 'Agent' && rec.status === 'failed' && S.pending.includes(rec.tool_use_id)) {
        const failed = S.nodes.get(rec.tool_use_id)
        S.pending.splice(S.pending.indexOf(rec.tool_use_id), 1)
        failed.status = 'failed'
        failed.end = ts
      }
      if (n.lastTool === 'SendMessage' && rec.to) S.comms.push({ ts, kind: 'message', from: n, to: S.resolve(rec.to) || String(rec.to) })
      S.counts.calls++
      push(S.calls, { ts, agent: n, tool: n.lastTool, status: rec.status, ms: rec.duration_ms ?? null, target: rec.target || null, targetKind: rec.target_kind || null }, 200)
      if (rec.target) {
        const t = S.targets.get(rec.target) || { id: rec.target, kind: rec.target_kind || 'file', count: 0, last: 0, from: null }
        t.count += 1; t.last = ts; t.from = n.id
        S.targets.set(rec.target, t)
        S.counts.hits++
        push(S.hits, { ts, from: n.id, target: rec.target }, 100)
      }
    } else if (ev === 'agent_stop' && S.nodes.has(aid)) {
      const n = S.nodes.get(aid)
      n.status = rec.status === 'failed' ? 'failed' : 'done'
      n.end = ts
      n.tokens = rec.tokens || n.tokens
      S.comms.push({ ts, kind: 'return', from: n, to: S.node(n.parent || MAIN, ts) })
    } else if (ev === 'prompt') {
      S.counts.prompts++
      push(S.prompts, { ts, text: rec.text || '' }, 50)
      if (!S.ended) main.status = 'running'
    } else if (ev === 'notification') {
      S.counts.notes++
      push(S.notes, { ts, kind: rec.kind || 'notification' }, 50)
    } else if (ev === 'session_stop') {
      main.status = 'idle' // between turns: waiting for you, not gone
    } else if (ev === 'session_end') {
      S.retire(ts)
      main.status = 'done'
      main.end = ts
      S.ended = true
    }
  }
}

/** Every edge as {from, to, kind, flowing, back}. Spawn edges flow parent to child while the
 *  child runs and back for FLOW_AFTER_RETURN after it finishes; message edges for FLOW_AFTER_MESSAGE. */
export function links(S, now) {
  const out = []
  for (const [nid, n] of S.nodes) {
    if (nid === MAIN || !S.nodes.has(n.parent)) continue
    const back = !n.isLive && n.end != null && now - n.end < FLOW_AFTER_RETURN
    out.push({ from: n.parent, to: nid, kind: 'spawn', flowing: n.isLive || back, back })
  }
  for (const c of S.comms) {
    if (c.kind !== 'message' || typeof c.to === 'string') continue
    out.push({ from: c.from.id, to: c.to.id, kind: 'message', flowing: now - c.ts < FLOW_AFTER_MESSAGE, back: false })
  }
  return out
}

/** Finished sibling leaves as one node per parent and status, as the terminal graph folds them (shell_tabs.py
 *  fold): agents still live, talking, or with children stay themselves. Returns the nodes to draw and the
 *  folds' edges. A fold node is {id, kind: 'fold', status, parent, alias, count, isLive: false}. */
export function fold(S, edges) {
  const busy = new Set(edges.filter(e => e.flowing).flatMap(e => [e.from, e.to]))
  const parents = new Set([...S.nodes.values()].map(n => n.parent))
  const groups = new Map()
  for (const n of S.nodes.values()) {
    if (n.id === MAIN || parents.has(n.id) || n.isLive || busy.has(n.id)) continue
    const key = `${n.parent}·${n.status}`
    if (!groups.has(key)) groups.set(key, [])
    groups.get(key).push(n)
  }
  const nodes = [], extra = []
  for (const n of S.nodes.values()) {
    const group = n.id === MAIN ? null : groups.get(`${n.parent}·${n.status}`)
    if (!group || group.length < 2) nodes.push(n)
    else if (n === group[0]) {
      const id = `${n.parent}·${n.status}`
      nodes.push({ id, kind: 'fold', status: n.status, parent: n.parent, alias: `${group.length} ${n.status}`, count: group.length, members: group, isLive: false, start: n.start, end: n.end, tools: group.reduce((a, m) => a + m.tools, 0), tokens: null, task: '', lastTool: '' })
      extra.push({ from: n.parent, to: id, kind: 'spawn', flowing: false, back: false })
    }
  }
  return { nodes, extra }
}
