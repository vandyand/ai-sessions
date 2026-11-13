[CmdletBinding()]
param([Parameter(Mandatory)][string]$ProfilePath)

$ErrorActionPreference = 'Stop'
$path = (Resolve-Path -LiteralPath $ProfilePath).Path
$text = [IO.File]::ReadAllText($path)
$updated = $text
foreach ($entry in @(@('cdba', 'codex'), @('cdsp', 'claude'))) {
    $name, $tool = $entry
    $pattern = '(?ms)(function\s+(?:global:)?' + [regex]::Escape($name) + '\s*\{\s*)' + [regex]::Escape($tool) + '(\s+[^\r\n{}]*@args\s*\})'
    $managed = '(?ms)function\s+(?:global:)?' + [regex]::Escape($name) + '\s*\{\s*sessions\s+new\s+' + [regex]::Escape($tool) + '\s+(?:''--''|--)\s+'
    if ([regex]::IsMatch($updated, $managed)) { continue }
    $matches = [regex]::Matches($updated, $pattern)
    if ($matches.Count -ne 1) {
        throw "Could not recognize the $name function. No profile changes were saved."
    }
    $replacement = '${1}sessions new ' + $tool + ' ''--''${2}'
    $updated = [regex]::Replace($updated, $pattern, $replacement)
}
if ($updated -ne $text) {
    $temporary = $path + '.managed-' + $PID
    try {
        [IO.File]::WriteAllText($temporary, $updated, [Text.UTF8Encoding]::new($false))
        if ([IO.File]::ReadAllText($path) -ne $text) { throw 'Profile changed during update; retry.' }
        [IO.File]::Replace($temporary, $path, [NullString]::Value)
    }
    finally {
        if (Test-Path -LiteralPath $temporary) { Remove-Item -LiteralPath $temporary }
    }
}
Write-Output 'cdba and cdsp now use sessions new. Reload this profile in existing PowerShell panes.'
