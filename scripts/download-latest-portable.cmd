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

function Open-Release([string]$suffix) {
    $request = [Net.HttpWebRequest][Net.WebRequest]::Create($base + $suffix)
    $request.AllowAutoRedirect = $false
    $request.Timeout = 30000
    $request.ReadWriteTimeout = 30000
    $request.Headers['Authorization'] = 'Bearer ' + $token
    $request.Headers['X-Lanxi-Device'] = $device
    if ($certificatePin) {
        $request.Proxy = $null
        $request.ConnectionGroupName = $certificatePin
        $request.ServerCertificateValidationCallback = [LanxiReleaseTls]::ForCertificate($certificatePin)
    }
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

try {
    $config = Join-Path $env:LOCALAPPDATA 'Lanxi\data\local-config.json'
    if (![IO.File]::Exists($config)) { throw '当前 Windows 用户还未配对澜汐。请用已配对的用户运行。' }
    $coordination = ([IO.File]::ReadAllText($config) | ConvertFrom-Json).coordination
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
    if ($partial -and [IO.File]::Exists($partial)) { [IO.File]::Delete($partial) }
}
exit 0
