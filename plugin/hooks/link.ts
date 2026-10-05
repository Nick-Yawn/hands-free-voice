// The listener's side of the wire, as plain functions: its stdout lines in,
// the events posted back to its socket out. No `$` here, so the tests run it
// as is.

/** What the listener's ears and voice are doing (VoiceFront.state()). */
export type ListenerState =
  | { type: 'state'; state: 'idle' | 'speaking' | 'paused' }
  | { type: 'state'; state: 'hearing'; words: string }
  | { type: 'state'; state: 'trouble'; detail: string }

/** One line of the listener's stdout (hands_free_voice/listen.py). */
export type ListenerLine =
  | {
      type: 'hello'
      socket: string
      contract: string
      address: string
      closer: string
      commands: string[]
      version?: string
    }
  | ListenerState
  | { type: 'turn'; text: string }
  | { type: 'compact' }
  | { type: 'confirm' }
  | { type: 'log'; text: string }
  | { type: 'error'; text: string }

/** One event posted to the listener's /event (hands_free_voice/bridge.py). */
export type ListenerEvent =
  | { kind: 'start' }
  | { kind: 'read' }
  | { kind: 'tool'; name: string; input: Record<string, string> }
  | { kind: 'text'; text: string }
  | { kind: 'complete'; answer: string; reason: string; pct?: number; elapsed_s: number }
  | { kind: 'compacted' }

/** The listener command as typed in the options, split on whitespace. */
export const splitArgv = (command: string): string[] => command.trim().split(/\s+/).filter(Boolean)

/** Turns stdout pieces into whole lines: a piece may end mid-line or hold several. */
export class LineSplitter {
  private rest = ''

  push(text: string): string[] {
    const parts = (this.rest + text).split('\n')
    this.rest = parts.pop() ?? ''
    return parts.filter(line => line.trim() !== '')
  }
}

const isString = (v: unknown): v is string => typeof v === 'string'

/** The spoken commands, one phrase each, when the listener's hello names none. */
export const COMMANDS = ['stop', 'resume', 'again', 'never mind', 'status', 'compact', 'quit']

/** A stdout line as a ListenerLine, or undefined for anything else. */
export const parseLine = (line: string): ListenerLine | undefined => {
  let obj: unknown
  try {
    obj = JSON.parse(line)
  } catch {
    return undefined
  }
  if (typeof obj !== 'object' || obj === null) return undefined
  const o = obj as Record<string, unknown>
  switch (o.type) {
    case 'hello':
      return isString(o.socket) && isString(o.contract)
        ? {
            type: 'hello',
            socket: o.socket,
            contract: o.contract,
            address: isString(o.address) ? o.address : 'operator',
            closer: isString(o.closer) ? o.closer : 'over',
            commands:
              Array.isArray(o.commands) && o.commands.length > 0 && o.commands.every(isString)
                ? o.commands
                : COMMANDS,
            ...(isString(o.version) ? { version: o.version } : {}),
          }
        : undefined
    case 'state':
      switch (o.state) {
        case 'idle':
        case 'speaking':
        case 'paused':
          return { type: 'state', state: o.state }
        case 'hearing':
          return { type: 'state', state: 'hearing', words: isString(o.words) ? o.words : '' }
        case 'trouble':
          return { type: 'state', state: 'trouble', detail: isString(o.detail) ? o.detail : 'trouble' }
        default:
          return undefined
      }
    case 'turn':
      return isString(o.text) ? { type: 'turn', text: o.text } : undefined
    case 'compact':
    case 'confirm':
      return { type: o.type }
    case 'log':
    case 'error':
      return isString(o.text) ? { type: o.type, text: o.text } : undefined
    default:
      return undefined
  }
}

/** The fields of a tool call that narration reads ("Editing config.py."):
 *  never the whole input, which can hold a file's contents. */
export const narrationInput = (input: Record<string, unknown>): Record<string, string> => {
  const out: Record<string, string> = {}
  for (const key of ['file_path', 'description', 'skill']) {
    const value = input[key]
    if (isString(value)) out[key] = value
  }
  return out
}

// The status line is a fixed-width slot, then every spoken command, always
// all of them: only the slot changes, so the commands never move.
//   waiting                 "operator [… over | stop | … | quit | feedback … over]"
//   ▸ weather in Houston    "operator [… over | stop | … | quit | feedback … over]"

/** The slot's width in columns. */
export const SLOT = 22

/** The spoken commands as usage: the address word, then one of them, feedback
 *  last. Its confirm is not shown: the read-back says when to say it. */
export const usage = (address: string, closer: string, commands: string[]): string =>
  `"${address} [… ${[closer, ...commands, `feedback … ${closer}`].join(' | ')}]"`

