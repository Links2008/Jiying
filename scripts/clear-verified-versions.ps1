# 盘点和回收确认属于即影发布包的版本目录，默认只预检。
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string[]]$Versions,
    [Parameter(Mandatory)][string[]]$Keep,
    [Parameter(Mandatory)][string[]]$Manifests,
    [switch]$Apply
)
$ErrorActionPreference = 'Stop'
$root = [IO.Path]::GetFullPath((Join-Path $env:LOCALAPPDATA 'Programs\Jiying'))
$rootInfo = Get-Item -LiteralPath $root -Force
for ($ancestor = $rootInfo; $null -ne $ancestor; $ancestor = $ancestor.Parent) {
    if ($ancestor.Attributes -band [IO.FileAttributes]::ReparsePoint) {
        throw "安装路径经过重解析点：$($ancestor.FullName)"
    }
}
$pathComparer = [StringComparer]::OrdinalIgnoreCase
$allowed = [Collections.Generic.HashSet[string]]::new($pathComparer)
$labelAliases = @{}
foreach ($manifest in $Manifests) {
    $data = Get-Content -LiteralPath $manifest -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($data.files -isnot [pscustomobject]) { throw '需要已验证的发布文件清单。' }
    foreach ($entry in $data.files.PSObject.Properties) { [void]$allowed.Add($entry.Name.Replace('\','/')) }
    if ($data.version_aliases) {
        foreach ($entry in $data.version_aliases.PSObject.Properties) { $labelAliases[$entry.Name] = $entry.Value }
    }
}
if ($Versions.Count -ne @($Versions | Sort-Object -Unique).Count -or $Keep.Count -lt 2) {
    throw '清理名单不可重复，并须列明现用及回退目录。'
}

function Read-ProgramTree([string]$name) {
    if ($name -notmatch '^\d+\.\d+\.\d+\.\d+-\d{8}(?:-[a-z0-9-]+)?$') { throw "无效目录名：$name" }
    $folder = Join-Path $root $name
    $resolved = (Resolve-Path -LiteralPath $folder).ProviderPath
    if ($resolved -ne $folder -or [IO.Path]::GetDirectoryName($resolved) -ne $root) { throw '目录越界。' }
    $app = Get-Item -LiteralPath (Join-Path $resolved 'Jiying.Native.exe')
    $fitsLabel = $app.VersionInfo.FileVersion -eq ($name -split '-',2)[0]
    if (-not $fitsLabel -and $labelAliases.ContainsKey($name)) {
        $alias = $labelAliases[$name]
        $fitsLabel = $app.VersionInfo.FileVersion -eq $alias.version -and
            (Get-FileHash -LiteralPath $app.FullName -Algorithm SHA256).Hash -eq $alias.exe_sha256
    }
    if (-not $fitsLabel -or $app.VersionInfo.ProductName -ne 'Jiying.Native' -or
        -not (Test-Path -LiteralPath (Join-Path $resolved 'engine\JiyingEngine.exe') -PathType Leaf)) {
        throw "目录缺少匹配版本的程序：$name"
    }
    $stack = [Collections.Generic.Stack[IO.DirectoryInfo]]::new()
    $stack.Push((Get-Item -LiteralPath $resolved -Force))
    $files = 0
    [long]$bytes = 0
    while ($stack.Count) {
        $current = $stack.Pop()
        if ($current.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw "目录中有重解析点：$name" }
        foreach ($entry in Get-ChildItem -LiteralPath $current.FullName -Force) {
            if ($entry.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw "目录中有重解析点：$name" }
            if ($entry.PSIsContainer) {
                if ($entry.Name -in @('data','cache','logs','library','models','downloads','website-profile')) {
                    throw "目录含用户数据子目录：$name"
                }
                $stack.Push($entry)
                continue
            }
            $relative = $entry.FullName.Substring($resolved.Length + 1).Replace('\','/')
            if (-not $allowed.Contains($relative)) { throw "发布清单之外的文件：$name/$relative" }
            $files++
            $bytes += $entry.Length
        }
    }
    [PSCustomObject]@{ name=$name; path=$resolved; files=$files; bytes=$bytes }
}

function Assert-Unused([string]$folder) {
    $prefix = $folder + [IO.Path]::DirectorySeparatorChar
    foreach ($process in Get-CimInstance -ClassName Win32_Process -Property ExecutablePath) {
        if ($process.ExecutablePath -and $process.ExecutablePath.StartsWith($prefix,[StringComparison]::OrdinalIgnoreCase)) {
            throw "目录仍有进程在运行：$folder"
        }
    }
    $shell = New-Object -ComObject WScript.Shell
    $places = @([Environment]::GetFolderPath('Desktop'), [Environment]::GetFolderPath('StartMenu'),
                [Environment]::GetFolderPath('CommonDesktopDirectory'), [Environment]::GetFolderPath('CommonStartMenu'),
                (Join-Path $env:APPDATA 'Microsoft\Internet Explorer\Quick Launch\User Pinned\TaskBar'))
    foreach ($place in $places) {
        if (-not $place -or -not (Test-Path -LiteralPath $place -PathType Container)) { continue }
        $recurse = $place -like '*Start Menu*'
        foreach ($link in Get-ChildItem -LiteralPath $place -File -Filter '*.lnk' -Recurse:$recurse) {
            $target = $shell.CreateShortcut($link.FullName).TargetPath
            if ($target -and $target.StartsWith($prefix,[StringComparison]::OrdinalIgnoreCase)) {
                throw "还有快捷方式指向待回收目录：$folder"
            }
        }
    }
    foreach ($name in @('HKCU:\Software\Classes\jiying-repaired\shell\open\command',
                          'HKCU:\Software\Classes\AppUserModelId\Jiying.Desktop.Repaired')) {
        if (Test-Path -LiteralPath $name) {
            $key = Get-Item -LiteralPath $name
            foreach ($value in @($key.GetValue(''),$key.GetValue('IconUri'))) {
                if ($value -and $value.TrimStart('"').StartsWith($prefix,[StringComparison]::OrdinalIgnoreCase)) {
                    throw "通知入口仍指向待回收目录：$folder"
                }
            }
        }
    }
}

foreach ($name in $Keep) { $null = Read-ProgramTree $name }
$candidates = @(foreach ($name in $Versions) {
    if ($name -in $Keep) { throw "已保护的目录不能回收：$name" }
    $item = Read-ProgramTree $name
    Assert-Unused $item.path
    $item
})
foreach ($item in $candidates) {
    if ($Apply) {
        $current = Read-ProgramTree $item.name
        if ($current.path -ne $item.path -or $current.files -ne $item.files -or $current.bytes -ne $item.bytes) {
            throw "预检后有文件变化：$($item.name)"
        }
        Assert-Unused $current.path
        Remove-Item -LiteralPath $current.path -Recurse -Force
        if (Test-Path -LiteralPath $current.path) { throw "目录未完全回收：$($item.name)" }
    }
    [PSCustomObject]@{版本目录=$item.name; 文件数=$item.files; 字节数=$item.bytes; 已删除=[bool]$Apply}
}
