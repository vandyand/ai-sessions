# A layout-only pane is an ordinary shell, independent of its launching pane.
Remove-Item Env:AI_SESSIONS_WORKSPACE, Env:AI_SESSIONS_WORKSPACE_PANE -ErrorAction SilentlyContinue
# No Python, session launcher or picker. pwsh -NoExit leaves the prompt usable.