/** The last words heard that fit the slot, oldest dropped first; "…" marks a drop. */
export const fitWords = (words: string, width = SLOT): string => {
  const all = words.trim().split(/\s+/).filter(Boolean).slice(-3)
  if (all.length === 0) return 'hearing'
  const shown = (kept: string[]) => `▸ ${kept.length < all.length ? '…' : ''}${kept.join(' ')}`
  let kept = all
  while (kept.length > 1 && shown(kept).length > width) kept = kept.slice(1)
  const text = shown(kept)
  return text.length > width ? `${text.slice(0, width - 1)}…` : text
}

/** What the slot says: the listener's state, or "working" while a turn runs
 *  and the listener is otherwise idle. */
export const slotFor = (state: ListenerState, working: boolean): string => {
  switch (state.state) {
    case 'trouble':
      return state.detail
    case 'hearing':
      return fitWords(state.words)
    case 'paused':
    case 'speaking':
      return state.state
    case 'idle':
      return working ? 'working' : 'waiting'
  }
}

/** The whole line: the slot padded to its width, then the usage. */
export const statusLine = (slot: string, commands: string): string =>
  `${slot.slice(0, SLOT).padEnd(SLOT)}  ${commands}`

/** How a turn heard while Claude works is put into the running turn. */
export const midTurn = (text: string): string =>
  `The user said this by voice while you were working: ${text}`

/** The prompt that starts a turn when words delivered into the last one were never read. */
export const UNREAD_NUDGE =
  'The user spoke while you were finishing your last response; their words are just above. Answer them.'

/** Where feedback said by voice is filed. */
export const FEEDBACK_REPO = 'Nick-Yawn/hands-free-voice'

/** What the mod tells Claude when the user says the address word, then "confirm". */
export const CONFIRMED =
  'The user confirmed by voice: file the feedback issue you just read back to them, exactly as read. ' +
  'If you have not read one back, say there is nothing to confirm.'

/** Whether a Bash command would open an issue on the feedback repo: allowed
 *  only after the user's spoken confirm. */
export const filesFeedback = (command: string): boolean =>
  /\bgh\s+issue\s+create\b/.test(command) && command.toLowerCase().includes(FEEDBACK_REPO.toLowerCase())

/** Why a filing was refused without the confirm. */
export const UNCONFIRMED =
  'Feedback is filed only after the user confirms it by voice. Read the issue back in your voice block ' +
  '(the title and their quoted words), then ask them to say the address word followed by "confirm".'

/** The spoken confirm: it lets one feedback filing through, and only until
 *  the user says something else, so it answers just the draft read back. */
export class Confirmation {
  private given = false

  /** The user said the address word, then "confirm". */
  give(): void {
    this.given = true
  }

  /** Anything else heard: the draft the confirm answered is gone. */
  drop(): void {
    this.given = false
  }

  /** Whether a filing may go through now; one that does spends the confirm. */
  spend(): boolean {
    const ok = this.given
    this.given = false
    return ok
  }
}

/** The tool-call guard: why this command is refused, or undefined to let it run. */
export const refuseFiling = (command: string, confirmation: Confirmation): string | undefined =>
  filesFeedback(command) && !confirmation.spend() ? UNCONFIRMED : undefined

/** The system prompt's rule for "operator feedback … over": an ordinary heard
 *  turn whose first word is "feedback", read back, then filed by Claude as a
 *  GitHub issue once the user confirms it. A message can begin with
 *  "feedback" by accident, and the issue is public. */
export const feedbackRule = (version: string | undefined): string =>
  [
    '# Feedback by voice',
    '',
    'A message from the user that begins with the word "feedback" is feedback about hands-free-voice itself, ' +
      'not a task for this project. It becomes a public GitHub issue on ' +
      `${FEEDBACK_REPO}, so never file it straight away: a message can begin with "feedback" by accident.`,
    '',
    '1. Draft the issue:',
    '   - Title: a short summary in your own words.',
    '   - Body: their words after "feedback", quoted as heard, then one line: ' +
      `"Filed by voice with hands-free-voice ${version ?? '(unknown version)'} · Claude Code <\`claude --version\`> · <\`uname -sr\`>".`,
    '   - Nothing else from this session, the project or any log goes in it.',
    '2. Read it back in your voice block: the title, then their quoted words. Ask them to say the address word ' +
      'followed by "confirm" to file it, or anything else to drop it. Describe that, never quote it as one phrase.',
    `3. Only when the next message is their spoken confirmation, file it with \`gh issue create --repo ${FEEDBACK_REPO}\`. ` +
      'Any other next message drops the draft. A filing without the confirmation is refused.',
    '',
    'If `gh` is missing or not signed in, after the confirmation give them a ' +
      `https://github.com/${FEEDBACK_REPO}/issues/new link with the title and body filled in instead. ` +
      'Then say in your voice block what you filed, or why you could not.',
  ].join('\n')
