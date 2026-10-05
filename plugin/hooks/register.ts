import type { EngineInterface, PluginOptions, Register } from 'claude-code'

import {
  CONFIRMED,
  Confirmation,
  LineSplitter,
  type ListenerEvent,
  type ListenerLine,
  type ListenerState,
  UNREAD_NUDGE,
  feedbackRule,
  midTurn,
  narrationInput,
  parseLine,
  refuseFiling,
  slotFor,
  splitArgv,
  statusLine,
  usage,
} from './link'

// The mod is the session half of hands-free-voice. The listener (the Python
// package's `hands-free-voice listen`) owns the mic, the speech link and the
// speaker; this module starts it, hands its heard turns to Claude Code, and
// posts each turn's start, tool calls, text and end back for it to speak.
// A hot reload (an edit to the mod, a changed option) stops the listener;
// session.start starts it again when voice was on.

const COMMAND = 'hands-free'
const SECTION = 'hands-free-voice:contract'
const BASE = 'http://hands-free'
// The listener this version of the mod speaks to, pinned. To run a checkout's
// listener while developing, point this at its .venv/bin/hands-free-voice;
// tests/test_release.py fails while it points anywhere else.
const LISTENER = 'uvx hands-free-voice==0.1.1 listen'
// Whether voice should be on. Module variables start over on a hot reload, which
// also stops the listener; this survives it, so the new copy can start it again.
const LISTENING = { plugin: 'hands-free-voice', key: 'listening' } as const

let child: AsyncGenerator<unknown, unknown> | undefined // the listener, while it runs
let generation = 0 // which start the running listener came from
let socket: string | undefined // its socket, once it said hello
let contract: string | undefined // the voice contract it sent
let commands: string | undefined // the spoken commands as usage, from its hello
let heard: ListenerState = { type: 'state', state: 'idle' } // its last reported state
let turnId: string | undefined // the main loop's running turn
let unread = 0 // heard turns put into the running turn that no request has carried yet
const confirmation = new Confirmation() // the spoken confirm a feedback filing needs
let posting: Promise<unknown> = Promise.resolve()

// Posts go one at a time, in order: a turn's text must reach the listener
// before its end, or the end would speak the voice block a second time.
function post($: EngineInterface, ev: ListenerEvent) {
  const socketPath = socket
  if (!socketPath) return
  posting = posting
    .then(() =>
      $.http.fetch(`${BASE}/event`, {
        method: 'POST',
        socketPath,
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify(ev),
      }),
    )
    .catch(err => $.ui.log(`hands-free-voice: post failed: ${String(err)}`, { to: 'debug' }))
}

// The status line: the listener's state (or "working" while a turn runs) in
// a fixed slot, then the spoken commands; "starting" until its hello.
function paint($: EngineInterface) {
  if (!child) return
  $.ui.status(commands ? statusLine(slotFor(heard, turnId !== undefined), commands) : 'starting')
}

async function deliver($: EngineInterface, text: string) {
  if (turnId !== undefined) {
    try {
      await $.session.append({ message: { type: 'user', content: [{ type: 'text', text: midTurn(text) }] } })
      $.ui.log(`you ▸ ${text}`) // an appended row is the model's alone: show it on screen too
      unread += 1
      return
    } catch (err) {
      $.ui.log(`hands-free-voice: could not join the running turn (${String(err)}); queued`, { to: 'debug' })
    }
  }
  void $.prompt.submit({ text, asUser: true })
}

async function compact($: EngineInterface) {
  try {
    await $.session.compact()
    post($, { kind: 'compacted' })
  } catch (err) {
    $.ui.log(`hands-free-voice: compact failed: ${String(err)}`)
  }
}

function onLine($: EngineInterface, line: ListenerLine) {
  switch (line.type) {
    case 'hello':
      socket = line.socket
      contract = `${line.contract}\n\n${feedbackRule(line.version)}`
      commands = usage(line.address, line.closer, line.commands)
      paint($)
      $.ui.toast(`Hands-free voice on: say "${line.address}", talk, then "${line.closer}".`)
      return
    case 'state':
      heard = line
      paint($)
      return
    case 'turn':
      confirmation.drop()
      void deliver($, line.text)
      return
    case 'compact':
      void compact($)
      return
    case 'confirm':
      confirmation.give()
      void deliver($, CONFIRMED)
      return
    case 'log':
      $.ui.log(line.text, { to: 'debug' })
      return
    case 'error':
      $.ui.log(`hands-free-voice: ${line.text}`)
      return
  }
}

