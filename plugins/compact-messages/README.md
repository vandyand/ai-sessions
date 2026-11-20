# Compact session messages

Claude Code renders peer messages as `from HANDLE: message preview…` by default.
Press/click the row to expand the full message body, and again to collapse it.
This mod renders `UserMessage` and loads read-only handle labels on `session.start`. It does not rewrite model input,
transcripts, permissions, or message delivery. Normal user messages are unchanged.

New ai-sessions messages carry sender handles. Older messages use the startup handle map; unknown or ambiguous IDs
fall back to a short native address. Unknown/malformed messages use Claude's normal rendering.

Install the local marketplace, then the plugin:

```powershell
claude plugin marketplace add C:\Users\vandy\ai-sessions-compact-messages\plugins
claude plugin install sessions-compact-messages@ai-sessions-local --scope user
```

Run `/reload-plugins` in an existing Claude session. New sessions load it on start.
Validated with Claude Code 2.1.329 native plugin validation and renderer tests.

```powershell
claude plugin validate plugins/compact-messages
claude plugin test plugins/compact-messages
```
