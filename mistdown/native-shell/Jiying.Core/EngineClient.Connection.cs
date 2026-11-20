using System.Diagnostics;
using System.IO;
using System.Net;
using System.Net.NetworkInformation;
using System.Net.Http.Headers;
using System.Net.Http.Json;
using System.Text.Json;

namespace Jiying.Desktop;

internal sealed partial class EngineClient
{
    private static readonly HttpClient ScanClient = NewLoopbackClient(Timeout.InfiniteTimeSpan);
    private readonly SemaphoreSlim _connectLock = new(1, 1);
    private readonly object _connectionStateLock = new();
    private int? _port;
    private string? _sessionToken;
    private long _connectionVersion;
    private readonly string _enginePath;
    private DateTime _lastSpawn = DateTime.MinValue;

    public EngineClient(string enginePath) => _enginePath = enginePath;

    public int? Port
    {
        get { lock (_connectionStateLock) return _port; }
        internal set
        {
            lock (_connectionStateLock)
            {
                _port = value;
                _sessionToken = null;
                ++_connectionVersion;
            }
        }
    }
    public long ConnectionVersion { get { lock (_connectionStateLock) return _connectionVersion; } }
    public bool IsConnected => Port.HasValue;
    public string EnginePath => _enginePath;
    public string ConnectionHint { get; private set; } = "";

    public async Task<bool> EnsureConnectedAsync(bool startIfMissing, CancellationToken token)
    {
        await _connectLock.WaitAsync(token);
        try
        {
            ConnectionHint = "";
            var (current, version, currentToken) = ConnectionSnapshot();
            if (!File.Exists(_enginePath))
            {
                TrySetPort(null, ref version);
                ConnectionHint = "请先完整解压即影安装包，并保留主程序旁的 engine 文件夹。";
                return false;
            }
            if (current is int port && (currentToken is not null
                    ? await PairedEngineReadyAsync(port, currentToken, token)
                    : await CheckPortAsync(port, token) is not null))
            {
                token.ThrowIfCancellationRequested();
                lock (_connectionStateLock) return _connectionVersion == version && _port == current;
            }

            var session = EngineSession.Read(_enginePath);
            int? found = session is not null && await PairedEngineReadyAsync(session.Port, session.Token, token)
                ? session.Port : null;
            if (found is null && !startIfMissing) found = await FindEngineAsync(token);
            token.ThrowIfCancellationRequested();
            if (!TrySetPort(found, ref version, found == session?.Port ? session?.Token : null)) return false;
            if (found.HasValue) return true;
            if (!startIfMissing) return false;
            if (!await PrepareEngineStartAsync(token)) return false;

            lock (_connectionStateLock)
            {
                token.ThrowIfCancellationRequested();
                if (_connectionVersion != version) return false;
                if (DateTime.UtcNow - _lastSpawn > TimeSpan.FromSeconds(3))
                {
                    _lastSpawn = DateTime.UtcNow;
                    using var started = Process.Start(new ProcessStartInfo(_enginePath)
                    {
                        WorkingDirectory = Path.GetDirectoryName(_enginePath)!,
                        UseShellExecute = false,
                        CreateNoWindow = true
                    });
                }
            }

            var startup = Stopwatch.StartNew();
            while (startup.Elapsed < TimeSpan.FromSeconds(30))
            {
                // 登记文件可能早于端口监听，启动初期短探测，慢启动恢复常规节奏。
                var early = startup.Elapsed < TimeSpan.FromSeconds(2);
                await Task.Delay(early ? 50 : 500, token).ConfigureAwait(false);
                session = EngineSession.Read(_enginePath);
                found = session is not null && (!early || IsLoopbackListening(session.Port))
                    && await PairedEngineReadyAsync(session.Port, session.Token, token)
                    ? session.Port : null;
                token.ThrowIfCancellationRequested();
                if (!TrySetPort(found, ref version, session?.Token)) return false;
                if (found.HasValue) return true;
            }
            return false;
        }
        finally { _connectLock.Release(); }
    }

    public void Disconnect() => Port = null;

    // 旧请求或事件流的晚到错误，只能清除自己所使用的那一代连接。
    public bool Disconnect(long expectedVersion)
    {
        lock (_connectionStateLock)
        {
            if (_connectionVersion != expectedVersion) return false;
            _port = null;
            _sessionToken = null;
            ++_connectionVersion;
            return true;
        }
    }

    private (int? Port, long Version, string? Token) ConnectionSnapshot()
    {
        lock (_connectionStateLock) return (_port, _connectionVersion, _sessionToken);
    }

