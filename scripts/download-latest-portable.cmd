@echo off
setlocal DisableDelayedExpansion
set "LANXI_DOWNLOADER=%~f0"
powershell.exe -NoLogo -NoProfile -Command "$source=[IO.File]::ReadAllText($env:LANXI_DOWNLOADER,[Text.Encoding]::UTF8) -split '(?m)^:LANXI_SCRIPT\r?$',2; & ([ScriptBlock]::Create($source[1]))"
set "result=%errorlevel%"
if /i not "%~1"=="--no-pause" pause
exit /b %result%
:LANXI_SCRIPT
$ErrorActionPreference = 'Stop'
$partial = $null
$pairLock = $null
$workspaceGrant = '__LANXI_WORKSPACE_GRANT__'

function New-Request([string]$address) {
    $request = [Net.HttpWebRequest][Net.WebRequest]::Create($address)
    $request.AllowAutoRedirect = $false
    $request.Timeout = 30000
    $request.ReadWriteTimeout = 30000
    if ($certificatePin) {
        $request.Proxy = $null
        $request.ConnectionGroupName = $certificatePin
        $request.ServerCertificateValidationCallback = [LanxiReleaseTls]::ForCertificate($certificatePin)
    }
    return $request
}

function Open-Release([string]$suffix) {
    $request = New-Request ($base + $suffix)
    $request.Headers['Authorization'] = 'Bearer ' + $token
    $request.Headers['X-Lanxi-Device'] = $device
    $response = $request.GetResponse()
    if ([int]$response.StatusCode -ne 200) {
        $response.Dispose()
        throw '更新服务器返回了非预期状态。'
    }
    return $response
}

