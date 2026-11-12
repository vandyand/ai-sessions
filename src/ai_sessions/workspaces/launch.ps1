[CmdletBinding()]
param(
    [Parameter(Mandatory)][ValidatePattern('^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$')][string]$Workspace,
    [Parameter(Mandatory)][ValidatePattern('^pane-[0-9]+$')][string]$Pane,
    [Parameter(Mandatory)][string]$WorkspaceFile,
    [Parameter(Mandatory)][string]$StateFile,
    [Parameter(Mandatory)][string]$Python
)

# Only this process and its children inherit the managed pane context.
$env:AI_SESSIONS_WORKSPACE = $Workspace
$env:AI_SESSIONS_WORKSPACE_PANE = $Pane
$env:AI_SESSIONS_WORKSPACE_FILE = $WorkspaceFile
$env:AI_SESSIONS_STATE_FILE = $StateFile
try {
    & $Python -m ai_sessions workspace run $Workspace $Pane
}
catch {
    Write-Error -ErrorRecord $_ -ErrorAction Continue
}
# No exit: pwsh -NoExit returns to an ordinary interactive prompt.
