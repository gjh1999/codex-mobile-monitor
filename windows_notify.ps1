param([string]$Title='Codex monitor',[string]$Body='Monitor notification')
$ErrorActionPreference='Stop'
try {
 [Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType=WindowsRuntime] | Out-Null
 $xml=New-Object Windows.Data.Xml.Dom.XmlDocument
 $xml.LoadXml('<toast><visual><binding template="ToastGeneric"><text></text><text></text></binding></visual></toast>')
 $nodes=$xml.GetElementsByTagName('text')
 $nodes.Item(0).InnerText=$Title
 $nodes.Item(1).InnerText=$Body
 $toast=[Windows.UI.Notifications.ToastNotification]::new($xml)
 [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('Microsoft.Windows.PowerShell').Show($toast)
 Write-Output 'toast_api_called'
} catch {
 Add-Type -AssemblyName System.Windows.Forms
 Add-Type -AssemblyName System.Drawing
 $icon=New-Object System.Windows.Forms.NotifyIcon
 try {
  $icon.Icon=[System.Drawing.SystemIcons]::Information
  $icon.Visible=$true
  $icon.ShowBalloonTip(5000,$Title,$Body,[System.Windows.Forms.ToolTipIcon]::Info)
  Start-Sleep -Seconds 6
  Write-Output 'balloon_api_called'
 } finally {$icon.Dispose()}
}
