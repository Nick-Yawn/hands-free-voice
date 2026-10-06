import { expect, test } from 'claude-code/testing'

import {
  COMMANDS,
  Confirmation,
  LineSplitter,
  FEEDBACK_REPO,
  UNCONFIRMED,
  SLOT,
  VOICE_BACK_ON,
  VOICE_OFF,
  contractRow,
  contractStep,
  feedbackRule,
  fitWords,
  hasVoiceBlock,
  narrationInput,
  parseLine,
  refuseFiling,
  slotFor,
  splitArgv,
  statusLine,
  usage,
  workingArgs,
} from '../hooks/link'

test('stdout pieces become whole lines across and within pieces', async () => {
  const lines = new LineSplitter()
  expect(lines.push('{"type":"tu')).toEqual([])
  expect(lines.push('rn","text":"hi"}\n{"type":"compact"}\n\n{"ty')).toEqual([
    '{"type":"turn","text":"hi"}',
    '{"type":"compact"}',
  ])
  expect(lines.push('pe":"log","text":"x"}\n')).toEqual(['{"type":"log","text":"x"}'])
})

test('only well-formed listener lines parse', async () => {
  expect(parseLine('{"type":"turn","text":"run the tests"}')).toEqual({ type: 'turn', text: 'run the tests' })
  expect(parseLine('{"type":"hello","socket":"/s","contract":"c"}')).toEqual({
    type: 'hello',
    socket: '/s',
    contract: 'c',
    address: 'operator',
    closer: 'over',
    commands: COMMANDS,
  })
  expect(parseLine('{"type":"state","state":"hearing","words":"run the tests"}')).toEqual({
    type: 'state',
    state: 'hearing',
    words: 'run the tests',
  })
  expect(parseLine('{"type":"state","state":"speaking"}')).toEqual({ type: 'state', state: 'speaking' })
  expect(parseLine('{"type":"confirm"}')).toEqual({ type: 'confirm' })
  expect(parseLine('{"type":"state","state":"dancing"}')).toBeUndefined()
  expect(parseLine('{"type":"turn"}')).toBeUndefined()
  expect(parseLine('{"type":"hello","socket":"/s"}')).toBeUndefined()
  expect(parseLine('Traceback (most recent call last):')).toBeUndefined()
  expect(parseLine('42')).toBeUndefined()
})

test('narration gets the fields it reads and never a file body', async () => {
  expect(narrationInput({ file_path: '/a/b.py', content: 'secret', description: 7 })).toEqual({
    file_path: '/a/b.py',
  })
})

test('the command splits on whitespace', async () => {
  expect(splitArgv('  uvx --from git+https://x/y  hands-free-voice listen ')).toEqual([
    'uvx',
    '--from',
    'git+https://x/y',
    'hands-free-voice',
    'listen',
  ])
})

test('the usage lists the spoken commands after the address word, feedback last', async () => {
  expect(usage('operator', 'over', COMMANDS)).toBe(
    '"operator [… over | stop | resume | again | never mind | status | compact | quit | feedback … over]"',
  )
})

test('the slot keeps the last words that fit, oldest dropped first', async () => {
  expect(fitWords('weather in Houston')).toBe('▸ weather in Houston')
  expect(fitWords('the integration tests')).toBe('▸ …integration tests')
  expect(fitWords('')).toBe('hearing')
  expect(fitWords('supercalifragilisticexpialidocious')).toHaveLength(SLOT)
})

test('the slot says what is happening, working only while the listener is idle', async () => {
  expect(slotFor({ type: 'state', state: 'idle' }, false)).toBe('waiting')
  expect(slotFor({ type: 'state', state: 'idle' }, true)).toBe('working')
  expect(slotFor({ type: 'state', state: 'speaking' }, true)).toBe('speaking')
  expect(slotFor({ type: 'state', state: 'trouble', detail: 'mic lost, rebuilding' }, true)).toBe(
    'mic lost, rebuilding',
  )
})

test('the status line is the same length whatever the slot says', async () => {
  const commands = usage('operator', 'over', COMMANDS)
  const lengths = ['waiting', '▸ weather in Houston', 'mic silent, rebuilding', 'x'.repeat(40)].map(
    slot => statusLine(slot, commands).length,
  )
  expect(new Set(lengths).size).toBe(1)
  expect(statusLine('waiting', commands).endsWith('· /config for settings')).toBe(true)
})

