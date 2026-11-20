# Compact session messages

Claude Code renders peer messages as `from HANDLE: message preview…` by default.
Press/click the row to expand the full message body, and again to collapse it.
This mod hooks only `ui.render` for `UserMessage`. It does not rewrite model input,
transcripts, permissions, or message delivery. Normal user messages are unchanged.

New ai-sessions messages carry sender handles. Older messages without that field
show a short native address. Unknown/malformed messages use Claude's normal rendering.

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
