# 为 Windows CI 准备固定版本、已校验的媒体工具。
[CmdletBinding()]
param([Parameter(Mandatory)][string]$OutputDir)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$tools = (New-Item -ItemType Directory -Path $OutputDir -Force).FullName
$scratch = (New-Item -ItemType Directory -Path (Join-Path (Split-Path $tools -Parent) ('media-' + [Guid]::NewGuid().ToString('N')))).FullName

function Get-PinnedArchive([string]$Uri, [string]$Name, [string]$Hash) {
    $file = Join-Path $scratch $Name
    & curl.exe --fail --silent --show-error --location --retry 2 --retry-max-time 110 --max-time 45 -o $file $Uri
    if ($LASTEXITCODE -ne 0 -or (Get-FileHash -LiteralPath $file -Algorithm SHA256).Hash -ne $Hash) {
        throw "下载或 SHA-256 校验失败 $Name"
    }
    $expanded = Join-Path $scratch ([IO.Path]::GetFileNameWithoutExtension($Name))
    New-Item -ItemType Directory -Path $expanded | Out-Null
    if ($Name.EndsWith('.zip')) {
        Expand-Archive -LiteralPath $file -DestinationPath $expanded
    } else {
        & tar.exe -xf $file -C $expanded
        if ($LASTEXITCODE -ne 0) { throw "无法解包 $Name" }
    }
    $expanded
}

$ffmpeg = Get-PinnedArchive 'https://github.com/GyanD/codexffmpeg/releases/download/9.0.1/ffmpeg-9.0.1-essentials_build.7z' 'ffmpeg.7z' '49A73BDF0850092A252AC4641D922F3048D63ED113E196CC65CE1E4F7FB33E85'
$aria = Get-PinnedArchive 'https://github.com/aria2/aria2/releases/download/release-1.37.0/aria2-1.37.0-win-64bit-build1.zip' 'aria2.zip' '67D015301EEF0B612191212D564C5BB0A14B5B9C4796B76454276A4D28D9B288'
$deno = Get-PinnedArchive 'https://github.com/denoland/deno/releases/download/v2.9.8/deno-x86_64-pc-windows-msvc.zip' 'deno.zip' 'CBA84B26E0DF5AE7F74FC66B86A95B866872A86292EBD84FA1E959927EDD93BF'

foreach ($entry in @(
    @{Root=$ffmpeg; Name='ffmpeg.exe'}, @{Root=$ffmpeg; Name='ffprobe.exe'},
    @{Root=$aria; Name='aria2c.exe'}, @{Root=$deno; Name='deno.exe'}
)) {
    $files = @(Get-ChildItem -LiteralPath $entry.Root -Recurse -File -Filter $entry.Name)
    if ($files.Count -ne 1) { throw "工具文件缺失或重复 $($entry.Name)" }
    Copy-Item -LiteralPath $files[0].FullName -Destination $tools
}
Write-Output $tools
