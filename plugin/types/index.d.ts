/** Whether voice should be on for this session: kept across a hot reload
 *  (editing the mod, changing its options), which stops the listener. */
export type Listening = boolean

declare module 'claude-code' {
  interface PluginState {
    'hands-free-voice': { listening: Listening }
  }
}
