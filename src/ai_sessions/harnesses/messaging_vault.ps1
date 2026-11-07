# Static helper: all variable input, including credentials, arrives on stdin.
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$WarningPreference = 'SilentlyContinue'
$VerbosePreference = 'SilentlyContinue'
$DebugPreference = 'SilentlyContinue'
$InformationPreference = 'SilentlyContinue'
$pipe = $null
$writer = $null
$writing = $false
try {
    $request = [Console]::In.ReadToEnd() | ConvertFrom-Json
    if ($request.vault_name -cnotmatch '^ai-sessions-claude-[0-9a-f]{64}$') {
        throw 'Invalid reference'
    }
    Import-Module Microsoft.PowerShell.SecretManagement -ErrorAction Stop
    # Approved unattended unlock: Import-Clixml decrypts only for this Windows user.
    $passwordPath = Join-Path $HOME '.config/powershell/secrets/DevSecretsPassword.clixml'
    if (Test-Path -LiteralPath $passwordPath) {
        Import-Module Microsoft.PowerShell.SecretStore -ErrorAction Stop
        $vaultPassword = Import-Clixml -LiteralPath $passwordPath
        Unlock-SecretStore -Password $vaultPassword | Out-Null
        $vaultPassword = $null
    }
    switch ($request.operation) {
        'store' {
            if ([string]::IsNullOrEmpty($request.token)) { throw 'Missing token' }
            $secure = ConvertTo-SecureString -String $request.token -AsPlainText -Force
            Set-Secret -Name $request.vault_name -Vault DevSecrets -Secret $secure | Out-Null
            [Console]::Out.WriteLine('{"status":"ok"}')
        }
        'remove' {
            $entry = Get-SecretInfo -Name $request.vault_name -Vault DevSecrets -ErrorAction Stop
            if ($null -ne $entry) {
                Remove-Secret -Name $request.vault_name -Vault DevSecrets | Out-Null
            }
            [Console]::Out.WriteLine('{"status":"ok"}')
        }
        'send' {
            $address = [string]$request.address
            if ($address.StartsWith('uds:', [StringComparison]::Ordinal)) {
                $address = $address.Substring(4)
            }
            if (-not $address.StartsWith('\\.\pipe\', [StringComparison]::Ordinal)) {
                throw 'Invalid address'
            }
            $pipeName = $address.Substring(9)
            if ([string]::IsNullOrEmpty($pipeName)) { throw 'Invalid address' }
            $token = Get-Secret -Name $request.vault_name -Vault DevSecrets -AsPlainText
            if ($token -isnot [string] -or [string]::IsNullOrEmpty($token)) {
                throw 'Missing credential'
            }
            $pipe = [System.IO.Pipes.NamedPipeClientStream]::new(
                '.', $pipeName, [System.IO.Pipes.PipeDirection]::Out,
                [System.IO.Pipes.PipeOptions]::Asynchronous)
            $pipe.Connect(3000)
            $writer = [System.IO.StreamWriter]::new($pipe, [Text.UTF8Encoding]::new($false))
            $writer.NewLine = "`n"
            $auth = @{type = 'auth'; token = $token} | ConvertTo-Json -Compress
            $label = [string]$request.sender.name
            if ([string]::IsNullOrEmpty($label)) { $label = [string]$request.sender.session_id }
            $frame = @{
                type = 'user'
                message = @{role = 'user'; content = [string]$request.text}
                from = $label
                msg_id = [string]$request.message_id
            } | ConvertTo-Json -Depth 8 -Compress
            $writing = $true
            $writer.WriteLine($auth)
            $writer.WriteLine($frame)
            $writer.Flush()
            # Native receiver owns inbound policy. No policy configuration is changed.
            # This unidirectional pipe exposes no acceptance/hold/refusal receipt.
            [Console]::Out.WriteLine('{"status":"submitted"}')
        }
        default { throw 'Invalid operation' }
    }
} catch {
    if ($writing) {
        [Console]::Out.WriteLine('{"status":"uncertain"}')
    } else {
        [Console]::Out.WriteLine('{"status":"unavailable"}')
    }
} finally {
    # Dispose may flush; suppress failures and never expose exception/credential data.
    if ($null -ne $writer) { try { $writer.Dispose() } catch {} }
    if ($null -ne $pipe) { try { $pipe.Dispose() } catch {} }
    $token = $null
    $secure = $null
    $auth = $null
    $request = $null
    $vaultPassword = $null
}
