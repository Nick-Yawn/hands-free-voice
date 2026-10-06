/** Whether voice should be on for this session: kept across a hot reload
 *  (editing the mod, changing its options), which stops the listener. */
export type Listening = boolean

/** Where the voice contract stands in the conversation, which keeps it as a
 *  message: in force, put aside by a "voice is off" note, or not there (never
 *  put in, or compacted away). Kept across a hot reload, like the conversation. */
export type ContractState = 'absent' | 'active' | 'paused'

declare module 'claude-code' {
  interface PluginState {
    'hands-free-voice': { listening: Listening; contract: ContractState }
  }
}
