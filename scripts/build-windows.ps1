# 打包 Windows 自包含候选，工具路径由调用方提供。
[CmdletBinding()]
param(
    [string]$DotNet = 'dotnet',
    [string]$Python = 'python',
    [Parameter(Mandatory)][string]$MediaBin,
    [Parameter(Mandatory)][string]$OutputDir
)

$ErrorActionPreference = 'Stop'
$repo = Split-Path $PSScriptRoot -Parent
$backend = Join-Path $repo 'video-subtitler'
$symbolSource = Join-Path $repo 'mistdown/native-shell'
$project = Join-Path $repo 'mistdown/native-shell/Jiying.Desktop/Jiying.Desktop.csproj'
$env:DOTNET_GENERATE_ASPNET_CERTIFICATE = 'false'
. (Join-Path $PSScriptRoot 'dotnet-sdk.ps1')
$DotNet = Resolve-JiyingDotNet $DotNet $repo
$Python = (Get-Command $Python -CommandType Application -ErrorAction Stop).Source
$version = (Get-Content -LiteralPath (Join-Path $backend 'app/VERSION') -Raw -Encoding UTF8).Trim()
$MediaBin = (Resolve-Path -LiteralPath $MediaBin).Path
$OutputDir = (New-Item -ItemType Directory -Path $OutputDir -Force).FullName
$publish = Join-Path $OutputDir 'app'
if (Test-Path -LiteralPath $publish) { throw '请使用新的输出目录，以免旧文件混入包内。' }
$env:JIYING_MEDIA_BIN = $MediaBin
$env:PYTHONUTF8 = '1'
$env:DOTNET_NOLOGO = '1'
$env:PYINSTALLER_CONFIG_DIR = Join-Path $OutputDir 'pyinstaller-cache'
$env:JIYING_SQLITE_DLL = (& $Python (Join-Path $repo 'scripts/prepare-sqlite.py') (Join-Path $OutputDir 'dependencies'))
if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $env:JIYING_SQLITE_DLL -PathType Leaf)) {
    throw 'SQLite 依赖未通过校验。'
}

function Invoke-Checked([string]$Program, [string[]]$Arguments) {
    & $Program @Arguments
    if ($LASTEXITCODE -ne 0) { throw "$Program 执行失败，退出码 $LASTEXITCODE" }
}

Push-Location -LiteralPath $repo
try {
    Invoke-Checked $Python @('-m','PyInstaller',(Join-Path $backend 'JiyingEngine.spec'),'--noconfirm',
        '--distpath',(Join-Path $OutputDir 'frozen'),'--workpath',(Join-Path $OutputDir 'pyinstaller'))
    $engine = Join-Path $OutputDir 'frozen/JiyingEngine'
    $engineVersion = (Get-Content -LiteralPath (Join-Path $engine '_internal/app/VERSION') -Raw -Encoding UTF8).Trim()
    if ($engineVersion -ne $version) { throw '冻结引擎版本与源码不符。' }
    Invoke-Checked $DotNet @('publish',$project,'-c','Release','-r','win-x64','--self-contained','true',
        "-p:JiyingEngineDir=$engine",'-p:CopyOutputSymbolsToPublishDirectory=false','--nologo','-o',$publish)
} finally { Pop-Location }

$desktop = Join-Path $publish 'Jiying.Native.exe'
if ((Get-Item -LiteralPath $desktop).VersionInfo.FileVersion -ne $version) {
    throw '桌面程序版本与源码不符。'
}
$frozenEngine = Join-Path $publish 'engine/JiyingEngine.exe'
if (-not (Test-Path -LiteralPath $frozenEngine -PathType Leaf) -or
    (Get-Item -LiteralPath $frozenEngine).VersionInfo.FileVersion -ne $version) {
    throw '发布目录缺少配对引擎或引擎文件版本不符。'
}
foreach ($name in @('LICENSE', '第三方说明.md')) {
    Copy-Item -LiteralPath (Join-Path $repo $name) -Destination $publish
}
$debugFiles = @(Get-ChildItem -LiteralPath $publish -Recurse -File -Filter '*.pdb')
$symbols = (New-Item -ItemType Directory -Path (Join-Path $OutputDir 'symbols') -Force).FullName
foreach ($doc in Get-ChildItem -LiteralPath $publish -File -Filter 'Microsoft.Web.WebView2.*.xml') {
    Move-Item -LiteralPath $doc.FullName -Destination $symbols
}
if ($debugFiles.Count -gt 0) {
    foreach ($file in $debugFiles) {
        $relative = $file.FullName.Substring($publish.TrimEnd('\','/').Length + 1)
        $target = Join-Path $symbols $relative
        New-Item -ItemType Directory -Path (Split-Path $target -Parent) -Force | Out-Null
        Move-Item -LiteralPath $file.FullName -Destination $target
    }
}
foreach ($file in @(
    'Jiying.Core/bin/Release/net10.0/Jiying.Core.pdb',
    'Jiying.Desktop/bin/Release/net10.0-windows10.0.19041.0/win-x64/Jiying.Native.pdb'
)) {
    Copy-Item -LiteralPath (Join-Path $symbolSource $file) -Destination $symbols -Force
}
Write-Output "即影 $version Windows 包 $publish"