async function listen($: EngineInterface, argv: string[], cwd: string, env: Record<string, string>) {
  const mine = ++generation
  const stream = $.process.spawn({ argv, cwd, env })
  child = stream
  socket = undefined
  contract = undefined
  commands = undefined
  heard = { type: 'state', state: 'idle' }
  confirmation.drop()
  paint($)
  const lines = new LineSplitter()
  let killed = false
  try {
    for (;;) {
      const piece = await stream.next()
      if (piece.done) {
        // the listener exits 128 + the signal when it caught one and closed cleanly
        const { code, signal } = piece.value
        killed = signal !== null || (code ?? 0) >= 128
        break
      }
      const { stream: pipe, text } = piece.value
      if (pipe === 'stderr') {
        $.ui.log(text.trimEnd(), { to: 'debug' })
        continue
      }
      for (const raw of lines.push(text)) {
        const line = parseLine(raw)
        if (line) onLine($, line)
      }
    }
  } catch (err) {
    $.ui.log(`hands-free-voice: the listener did not start: ${String(err)} (is \`${argv[0]}\` on PATH?)`)
  } finally {
    if (generation === mine) {
      child = undefined
      socket = undefined
      contract = undefined
      commands = undefined
    }
    // Ended on its own (a spoken quit, a failed start): voice stays off. Ended
    // by a signal (a reload's): the flag stands, for the new copy to restart it.
    if (!killed) await $.state.set(LISTENING, false)
    $.ui.status(undefined)
    $.ui.toast('Hands-free voice off.')
  }
}

function start($: EngineInterface, cwd: string, options: PluginOptions) {
  void $.state.set(LISTENING, true)
  const argv = [...splitArgv(LISTENER), '--project', cwd]
  const env: Record<string, string> = {}
  if (options.deepgram_api_key) env.DEEPGRAM_API_KEY = String(options.deepgram_api_key)
  if (options.cartesia_api_key) env.CARTESIA_API_KEY = String(options.cartesia_api_key)
  void listen($, argv, cwd, env)
}

async function stop($: EngineInterface) {
  const running = child
  if (!running) return
  await $.state.set(LISTENING, false)
  if (socket) {
    const quit = await $.http.fetch(`${BASE}/quit`, { method: 'POST', socketPath: socket }).catch(() => undefined)
    if (quit?.ok) return
  }
  await running.return(undefined)
}

async function complete($: EngineInterface, answer: string, reason: string, durationMs: number) {
  const { context } = await $.session.usage()
  post($, {
    kind: 'complete',
    answer,
    reason,
    ...(context.percent !== undefined ? { pct: context.percent } : {}),
    elapsed_s: durationMs / 1000,
  })
}

export const register: Register = (on, options) => {
  on('session.start', async ($, e, next) => {
    const started = await next(e)
    await $.command.register({
      name: COMMAND,
      description: 'Talk to Claude hands-free: say the address word, talk, then the closer; /hands-free off stops',
    })
    if (!child) {
      // a reload: the old copy's line outlives it, and voice comes back if it was on
      $.ui.status(undefined)
      const { value: wasOn } = await $.state.get(LISTENING)
      if (e.isInteractive && (wasOn === true || options.autostart === true)) start($, e.cwd, options)
    }
    return started
  })

  on('command.run', { command: COMMAND }, async ($, e) => {
    const arg = e.args.trim().toLowerCase()
    if (arg === 'off' || (arg === '' && child)) {
      if (!child) return { text: 'Hands-free voice is not listening.' }
      await stop($)
      return { text: 'Hands-free voice stopping.' }
    }
    if (arg !== '' && arg !== 'on') return { text: 'Usage: /hands-free [on|off]' }
    if (child) return { text: 'Hands-free voice is already listening.' }
    start($, await $.session.cwd(), options)
    return { text: 'Hands-free voice starting.' }
  })

  on('turn.start', ($, e, next) => {
    turnId = e.turnId
    post($, { kind: 'start' })
    paint($)
    return next(e)
  })

  on('turn.step', async function* ($, e, next) {
    if (e.agentId === undefined) {
      // this request carries every row appended so far
      for (; unread > 0; unread -= 1) post($, { kind: 'read' })
    }
    const step = yield* next(e)
    if (e.agentId === undefined && step.answer) post($, { kind: 'text', text: step.answer })
    return step
  })

  on('tool.call', ($, e, next) => {
    // Feedback said by voice becomes a public issue only after the spoken
    // confirm; a message that begins with "feedback" by accident files nothing.
    if (child && e.tool === 'Bash') {
      const refused = refuseFiling(e.command, confirmation)
      if (refused) return { deny: refused }
    }
    if (e.agentId === undefined && socket) {
      post($, { kind: 'tool', name: String(e.tool), input: narrationInput(e as unknown as Record<string, unknown>) })
    }
    return next(e)
  })

  on('turn.complete', async ($, e, next) => {
    const done = await next(e)
    if (e.agentId !== undefined) return done
    turnId = undefined
    paint($)
    const wasUnread = unread > 0
    unread = 0
    if (socket) await complete($, e.answer, e.reason, e.durationMs)
    if (wasUnread && !e.isAborted) void $.prompt.submit({ text: UNREAD_NUDGE })
    return done
  })

  on('prompt.compose', async ($, e, next) => {
    const composed = await next(e)
    if (!contract) return composed
    return { sections: [...composed.sections, { id: SECTION, text: contract, scope: 'session' as const }] }
  })
}