function Confirm-Package([string]$path) {
    (Get-Item -LiteralPath $path).Length -eq $size -and
        (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash -eq $sha
}

function Save-Json([string]$path, $value) {
    $tmp = $path + '.' + [guid]::NewGuid().ToString('N') + '.tmp'
    try {
        [IO.File]::WriteAllText($tmp, ($value | ConvertTo-Json -Depth 100 -Compress))
        if ([IO.File]::Exists($path)) {
            [IO.File]::Replace($tmp, $path, [System.Management.Automation.Language.NullString]::Value)
        } else { [IO.File]::Move($tmp, $path) }
    } finally { if ([IO.File]::Exists($tmp)) { [IO.File]::Delete($tmp) } }
}

function Read-Settings {
    if (![IO.File]::Exists($config)) { return [pscustomobject]@{} }
    $settings = [IO.File]::ReadAllText($config) | ConvertFrom-Json
    if ($settings -isnot [pscustomobject]) { throw '本机配置格式有误，请检查 local-config.json。' }
    return $settings
}

try {
    $config = Join-Path $env:LOCALAPPDATA 'Lanxi\data\local-config.json'
    $settings = Read-Settings
    $coordination = $settings.coordination
    $claim = $null
    if (!($coordination.device -and ($coordination.protectedToken -or $env:LANXI_DEVICE_TOKEN))) {
        if ($workspaceGrant -cnotmatch '^LANXI3\.([A-Za-z0-9_-]+)$') {
            throw '请使用私人发行目录「笔记本下载」中的「下载澜汐最新版.cmd」完成首次配对。'
        }
        $encoded = $Matches[1].Replace('-', '+').Replace('_', '/')
        $encoded += '=' * ((4 - $encoded.Length % 4) % 4)
        $grant = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($encoded)) | ConvertFrom-Json
        if ($grant.code -cnotmatch '\A[A-Za-z0-9_-]{43}\z') { throw '私人下载脚本的加入授权格式有误。' }
        $local = Join-Path ([IO.Path]::GetDirectoryName($config)) '.local'
        [IO.Directory]::CreateDirectory($local) | Out-Null
        $pairLock = [IO.File]::Open((Join-Path $local 'download-pairing.lock'), 'OpenOrCreate', 'ReadWrite', 'None')
        $settings = Read-Settings
        $coordination = $settings.coordination
        if (!($coordination.device -and ($coordination.protectedToken -or $env:LANXI_DEVICE_TOKEN))) {
            $pending = Join-Path $local 'download-pairing.json'
            $key = [Security.Cryptography.SHA256]::Create()
            try { $grantHash = [Convert]::ToBase64String($key.ComputeHash([Text.Encoding]::ASCII.GetBytes($workspaceGrant))) }
            finally { $key.Dispose() }
            $identity = $null
            if ([IO.File]::Exists($pending)) { $identity = [IO.File]::ReadAllText($pending) | ConvertFrom-Json }
            if ($identity.grantHash -cne $grantHash) {
                Add-Type -AssemblyName System.Security
                $rng = [Security.Cryptography.RandomNumberGenerator]::Create()
                $bytes = [byte[]]::new(32)
                try {
                    $rng.GetBytes($bytes)
                    $ascii = [Text.Encoding]::ASCII.GetBytes([Convert]::ToBase64String($bytes).TrimEnd('=').Replace('+','-').Replace('/','_'))
                    $protected = [Security.Cryptography.ProtectedData]::Protect($ascii, $null, 'CurrentUser')
                } finally { $rng.Dispose(); [Array]::Clear($bytes,0,$bytes.Length); if ($ascii) { [Array]::Clear($ascii,0,$ascii.Length) } }
                $identity = @{ grantHash=$grantHash; device='lanxi-' + [guid]::NewGuid().ToString('N').Substring(0,24); protectedToken=[Convert]::ToBase64String($protected) }
                Save-Json $pending $identity
            }
            $projects = $coordination.projects
            if (!$projects) { $projects = @{} }
            $coordination = @{ url=$grant.url; device=$identity.device; protectedToken=$identity.protectedToken; projects=$projects }
            if ($grant.trustedCertificate) { $coordination.trustedCertificate = $grant.trustedCertificate }
            $claim = $grant.code
        }
    }
    $origin = $null
    if (![Uri]::TryCreate([string]$coordination.url, [UriKind]::Absolute, [ref]$origin) -or
        $origin.Scheme -ne 'https' -or $origin.UserInfo -or $origin.Query -or
        $origin.Fragment -or $origin.AbsolutePath -ne '/') {
        throw '请先在澜汐中完成 HTTPS 云配对。'
    }
    $device = [string]$coordination.device
    if ($device -cnotmatch '\A[A-Za-z0-9_-]{1,64}\z') { throw '本机配对设备标识有误。' }
    $token = $env:LANXI_DEVICE_TOKEN
    if ($coordination.protectedToken) {
        Add-Type -AssemblyName System.Security
        $secret = [Security.Cryptography.ProtectedData]::Unprotect(
            [Convert]::FromBase64String($coordination.protectedToken), $null, 'CurrentUser')
        try { $token = [Text.Encoding]::ASCII.GetString($secret) }
        finally { [Array]::Clear($secret, 0, $secret.Length) }
    }
    if ($token -cnotmatch '\A[A-Za-z0-9_-]{16,256}\z') {
        throw '未找到有效的云配对凭据。请先在澜汐中完成配对。'
    }

    $certificatePin = $null
    if ($coordination.trustedCertificate) {
        $pem = [regex]::Match($coordination.trustedCertificate,
            '\A\s*-----BEGIN CERTIFICATE-----\s*([A-Za-z0-9+/=\s]+?)\s*-----END CERTIFICATE-----\s*\z')
        if (!$pem.Success) { throw '配对证书格式有误。' }
        $sha256 = [Security.Cryptography.SHA256]::Create()
        try {
            $certificatePin = [Convert]::ToBase64String($sha256.ComputeHash(
                [Convert]::FromBase64String($pem.Groups[1].Value)))
        } finally { $sha256.Dispose() }
        Add-Type -TypeDefinition @'
using System;
using System.Net.Security;
using System.Security.Cryptography;
using System.Security.Cryptography.X509Certificates;

public static class LanxiReleaseTls
{
    public static RemoteCertificateValidationCallback ForCertificate(string expected)
    {
        return (sender, cert, chain, errors) => {
            if (cert == null || (errors & ~SslPolicyErrors.RemoteCertificateChainErrors) != SslPolicyErrors.None)
                return false;
            using (var presented = new X509Certificate2(cert))
            using (var hash = SHA256.Create()) {
                var now = DateTime.Now;
                return now >= presented.NotBefore && now <= presented.NotAfter
                    && Convert.ToBase64String(hash.ComputeHash(presented.RawData)) == expected;
            }
        };
    }
}
'@
    }
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    if ($claim) {
        Write-Host '正在将这台电脑加入私人工作区…'
        $request = New-Request ($origin.GetLeftPart([UriPartial]::Authority) + '/api/v1/pairing/join-workspace')
        $request.Method = 'POST'
        $request.ContentType = 'application/json'
        $payload = [Text.Encoding]::UTF8.GetBytes((@{code=$claim; device=$device; token=$token} | ConvertTo-Json -Compress))
        $request.ContentLength = $payload.Length
        $stream = $request.GetRequestStream()
        try { $stream.Write($payload, 0, $payload.Length) } finally { $stream.Dispose() }
        $reply = $request.GetResponse()
        try {
            $reader = [IO.StreamReader]::new($reply.GetResponseStream())
            try { $paired = $reader.ReadToEnd() | ConvertFrom-Json } finally { $reader.Dispose() }
            if ([int]$reply.StatusCode -ne 200 -or $paired.device -cne $device) { throw '服务器未确认本机配对，请重试。' }
        } finally { $reply.Dispose() }
        $settings = Read-Settings
        if ($settings.coordination.device -and ($settings.coordination.protectedToken -or $env:LANXI_DEVICE_TOKEN)) { throw '本机配对设置已更新，请重新运行下载脚本。' }
        $settings | Add-Member -Force NoteProperty coordination $coordination
        $sync = $settings.personalSync
        if (!$sync) { $sync = [pscustomobject]@{} }
        $sync | Add-Member -Force NoteProperty exchange $null
        $sync | Add-Member -Force NoteProperty codeOnly $false
        $sync | Add-Member -Force NoteProperty memory $true
        $settings | Add-Member -Force NoteProperty personalSync $sync
        Save-Json $config $settings
        [IO.File]::Delete($pending)
        Write-Host '本机已完成配对。'
    }
    if ($pairLock) { $pairLock.Dispose(); $pairLock = $null }
    $base = $origin.GetLeftPart([UriPartial]::Authority) + '/api/v1/releases/windows'
    Write-Host '正在查询服务器上的最新版本…'
    $response = Open-Release ''
    try {
        $reader = [IO.StreamReader]::new($response.GetResponseStream())
        try {
            $chars = [char[]]::new(4097)
            $count = $reader.ReadBlock($chars, 0, $chars.Length)
            if ($count -gt 4096) { throw '更新清单过大。' }
            $manifest = [string]::new($chars, 0, $count) | ConvertFrom-Json
        } finally { $reader.Dispose() }
    } finally { $response.Dispose() }
    $version = $manifest.version
    $sha = $manifest.sha256
    $size = $manifest.size
    if ($version -isnot [string] -or $version -cnotmatch '\A[1-9][0-9]{0,5}\z' -or
        $sha -isnot [string] -or $sha -cnotmatch '\A[0-9a-f]{64}\z' -or
        ($size -isnot [int] -and $size -isnot [long]) -or $size -le 0 -or $size -gt 160MB) {
        throw '服务器更新清单格式有误。'
    }

    $folder = [IO.Path]::GetDirectoryName($env:LANXI_DOWNLOADER)
    $archive = "Lanxi-R$version-Windows-Private.zip"
    $destination = Join-Path $folder $archive
    if ([IO.File]::Exists($destination)) {
        if (!(Confirm-Package $destination)) { throw "已有同名文件但校验失败，请移开后重试。$destination" }
        Write-Host "本地已是最新版安装包，校验通过。$destination"
        exit 0
    }

    $partial = Join-Path $folder ($archive + '.' + [guid]::NewGuid().ToString('N') + '.part')
    Write-Host "正在下载澜汐 R$version…"
    $response = Open-Release "/$version"
    try {
        $inputStream = $response.GetResponseStream()
        $file = [IO.File]::Open($partial, 'CreateNew', 'Write', 'None')
        try {
            $buffer = [byte[]]::new(128KB)
            $received = 0L
            while (($count = $inputStream.Read($buffer, 0, $buffer.Length)) -gt 0) {
                $received += $count
                if ($received -gt $size) { throw '实际下载大小超过更新清单。' }
                $file.Write($buffer, 0, $count)
                Write-Progress -Activity "下载澜汐 R$version" -PercentComplete ([int](100 * $received / $size))
            }
        } finally { $file.Dispose() }
    } finally { $response.Dispose() }
    Write-Progress -Activity "下载澜汐 R$version" -Completed
    if (!(Confirm-Package $partial)) { throw '安装包大小或 SHA-256 校验失败，请重试。' }
    [IO.File]::Move($partial, $destination)
    Write-Host "下载完成，SHA-256 校验通过。$destination"
    Write-Host '退出旧版澜汐后，解压 ZIP 并双击新版程序。'
} catch {
    [Console]::Error.WriteLine($_.Exception.Message)
    exit 1
} finally {
    if ($pairLock) { $pairLock.Dispose() }
    if ($partial -and [IO.File]::Exists($partial)) { [IO.File]::Delete($partial) }
}
exit 0
