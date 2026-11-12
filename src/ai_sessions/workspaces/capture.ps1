param([Parameter(Mandatory)][string]$Prefix, [Parameter(Mandatory)][string]$Caller)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
$element = [System.Windows.Automation.AutomationElement]
$scope = [System.Windows.Automation.TreeScope]
$windowCondition = New-Object System.Windows.Automation.PropertyCondition($element::ClassNameProperty, 'CASCADIA_HOSTING_WINDOW_CLASS')
$paneCondition = New-Object System.Windows.Automation.PropertyCondition($element::ClassNameProperty, 'TermControl')
$deadline = [DateTime]::UtcNow.AddSeconds(6)
$selected = $null
$known = @{}
$stableIds = $null
$sampleUntil = $null
do {
    $windows = if ($null -eq $selected) { @($element::RootElement.FindAll($scope::Children, $windowCondition)) } else { @($selected) }
    foreach ($window in $windows) {
        if ($null -ne $selected -and $window.Current.NativeWindowHandle -ne $selected.Current.NativeWindowHandle) { continue }
        $panes = @($window.FindAll($scope::Descendants, $paneCondition) | Where-Object { -not $_.Current.IsOffscreen -and $_.Current.BoundingRectangle.Width -gt 0 })
        if ($null -eq $selected) {
            if (@($panes | Where-Object { $_.Current.HelpText.TrimEnd('~') -eq $Caller }).Count -ne 1) { continue }
            $selected = $window
            $stableIds = @($panes | ForEach-Object { $_.GetRuntimeId() -join ',' } | Sort-Object) -join ';'
            $sampleUntil = [DateTime]::UtcNow.AddSeconds(1.5)
        }
        $ids = @($panes | ForEach-Object { $_.GetRuntimeId() -join ',' } | Sort-Object) -join ';'
        if ($ids -ne $stableIds) { throw 'Pane layout changed during capture. Retry saving.' }
        foreach ($pane in $panes) {
            $id = $pane.GetRuntimeId() -join ','
            $bounds = $pane.Current.BoundingRectangle
            $title = $pane.Current.HelpText.TrimEnd('~')
            if (-not $known.ContainsKey($id)) {
                $known[$id] = @{marker=''; x=$bounds.X; y=$bounds.Y; width=$bounds.Width; height=$bounds.Height}
            }
            # Accumulate only exact nonce identities against stable UIA runtime
            # IDs, never infer session identity from a title or coordinates.
            if ($title.StartsWith($Prefix + '-')) {
                if ($known[$id].marker -and $known[$id].marker -ne $title) { throw 'Pane identity changed during capture.' }
                $known[$id].marker = $title
            }
        }
        $missing = @($known.Values | Where-Object { -not $_.marker }).Count
        if ($missing -eq 0 -or [DateTime]::UtcNow -ge $sampleUntil) {
            $result = @($panes | ForEach-Object { $known[$_.GetRuntimeId() -join ','] })
            ConvertTo-Json -InputObject $result -Compress
            exit 0
        }
    }
    Start-Sleep -Milliseconds 30
} while ([DateTime]::UtcNow -lt $deadline)
exit 2