    private bool TrySetPort(int? port, ref long expectedVersion, string? sessionToken = null)
    {
        lock (_connectionStateLock)
        {
            if (_connectionVersion != expectedVersion) return false;
            _port = port;
            _sessionToken = port.HasValue ? sessionToken : null;
            expectedVersion = ++_connectionVersion;
            return true;
        }
    }


    private static bool IsLoopbackListening(int port)
    {
        try
        {
            return IPGlobalProperties.GetIPGlobalProperties().GetActiveTcpListeners()
                .Any(endpoint => endpoint.Port == port && endpoint.Address.Equals(IPAddress.Loopback));
        }
        catch (NetworkInformationException) { return true; }
    }

    private static async Task<bool> PairedEngineReadyAsync(int port, string credential, CancellationToken token)
    {
        using var timeout = CancellationTokenSource.CreateLinkedTokenSource(token);
        timeout.CancelAfter(TimeSpan.FromSeconds(1));
        using var request = new HttpRequestMessage(HttpMethod.Get, $"http://127.0.0.1:{port}/api/v1/session/health");
        request.Headers.Authorization = new AuthenticationHeaderValue("Bearer", credential);
        try
        {
            using var response = await ScanClient.SendAsync(request, HttpCompletionOption.ResponseHeadersRead, timeout.Token);
            return response.StatusCode == HttpStatusCode.NoContent;
        }
        catch (OperationCanceledException) when (!token.IsCancellationRequested) { return false; }
        catch (HttpRequestException) { return false; }
    }

    private async Task<bool> PrepareEngineStartAsync(CancellationToken token)
    {
        var running = Process.GetProcessesByName(Path.GetFileNameWithoutExtension(_enginePath));
        try
        {
            foreach (var process in running)
            {
                string? path;
                try
                {
                    // Windows 可能暂时保留已经退出的进程记录。
                    if (process.Threads.Count == 0) continue;
                    path = process.MainModule?.FileName;
                }
                catch (Exception ex) when (ex is System.ComponentModel.Win32Exception or InvalidOperationException)
                {
                    ConnectionHint = "无法确认已有引擎的路径，请关闭旧版即影后重试。";
                    return false;
                }
                if (path is null) continue;
                if (Path.GetFullPath(path).Equals(Path.GetFullPath(_enginePath), StringComparison.OrdinalIgnoreCase))
                {
                    ConnectionHint = "当前版本引擎正在启动，请稍后自动重连。";
                    return false;
                }
                var oldInstall = Directory.GetParent(Path.GetDirectoryName(path)!)?.FullName;
                if (oldInstall is null || !File.Exists(Path.Combine(oldInstall, "Jiying.Native.exe")))
                {
                    ConnectionHint = "检测到另一份即影引擎，请关闭它后重试。";
                    return false;
                }
                if (AppRunningIn(oldInstall))
                {
                    ConnectionHint = "旧版即影窗口仍在运行。关闭旧窗口后，新版会自动切换引擎。";
                    return false;
                }
                var session = EngineSession.Read(path);
                if (session is not null)
                {
                    if (session.ProcessId != process.Id || !await AskEngineToExitAsync(session, token))
                    {
                        ConnectionHint = "旧版引擎仍有任务或无法确认任务状态。任务结束后会自动切换。";
                        return false;
                    }
                }
                else
                {
                    var port = await FindEngineAsync(token, path);
                    if (port is null || await HasActiveTasksAsync(port.Value, token))
                    {
                        ConnectionHint = "旧版引擎仍有任务或无法确认任务状态。任务结束后会自动切换。";
                        return false;
                    }
                }
                try
                {
                    if (session is null) process.Kill(entireProcessTree: true);
                    using var timeout = CancellationTokenSource.CreateLinkedTokenSource(token);
                    timeout.CancelAfter(TimeSpan.FromSeconds(5));
                    await process.WaitForExitAsync(timeout.Token);
                }
                catch (Exception ex) when (ex is System.ComponentModel.Win32Exception or InvalidOperationException or OperationCanceledException)
                {
                    ConnectionHint = "旧版引擎尚未退出，请稍后自动重试。";
                    return false;
                }
            }
            return true;
        }
        finally { foreach (var process in running) process.Dispose(); }
    }

    internal static bool AppRunningIn(string directory)
    {
        var apps = Process.GetProcessesByName("Jiying.Native");
        try
        {
            foreach (var app in apps)
            {
                try
                {
                    if (app.Threads.Count == 0) continue;
                    if (string.Equals(Path.GetDirectoryName(app.MainModule?.FileName), directory,
                                      StringComparison.OrdinalIgnoreCase)) return true;
                }
                catch (Exception ex) when (ex is System.ComponentModel.Win32Exception or InvalidOperationException) { return true; }
            }
            return false;
        }
        finally { foreach (var app in apps) app.Dispose(); }
    }

