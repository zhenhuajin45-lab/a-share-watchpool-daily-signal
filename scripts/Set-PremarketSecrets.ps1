[CmdletBinding()]
param(
    [switch]$PersistUserEnvironment
)

$ErrorActionPreference = "Stop"

function ConvertFrom-SecureValue {
    param([Security.SecureString]$Value)
    $pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($Value)
    try {
        return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer)
    }
    finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer)
    }
}

$gmSecure = Read-Host "GM_TOKEN（输入不可见）" -AsSecureString
$deepSeekSecure = Read-Host "DEEPSEEK_API_KEY（输入不可见）" -AsSecureString
$gmValue = ConvertFrom-SecureValue $gmSecure
$deepSeekValue = ConvertFrom-SecureValue $deepSeekSecure
try {
    $env:GM_TOKEN = $gmValue
    $env:DEEPSEEK_API_KEY = $deepSeekValue
    if ($PersistUserEnvironment) {
        [Environment]::SetEnvironmentVariable("GM_TOKEN", $gmValue, "User")
        [Environment]::SetEnvironmentVariable("DEEPSEEK_API_KEY", $deepSeekValue, "User")
    }
}
finally {
    $gmValue = $null
    $deepSeekValue = $null
}

Write-Host "凭据已注入当前 PowerShell 进程；未写入项目、日志或命令行。"
if ($PersistUserEnvironment) {
    Write-Host "同时已写入当前 Windows 用户环境变量，后续新启动的任务计划进程可读取。"
}
