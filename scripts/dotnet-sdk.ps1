function Resolve-JiyingDotNet([string]$Command,[string]$Repository) {
    $required = (Get-Content -LiteralPath (Join-Path $Repository 'global.json') -Raw -Encoding UTF8 |
        ConvertFrom-Json).sdk.version
    $application = Get-Command $Command -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($application) {
        Push-Location -LiteralPath $Repository
        try {
            $ErrorActionPreference = 'Continue'
            $actual = & $application.Source --version 2>$null
            if ($LASTEXITCODE -eq 0 -and $actual -match '^\d+\.\d+\.\d+$' -and
                ([version]$actual).Major -eq ([version]$required).Major -and
                [version]$actual -ge [version]$required) {
                return $application.Source
            }
        } finally { Pop-Location }
    }
    throw "项目需要 .NET SDK $required 或 global.json 允许的补丁版本。请用 -DotNet 指定对应 dotnet.exe 完整路径。"
}