    internal static async Task<bool> HasActiveTasksAsync(int port, CancellationToken token)
    {
        using var timeout = CancellationTokenSource.CreateLinkedTokenSource(token);
        timeout.CancelAfter(TimeSpan.FromSeconds(3));
        try
        {
            using var response = await ScanClient.GetAsync($"http://127.0.0.1:{port}/api/v1/tasks", timeout.Token);
            if (!response.IsSuccessStatusCode) return true;
            using var doc = JsonDocument.Parse(await response.Content.ReadAsStringAsync(timeout.Token));
            return doc.RootElement.ValueKind != JsonValueKind.Array ||
                   doc.RootElement.EnumerateArray().Any(task =>
                       // 只有明确的终态才允许退出旧引擎，缺失字段或未来状态均保守保留。
                       task.ValueKind != JsonValueKind.Object ||
                       !task.TryGetProperty("status", out var status) ||
                       status.ValueKind != JsonValueKind.String ||
                       status.GetString() is not ("completed" or "failed" or "cancelled" or "interrupted"));
        }
        catch (Exception ex) when (ex is HttpRequestException or JsonException or InvalidOperationException or OperationCanceledException) { return true; }
    }

    private static async Task<bool> AskEngineToExitAsync(EngineSession session, CancellationToken token)
    {
        using var timeout = CancellationTokenSource.CreateLinkedTokenSource(token);
        timeout.CancelAfter(TimeSpan.FromSeconds(3));
        using var request = new HttpRequestMessage(HttpMethod.Post,
            $"http://127.0.0.1:{session.Port}/api/v1/session/exit");
        request.Headers.Authorization = new AuthenticationHeaderValue("Bearer", session.Token);
        try
        {
            using var response = await ScanClient.SendAsync(request, timeout.Token);
            return response.StatusCode == HttpStatusCode.Accepted;
        }
        catch (OperationCanceledException) when (!token.IsCancellationRequested) { return false; }
        catch (HttpRequestException) { return false; }
    }

    internal async Task<int?> FindEngineAsync(CancellationToken token, string? enginePath = null, IEnumerable<int>? portsToScan = null)
    {
        using var search = CancellationTokenSource.CreateLinkedTokenSource(token);
        var checks = (portsToScan ?? Enumerable.Range(8765, 20))
            .Select(port => CheckPortAsync(port, search.Token, enginePath)).ToArray();
        var pending = checks.ToList();
        try
        {
            while (pending.Count > 0)
            {
                var completed = await Task.WhenAny(pending);
                pending.Remove(completed);
                var port = await completed;
                token.ThrowIfCancellationRequested();
                if (port.HasValue) return port;
            }
            return null;
        }
        finally
        {
            search.Cancel();
            try { await Task.WhenAll(checks); }
            catch (OperationCanceledException) when (!token.IsCancellationRequested) { }
        }
    }

    internal async Task<int?> CheckPortAsync(int port, CancellationToken token, string? enginePath = null)
    {
        using var timeout = CancellationTokenSource.CreateLinkedTokenSource(token);
        timeout.CancelAfter(600);
        try
        {
            using var request = new HttpRequestMessage(HttpMethod.Get, $"http://127.0.0.1:{port}/api/v1/instance");
            request.Headers.ConnectionClose = true;
            using var response = await ScanClient.SendAsync(request, timeout.Token);
            if (!response.IsSuccessStatusCode) return null;
            using var doc = JsonDocument.Parse(await response.Content.ReadAsStringAsync(timeout.Token));
            if (doc.RootElement.ValueKind != JsonValueKind.Object ||
                !doc.RootElement.TryGetProperty("engine_root", out var root) || root.ValueKind != JsonValueKind.String) return null;
            if (doc.RootElement.TryGetProperty("requires_token", out var paired) && paired.ValueKind != JsonValueKind.False)
                return null;
            var expected = Path.TrimEndingDirectorySeparator(Path.GetFullPath(Path.GetDirectoryName(enginePath ?? _enginePath)!));
            var actual = Path.TrimEndingDirectorySeparator(Path.GetFullPath(root.GetString() ?? ""));
            return string.Equals(expected, actual, StringComparison.OrdinalIgnoreCase) ? port : null;
        }
        catch (OperationCanceledException) when (!token.IsCancellationRequested) { return null; }
        catch (HttpRequestException) { return null; }
        catch (JsonException) { return null; }
        catch (ArgumentException) { return null; }
        catch (NotSupportedException) { return null; }
    }
}
