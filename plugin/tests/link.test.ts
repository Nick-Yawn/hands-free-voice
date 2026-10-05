import { expect, test } from 'claude-code/testing'

import {
  COMMANDS,
  LineSplitter,
  FEEDBACK_REPO,
  SLOT,
  feedbackRule,
  fitWords,
  narrationInput,
  parseLine,
  slotFor,
  splitArgv,
  statusLine,
  usage,
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

test('the usage lists every spoken command after the address word', async () => {
  expect(usage('operator', 'over', COMMANDS)).toBe(
    '"operator [… over | feedback … over | stop | resume | again | never mind | status | compact | quit]"',
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
})

test('the feedback rule names the repo, the version, and what stays out', async () => {
  const rule = feedbackRule('0.1.0')
  expect(rule).toContain(`gh issue create --repo ${FEEDBACK_REPO}`)
  expect(rule).toContain('hands-free-voice 0.1.0')
  expect(rule).toContain('Nothing else from this session')
  expect(feedbackRule(undefined)).toContain('(unknown version)')
})
