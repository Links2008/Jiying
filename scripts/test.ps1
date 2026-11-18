# 在 Windows 上从仓库根运行。测试日志及工作目录由 OutputDir 隔离。
[CmdletBinding()]
param(
    [ValidateSet('all', 'desktop', 'backend')][string]$Suite = 'all',
    [string]$DotNet = 'dotnet',
    [string]$Python = 'python',
    [string]$Ffmpeg,
    [string]$Aria2,
    [string]$OutputDir,
    [ValidateSet('both', 'release', 'system')][string]$SqliteMode = 'both'
)

$ErrorActionPreference = 'Stop'
$repo = Split-Path $PSScriptRoot -Parent
$env:DOTNET_GENERATE_ASPNET_CERTIFICATE = 'false'
if ($Suite -ne 'backend') {
    . (Join-Path $PSScriptRoot 'dotnet-sdk.ps1')
    $DotNet = Resolve-JiyingDotNet $DotNet $repo
}
if ($Suite -ne 'desktop') { $Python = (Get-Command $Python -CommandType Application -ErrorAction Stop).Source }
if (-not $OutputDir) {
    $OutputDir = Join-Path ([IO.Path]::GetTempPath()) ('jiying-tests-' + [Guid]::NewGuid().ToString('N'))
}
$OutputDir = (New-Item -ItemType Directory -Path $OutputDir -Force).FullName
$isolated = (New-Item -ItemType Directory -Path (Join-Path $OutputDir ([Guid]::NewGuid().ToString('N'))) -Force).FullName
$env:PYTHONUTF8 = '1'
$env:DOTNET_NOLOGO = '1'
$env:HF_HUB_OFFLINE = '1'
$env:HF_HUB_DISABLE_IMPLICIT_TOKEN = '1'
$env:TRANSFORMERS_OFFLINE = '1'
$env:LOCALAPPDATA = $isolated
$env:APPDATA = $isolated
$env:XDG_CACHE_HOME = Join-Path $isolated 'cache'
$env:HF_HOME = Join-Path $isolated 'hf'
$env:TEMP = $isolated
$env:TMP = $isolated
$env:JIYING_UI_SETTINGS_PATH = Join-Path $isolated 'native-ui.json'
$env:JIYING_PLATFORM_PROFILE = Join-Path $isolated 'webview'
$env:DOTNET_BUNDLE_EXTRACT_BASE_DIR = Join-Path $isolated 'dotnet-bundle'

function Invoke-Checked([string]$Program, [string[]]$Arguments, [string]$LogName) {
    if ($LogName) {
        # Windows PowerShell 把合并的原生命令 stderr 包装成错误记录。
        # 子进程成功与否统一由退出码决定。
        $ErrorActionPreference = 'Continue'
        & $Program @Arguments 2>&1 | ForEach-Object { $_.ToString() } |
            Tee-Object -FilePath (Join-Path $OutputDir $LogName)
    } else {
        & $Program @Arguments
    }
    if ($LASTEXITCODE -ne 0) { throw "$Program 执行失败，退出码 $LASTEXITCODE" }
}

Push-Location -LiteralPath $repo
try {
    if ($Suite -ne 'desktop') {
        if (-not $Ffmpeg) { $Ffmpeg = (Get-Command ffmpeg -CommandType Application -ErrorAction Stop).Source }
        if (-not $Aria2) { $Aria2 = (Get-Command aria2c -CommandType Application -ErrorAction Stop).Source }
        $env:JIYING_TEST_FFMPEG = (Resolve-Path -LiteralPath $Ffmpeg).Path
        $env:JIYING_TEST_ARIA2C = (Resolve-Path -LiteralPath $Aria2).Path
        $ffprobe = Join-Path (Split-Path $env:JIYING_TEST_FFMPEG -Parent) 'ffprobe.exe'
        if (-not (Test-Path -LiteralPath $ffprobe -PathType Leaf)) { throw '请将 ffprobe.exe 放在指定 FFmpeg 的同一目录。' }
        $env:PATH = (Split-Path $env:JIYING_TEST_FFMPEG -Parent) + [IO.Path]::PathSeparator + $env:PATH
        $sqlite = '-'
        if ($SqliteMode -ne 'system') {
            $sqlite = & $Python (Join-Path $repo 'scripts/prepare-sqlite.py') (Join-Path $OutputDir 'dependencies')
            if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $sqlite -PathType Leaf)) {
                throw 'SQLite 测试库未通过校验。'
            }
        }
        $modes = if ($SqliteMode -eq 'both') { @('release', 'system') } else { @($SqliteMode) }
        $backendRunner = Join-Path $repo 'scripts/test-backend.py'
        Push-Location -LiteralPath (Join-Path $repo 'video-subtitler')
        try {
            foreach ($mode in $modes) {
                $library = if ($mode -eq 'release') { $sqlite } else { '-' }
                Invoke-Checked $Python @($backendRunner,$library,'tests','-q','-ra',
                    '--basetemp',(Join-Path $isolated "pytest-$mode"),
                    '--junitxml',(Join-Path $OutputDir "backend-$mode.xml")) "backend-$mode.log"
            }
        } finally { Pop-Location }
    }

    if ($Suite -ne 'backend') {
        $solution = Join-Path $repo 'Jiying.sln'
        Invoke-Checked $DotNet @('build',$solution,'-c','Release','--nologo') 'build.log'
        Invoke-Checked $DotNet @('test',$solution,'-c','Release','--no-build','--nologo','--results-directory',
            $OutputDir,'--logger','trx;LogFileName=subtitle-unit.trx') 'subtitle-unit.log'
        $tests = Join-Path $repo 'mistdown/native-shell/tests'
        $env:JIYING_FIXTURE_ONLY = '1'
        $env:JIYING_WEBSITE_CHECKS = '0'
        [Environment]::SetEnvironmentVariable('JIYING_LIVE_ENGINE_PORT', $null, 'Process')
        foreach ($name in @('EngineArtifactRegression','UiRegression','DesktopRaceRegression')) {
            $framework = if ($name -eq 'EngineArtifactRegression') { 'net10.0' } else { 'net10.0-windows10.0.19041.0/win-x64' }
            $binary = Join-Path $tests "$name/bin/Release/$framework/$name.dll"
            Invoke-Checked $DotNet @($binary) "$name.log"
        }
    }
} finally {
    Pop-Location
}
Write-Output "测试报告目录 $OutputDir"
