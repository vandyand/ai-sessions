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
do {
    foreach ($window in $element::RootElement.FindAll($scope::Children, $windowCondition)) {
        $panes = @($window.FindAll($scope::Descendants, $paneCondition) | Where-Object { -not $_.Current.IsOffscreen -and $_.Current.BoundingRectangle.Width -gt 0 })
        if (@($panes | Where-Object { $_.Current.HelpText -eq $Caller }).Count -ne 1) { continue }
        $result = @()
        foreach ($pane in $panes) {
            $bounds = $pane.Current.BoundingRectangle
            $title = $pane.Current.HelpText
            # Do not return titles, terminal buffers, or conversation content.
            $marker = if ($title.StartsWith($Prefix + '-')) { $title } else { '' }
            $result += @{marker=$marker; x=$bounds.X; y=$bounds.Y; width=$bounds.Width; height=$bounds.Height}
        }
        ConvertTo-Json -InputObject @($result) -Compress
        exit 0
    }
    Start-Sleep -Milliseconds 100
} while ([DateTime]::UtcNow -lt $deadline)
exit 2
