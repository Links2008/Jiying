param([string]$AssemblyPath = (Join-Path $PSScriptRoot '..\Jiying.Desktop\bin\Release\net10.0-windows\win-x64\Jiying.Native.dll'))

$ErrorActionPreference = 'Stop'
$assembly = [Reflection.Assembly]::LoadFrom((Resolve-Path $AssemblyPath).Path)
$type = $assembly.GetType('Jiying.Desktop.EngineClient', $true)
$listener = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback, 0)
$listener.Start()
$port = ([Net.IPEndPoint]$listener.LocalEndpoint).Port
$accepted = $listener.AcceptTcpClientAsync()
$client = [Activator]::CreateInstance($type, [Reflection.BindingFlags]'Instance,Public,NonPublic', $null, @('unused'), $null)
$type.GetProperty('Port').GetSetMethod($true).Invoke($client, @($port)) | Out-Null

try {
    $method = $type.GetMethod('ProbeAsync')
    $task = $method.Invoke($client, @('https://example.test/slow.mp4', [Threading.CancellationToken]::None))
    try { $completed = $task.Wait(35000) }
    catch [AggregateException] { $completed = $true }
    if (-not $completed) { throw 'Probe remained pending after 35 seconds.' }
    $actualError = $task.Exception.Flatten().InnerExceptions[0]
    if ($actualError -isnot [TimeoutException]) { throw "Expected TimeoutException, got $($actualError.GetType().FullName): $actualError" }
    Write-Output 'PASS: slow probe ends with TimeoutException within 35 seconds.'
} finally {
    $client.Dispose()
    if ($accepted.IsCompletedSuccessfully) { $accepted.Result.Dispose() }
    $listener.Stop()
}
