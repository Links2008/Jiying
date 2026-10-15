using System.Diagnostics;
using System.IO;
using System.Net.Http;
using System.Net.Http.Json;
using System.Text.Json;

namespace Jiying.Desktop;

internal sealed class EngineClient : IDisposable
{
    internal static readonly JsonSerializerOptions JsonOptions = new(JsonSerializerDefaults.Web)
    {
        PropertyNameCaseInsensitive = true,
        WriteIndented = true
    };

    private static readonly HttpClient ScanClient = new() { Timeout = Timeout.InfiniteTimeSpan };
    private readonly HttpClient _api = new() { Timeout = TimeSpan.FromMinutes(30) };
    private readonly HttpClient _events = new() { Timeout = Timeout.InfiniteTimeSpan };
    private readonly SemaphoreSlim _connectLock = new(1, 1);
    private readonly object _connectionStateLock = new();
    private int? _port;
    private long _connectionVersion;
    private readonly string _enginePath;
    private DateTime _lastSpawn = DateTime.MinValue;

    public EngineClient(string enginePath) => _enginePath = enginePath;

    public int? Port
    {
        get { lock (_connectionStateLock) return _port; }
        private set
        {
            lock (_connectionStateLock)
            {
                _port = value;
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
            var (current, version) = ConnectionSnapshot();
            if (!File.Exists(_enginePath))
            {
                TrySetPort(null, ref version);
                ConnectionHint = "请先完整解压即影安装包，并保留主程序旁的 engine 文件夹。";
                return false;
            }
            if (current is int port && await CheckPortAsync(port, token) is not null)
            {
                token.ThrowIfCancellationRequested();
                lock (_connectionStateLock) return _connectionVersion == version && _port == current;
            }

            var found = await FindEngineAsync(token);
            token.ThrowIfCancellationRequested();
            if (!TrySetPort(found, ref version)) return false;
            if (found.HasValue) return true;
            if (!startIfMissing) return false;
            if (!await PrepareEngineStartAsync(token)) return false;

            lock (_connectionStateLock)
            {
                token.ThrowIfCancellationRequested();
                if (_connectionVersion != version) return false;
                if (DateTime.UtcNow - _lastSpawn > TimeSpan.FromSeconds(30))
                {
                    _lastSpawn = DateTime.UtcNow;
                    Process.Start(new ProcessStartInfo(_enginePath)
                    {
                        WorkingDirectory = Path.GetDirectoryName(_enginePath)!,
                        UseShellExecute = false,
                        CreateNoWindow = true
                    });
                }
            }

            var deadline = DateTime.UtcNow + TimeSpan.FromSeconds(30);
            while (DateTime.UtcNow < deadline)
            {
                await Task.Delay(500, token);
                found = await FindEngineAsync(token);
                token.ThrowIfCancellationRequested();
                if (!TrySetPort(found, ref version)) return false;
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
            ++_connectionVersion;
            return true;
        }
    }

    private (int? Port, long Version) ConnectionSnapshot()
    {
        lock (_connectionStateLock) return (_port, _connectionVersion);
    }

    private bool TrySetPort(int? port, ref long expectedVersion)
    {
        lock (_connectionStateLock)
        {
            if (_connectionVersion != expectedVersion) return false;
            _port = port;
            expectedVersion = ++_connectionVersion;
            return true;
        }
    }

    public async Task<ProbeResult> ProbeAsync(string url, CancellationToken token, bool forceRefresh = false)
    {
        using var timeout = CancellationTokenSource.CreateLinkedTokenSource(token);
        timeout.CancelAfter(TimeSpan.FromSeconds(90));
        try { return await SendAsync<ProbeResult>(HttpMethod.Post, "/api/v1/probe", new { url, force_refresh = forceRefresh }, timeout.Token); }
        catch (OperationCanceledException) when (!token.IsCancellationRequested)
        {
            throw new TimeoutException("链接解析超过 90 秒，请检查网络后重试；B 站高画质可先点击“连接 B 站”扫码登录。");
        }
    }

    public Task<TaskItem> SubmitAsync(SubmitTaskRequest body, CancellationToken token) =>
        SendAsync<TaskItem>(HttpMethod.Post, "/api/v1/tasks", body, token);

    public Task<List<TaskItem>> GetTasksAsync(CancellationToken token) =>
        SendAsync<List<TaskItem>>(HttpMethod.Get, "/api/v1/tasks", null, token);

    public Task<TaskItem> GetTaskAsync(string id, CancellationToken token) =>
        SendAsync<TaskItem>(HttpMethod.Get, $"/api/v1/tasks/{Escape(id)}", null, token);

    public Task CancelAsync(string id, CancellationToken token) =>
        SendAsync<JsonElement>(HttpMethod.Post, $"/api/v1/tasks/{Escape(id)}/cancel", null, token);

    public Task RetryAsync(string id, CancellationToken token) =>
        SendAsync<JsonElement>(HttpMethod.Post, $"/api/v1/tasks/{Escape(id)}/retry", null, token);

    public Task RevealAsync(string id, CancellationToken token) =>
        SendAsync<JsonElement>(HttpMethod.Post, $"/api/v1/tasks/{Escape(id)}/reveal", null, token);

    public Task RerenderAsync(string id, int artifactId, string language, CancellationToken token) =>
        SendAsync<JsonElement>(HttpMethod.Post, $"/api/v1/tasks/{Escape(id)}/rerender",
                               new { lang = language, artifact_id = artifactId, background = true }, token);

    public Task<DeleteResult> DeleteTaskAsync(string id, CancellationToken token) =>
        SendAsync<DeleteResult>(HttpMethod.Delete, $"/api/v1/tasks/{Escape(id)}?with_files=true", null, token);

    public Task<DeleteResult> DeleteTaskArtifactsAsync(string id, string kind, CancellationToken token) =>
        SendAsync<DeleteResult>(HttpMethod.Delete, $"/api/v1/tasks/{Escape(id)}/artifacts?kind={Escape(kind)}", null, token);

    public Task<DeleteResult> ClearDoneAsync(string kind, CancellationToken token, IReadOnlyList<string>? taskIds = null) =>
        SendAsync<DeleteResult>(HttpMethod.Post, "/api/v1/tasks/clear_done",
                                new { with_files = true, kind = kind == "all" ? "" : kind, task_ids = taskIds }, token);

    public Task<EngineConfig> GetConfigAsync(CancellationToken token) =>
        SendAsync<EngineConfig>(HttpMethod.Get, "/api/v1/config", null, token);

    public Task UpdateConfigAsync(EngineConfigUpdate update, CancellationToken token) =>
        SendAsync<JsonElement>(HttpMethod.Post, "/api/v1/config", update, token);

    public Task<BilibiliLoginStatus> StartBilibiliLoginAsync(CancellationToken token) =>
        SendAsync<BilibiliLoginStatus>(HttpMethod.Post, "/api/v1/bilibili/login/start", new { }, token);

    public Task UpdateLoginAsync(string file, string browser, CancellationToken token) =>
        SendAsync<JsonElement>(HttpMethod.Post, "/api/v1/config", new { cookies = file, cookies_from_browser = browser }, token);

    public Task SavePlatformSessionAsync(string platform, IReadOnlyList<WebsiteCookie> cookies, CancellationToken token) =>
        SendAsync<JsonElement>(HttpMethod.Post, "/api/v1/platform/session", new { platform,
            cookies = cookies.Select(c => new { domain = c.Domain, name = c.Name, value = c.Value,
                path = c.Path, secure = c.Secure, http_only = c.HttpOnly, expires = c.Expires }) }, token);

    public Task<BilibiliLoginStatus> PollBilibiliLoginAsync(string sessionId, CancellationToken token) =>
        SendAsync<BilibiliLoginStatus>(HttpMethod.Post, "/api/v1/bilibili/login/poll", new { session_id = sessionId }, token);

    public Task<DriveList> GetDrivesAsync(CancellationToken token) =>
        SendAsync<DriveList>(HttpMethod.Get, "/api/v1/fs/drives", null, token);

    public Task<DirectoryList> GetDirectoriesAsync(string path, CancellationToken token) =>
        SendAsync<DirectoryList>(HttpMethod.Get, $"/api/v1/fs/list?path={Uri.EscapeDataString(path)}", null, token);

    public Task<ArtifactContent> GetArtifactContentAsync(int id, CancellationToken token) =>
        SendAsync<ArtifactContent>(HttpMethod.Get, $"/api/v1/artifacts/{id}/content", null, token);

    public Task SaveArtifactContentAsync(int id, string text, CancellationToken token) =>
        SendAsync<JsonElement>(HttpMethod.Put, $"/api/v1/artifacts/{id}/content", new { text }, token);

    public async Task SaveArtifactAsync(int id, string destination, CancellationToken token)
    {
        // 长视频不能先装进 byte[]：多 GB 响应会超出缓冲上限或耗尽内存。
        // 先流式写入同目录临时稿，完整成功后才替换用户选择的文件。
        token.ThrowIfCancellationRequested();
        var output = Path.GetFullPath(destination);
        var temporary = Path.Combine(Path.GetDirectoryName(output)!, $".jiying-save-{Guid.NewGuid():N}.tmp");
        using var deadline = CancellationTokenSource.CreateLinkedTokenSource(token);
        deadline.CancelAfter(TimeSpan.FromMinutes(30));
        var uri = BuildUri($"/api/v1/artifacts/{id}/file", out var version);
        try
        {
            using (var request = new HttpRequestMessage(HttpMethod.Get, uri))
            {
                request.Headers.ConnectionClose = true;
                using var response = await _api.SendAsync(request, HttpCompletionOption.ResponseHeadersRead, deadline.Token).ConfigureAwait(false);
                if (!response.IsSuccessStatusCode)
                    throw new InvalidOperationException(await ErrorFromResponseAsync(response, deadline.Token).ConfigureAwait(false));
                await using var input = await response.Content.ReadAsStreamAsync(deadline.Token).ConfigureAwait(false);
                await using var file = new FileStream(temporary, FileMode.CreateNew, FileAccess.Write, FileShare.None,
                                                      64 * 1024, FileOptions.Asynchronous | FileOptions.SequentialScan);
                await input.CopyToAsync(file, 64 * 1024, deadline.Token).ConfigureAwait(false);
                await file.FlushAsync(deadline.Token).ConfigureAwait(false);
                if (response.Content.Headers.ContentLength is long expected && file.Length != expected)
                    throw new IOException("视频传输不完整，已保留原文件，请重试保存。");
                file.Flush(flushToDisk: true);
            }
            deadline.Token.ThrowIfCancellationRequested();
            File.Move(temporary, output, overwrite: true);
        }
        catch (HttpRequestException)
        {
            Disconnect(version);
            throw;
        }
        finally
        {
            try { if (File.Exists(temporary)) File.Delete(temporary); }
            catch (Exception error) when (error is IOException or UnauthorizedAccessException)
            {
                Debug.WriteLine("视频临时下载稿清理失败。" + error.GetType().Name);
            }
        }
    }

    public Task<byte[]> GetSubtitlePreviewAsync(string fontName, int fontSize, string position, int outline, string color, int verticalOffset, int horizontalOffset, string text, CancellationToken token)
    {
        var query = $"font_name={Uri.EscapeDataString(fontName)}&font_size={fontSize}&position={Uri.EscapeDataString(position)}&outline={outline}&color={Uri.EscapeDataString(color)}&vertical_offset={verticalOffset}&horizontal_offset={horizontalOffset}&text={Uri.EscapeDataString(text)}";
        return GetBytesAsync($"/api/v1/preview/subtitle?{query}", token);
    }

    public Task<WhisperStatus> GetWhisperStatusAsync(string model, CancellationToken token) =>
        SendAsync<WhisperStatus>(HttpMethod.Get, $"/api/v1/whisper/status?model={Uri.EscapeDataString(model)}", null, token);

    public Task DownloadWhisperAsync(string model, CancellationToken token) =>
        SendAsync<JsonElement>(HttpMethod.Post, "/api/v1/whisper/download", new { model }, token);

    public Task<GpuStatus> GetGpuStatusAsync(CancellationToken token) =>
        SendAsync<GpuStatus>(HttpMethod.Get, "/api/v1/gpu/status", null, token);

    public Task DownloadGpuAsync(CancellationToken token) =>
        SendAsync<JsonElement>(HttpMethod.Post, "/api/v1/gpu/download", null, token);

    public Task<YtDlpCheckResult> CheckYtDlpAsync(CancellationToken token) =>
        SendAsync<YtDlpCheckResult>(HttpMethod.Post, "/api/v1/engine/ytdlp/check", null, token);

    public Task<YtDlpStatus> GetYtDlpStatusAsync(CancellationToken token) =>
        SendAsync<YtDlpStatus>(HttpMethod.Get, "/api/v1/engine/ytdlp", null, token);

    public async Task ListenEventsAsync(Func<Task> onEvent, CancellationToken token)
    {
        using var request = new HttpRequestMessage(HttpMethod.Get, BuildUri("/api/v1/events"));
        using var response = await _events.SendAsync(request, HttpCompletionOption.ResponseHeadersRead, token);
        if (!response.IsSuccessStatusCode) throw new InvalidOperationException(await ErrorFromResponseAsync(response, token));

        await using var stream = await response.Content.ReadAsStreamAsync(token);
        using var reader = new StreamReader(stream);
        var hasData = false;
        while (!token.IsCancellationRequested)
        {
            var line = await reader.ReadLineAsync(token);
            if (line is null) throw new IOException("引擎事件流已断开");
            if (line.StartsWith("data:", StringComparison.Ordinal)) hasData = true;
            else if (line.Length == 0 && hasData)
            {
                hasData = false;
                await onEvent();
            }
        }
    }

    private async Task<T> SendAsync<T>(HttpMethod method, string path, object? body, CancellationToken token)
    {
        using var request = new HttpRequestMessage(method, BuildUri(path, out var version));
        request.Headers.ConnectionClose = true;
        if (body is not null) request.Content = JsonContent.Create(body, options: JsonOptions);
        try
        {
            using var response = await _api.SendAsync(request, token);
            if (!response.IsSuccessStatusCode) throw new InvalidOperationException(await ErrorFromResponseAsync(response, token));
            var result = await response.Content.ReadFromJsonAsync<T>(JsonOptions, token);
            return result ?? throw new InvalidDataException("引擎返回了空响应");
        }
        catch (HttpRequestException)
        {
            Disconnect(version);
            throw;
        }
    }

    private async Task<byte[]> GetBytesAsync(string path, CancellationToken token)
    {
        var uri = BuildUri(path, out var version);
        try
        {
            using var request = new HttpRequestMessage(HttpMethod.Get, uri);
            request.Headers.ConnectionClose = true;
            using var response = await _api.SendAsync(request, token);
            if (!response.IsSuccessStatusCode) throw new InvalidOperationException(await ErrorFromResponseAsync(response, token));
            return await response.Content.ReadAsByteArrayAsync(token);
        }
        catch (HttpRequestException)
        {
            Disconnect(version);
            throw;
        }
    }

    private Uri BuildUri(string path) => BuildUri(path, out _);

    private Uri BuildUri(string path, out long version)
    {
        var connection = ConnectionSnapshot();
        if (connection.Port is not int port) throw new InvalidOperationException("本地视频引擎未连接");
        version = connection.Version;
        return new Uri($"http://127.0.0.1:{port}{path}");
    }

    private static string Escape(string value) => Uri.EscapeDataString(value);

    private async Task<bool> PrepareEngineStartAsync(CancellationToken token)
    {
        var running = Process.GetProcessesByName(Path.GetFileNameWithoutExtension(_enginePath));
        try
        {
            foreach (var process in running)
            {
                // Windows can retain an exited process record while another process holds its handle.
                if (process.Threads.Count == 0) continue;
                string? path;
                try { path = process.MainModule?.FileName; }
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
                var port = await FindEngineAsync(token, path);
                if (port is null || await HasActiveTasksAsync(port.Value, token))
                {
                    ConnectionHint = "旧版引擎仍有任务或无法确认任务状态。任务结束后会自动切换。";
                    return false;
                }
                try
                {
                    process.Kill(entireProcessTree: true);
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

    private static bool AppRunningIn(string directory)
    {
        var apps = Process.GetProcessesByName("Jiying.Native");
        try
        {
            foreach (var app in apps)
            {
                if (app.Threads.Count == 0) continue;
                try
                {
                    if (string.Equals(Path.GetDirectoryName(app.MainModule?.FileName), directory,
                                      StringComparison.OrdinalIgnoreCase)) return true;
                }
                catch (Exception ex) when (ex is System.ComponentModel.Win32Exception or InvalidOperationException) { return true; }
            }
            return false;
        }
        finally { foreach (var app in apps) app.Dispose(); }
    }

    private static async Task<bool> HasActiveTasksAsync(int port, CancellationToken token)
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

    private async Task<int?> FindEngineAsync(CancellationToken token, string? enginePath = null, IEnumerable<int>? portsToScan = null)
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

    private async Task<int?> CheckPortAsync(int port, CancellationToken token, string? enginePath = null)
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

    private static async Task<string> ErrorFromResponseAsync(HttpResponseMessage response, CancellationToken token = default)
    {
        var text = await response.Content.ReadAsStringAsync(token);
        if (string.IsNullOrWhiteSpace(text)) return $"引擎请求失败（{(int)response.StatusCode}）";
        try
        {
            using var doc = JsonDocument.Parse(text);
            if (!doc.RootElement.TryGetProperty("detail", out var detail)) return text;
            if (detail.ValueKind == JsonValueKind.String) return detail.GetString() ?? text;
            if (detail.ValueKind == JsonValueKind.Object && detail.TryGetProperty("message", out var message))
                return (message.GetString() ?? detail.ToString()) +
                    (detail.TryGetProperty("suggestion", out var suggestion) && !string.IsNullOrWhiteSpace(suggestion.GetString())
                        ? "\n" + suggestion.GetString() : "");
            return detail.ToString();
        }
        catch (JsonException) { return text; }
    }

    public void Dispose()
    {
        _api.Dispose();
        _events.Dispose();
        _connectLock.Dispose();
    }
}