test('the feedback rule reads the draft back and files it only on the confirm', async () => {
  const rule = feedbackRule('0.1.0')
  expect(rule).toContain(`gh issue create --repo ${FEEDBACK_REPO}`)
  expect(rule).toContain('hands-free-voice 0.1.0')
  expect(rule).toContain('Nothing else from this session')
  expect(rule).toContain('never file it straight away')
  expect(rule).toContain('Read it back in your voice block')
  expect(rule).toContain('Any other next message drops the draft')
  expect(feedbackRule(undefined)).toContain('(unknown version)')
})

const FILING = `gh issue create --repo ${FEEDBACK_REPO} --title "x" --body "y"`

test('no feedback issue is filed without the spoken confirm', async () => {
  expect(refuseFiling(FILING, new Confirmation())).toBe(UNCONFIRMED)
  expect(refuseFiling('gh issue create -R nick-yawn/Hands-Free-Voice --title x', new Confirmation())).toBe(
    UNCONFIRMED,
  )
})

test('one confirm lets one filing through', async () => {
  const confirmation = new Confirmation()
  confirmation.give()
  expect(refuseFiling(FILING, confirmation)).toBeUndefined()
  expect(refuseFiling(FILING, confirmation)).toBe(UNCONFIRMED)
})

test('anything heard after the confirm voids it', async () => {
  const confirmation = new Confirmation()
  confirmation.give()
  confirmation.drop()
  expect(refuseFiling(FILING, confirmation)).toBe(UNCONFIRMED)
})

test('other commands run untouched and leave the confirm unspent', async () => {
  const confirmation = new Confirmation()
  confirmation.give()
  expect(refuseFiling(`gh issue list --repo ${FEEDBACK_REPO}`, confirmation)).toBeUndefined()
  expect(refuseFiling('gh issue create --repo someone/else --title x', confirmation)).toBeUndefined()
  expect(refuseFiling('git status', confirmation)).toBeUndefined()
  expect(refuseFiling(FILING, confirmation)).toBeUndefined()
})

test('voice coming on puts the whole contract in once', async () => {
  const on = contractStep('absent', 'on', 'C')
  expect(on.state).toBe('active')
  expect(on.row).toBe(contractRow('C'))
  expect(on.row).toContain('Hands-free voice is on.')
  expect(on.row?.endsWith('\n\nC')).toBe(true)
  // a reload's hello, or every turn's start: already in force, nothing more
  expect(contractStep('active', 'on', 'C')).toEqual({ state: 'active' })
})

test('voice going off puts it aside, and coming back points at it', async () => {
  expect(contractStep('active', 'off', 'C')).toEqual({ state: 'paused', row: VOICE_OFF })
  expect(contractStep('paused', 'on', 'C')).toEqual({ state: 'active', row: VOICE_BACK_ON })
  // off twice (a listener that never started) says it once
  expect(contractStep('paused', 'off', 'C')).toEqual({ state: 'paused' })
  expect(contractStep('absent', 'off', 'C')).toEqual({ state: 'absent' })
})

test('a compaction loses it, and the next start puts all of it back', async () => {
  for (const state of ['active', 'paused', 'absent'] as const) {
    expect(contractStep(state, 'lost', 'C')).toEqual({ state: 'absent' })
  }
  expect(contractStep('absent', 'on', 'C').row).toBe(contractRow('C'))
})

test('a missing block puts the whole contract in again', async () => {
  expect(contractStep('active', 'missed', 'C')).toEqual({ state: 'active', row: contractRow('C') })
  expect(hasVoiceBlock('done.\n⟦voice⟧All set.⟦/voice⟧')).toBe(true)
  expect(hasVoiceBlock('done, no block')).toBe(false)
})

test('the working cue options reach the listener as flags, the volume as a share', async () => {
  expect(workingArgs({ working_cue: 'word', working_cue_interval: 20, working_tone_volume: 10 })).toEqual([
    '--working-cue', 'word', '--working-interval', '20', '--working-tone-volume', '0.1',
  ])
  expect(workingArgs({ autostart: true })).toEqual([])
})
