using System.Diagnostics;
using System.Globalization;
using System.IO;
using System.Net;
using System.Net.Http;
using System.Net.Http.Headers;
using System.Net.Http.Json;
using System.Text;
using System.Text.Json;

namespace Jiying.Desktop;

internal sealed class EngineApiException(string message, string code) : InvalidOperationException(message)
{
    public string Code { get; } = code;
}

internal sealed partial class EngineClient : IDisposable
{
    internal static readonly JsonSerializerOptions JsonOptions = new(JsonSerializerDefaults.Web)
    {
        PropertyNameCaseInsensitive = true,
        WriteIndented = true
    };

    private static HttpClient NewLoopbackClient(TimeSpan timeout) =>
        new(new HttpClientHandler { UseProxy = false, AllowAutoRedirect = false }) { Timeout = timeout };

    private HttpClient _api = NewLoopbackClient(TimeSpan.FromMinutes(30));
    private HttpClient _events = NewLoopbackClient(Timeout.InfiniteTimeSpan);
    internal HttpClient RequestTransport { get => _api; set => _api = value; }
    internal HttpClient EventTransport { get => _events; set => _events = value; }
    public async Task<ProbeResult> ProbeAsync(string url, CancellationToken token, bool forceRefresh = false)
    {
        using var timeout = CancellationTokenSource.CreateLinkedTokenSource(token);
        timeout.CancelAfter(TimeSpan.FromSeconds(90));
        try { return await SendAsync<ProbeResult>(HttpMethod.Post, "/api/v1/probe", new { url, force_refresh = forceRefresh }, timeout.Token); }
        catch (OperationCanceledException) when (!token.IsCancellationRequested)
        {
            throw new TimeoutException("解析超时，请检查网络后重试。");
        }
    }

    public Task<TaskItem> SubmitAsync(SubmitTaskRequest body, CancellationToken token) =>
        SendAsync<TaskItem>(HttpMethod.Post, "/api/v1/tasks", body, token);

    public Task<List<TaskItem>> GetTasksAsync(CancellationToken token) =>
        SendAsync<List<TaskItem>>(HttpMethod.Get, "/api/v1/tasks", null, token);

    public async Task<TaskSnapshot> GetTaskSnapshotAsync(IReadOnlyCollection<string>? ids, CancellationToken token)
    {
        var query = ids is { Count: > 0 } ? $"&ids={Escape(string.Join(',', ids))}" : "";
        using var deadline = CancellationTokenSource.CreateLinkedTokenSource(token);
        deadline.CancelAfter(TimeSpan.FromSeconds(15));
        using var request = NewRequest(HttpMethod.Get, $"/api/v1/tasks?include_artifacts=true{query}", out var version);
        try
        {
            using var response = await _api.SendAsync(request, deadline.Token);
            ClearExpiredSession(response, version);
            if (!response.IsSuccessStatusCode) throw await ExceptionFromResponseAsync(response, deadline.Token);
            var items = await response.Content.ReadFromJsonAsync<List<TaskItem>>(JsonOptions, deadline.Token)
                ?? throw new InvalidDataException("引擎返回了空任务列表");
            var sequence = EventSequence(response);
            if (sequence is null && ids is { Count: > 0 })
                items = items.Where(item => ids.Contains(item.Id)).ToList();
            return new(items, sequence);
        }
        catch (HttpRequestException)
        {
            Disconnect(version);
            throw;
        }
    }

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
        using var request = NewRequest(HttpMethod.Get, $"/api/v1/artifacts/{id}/file", out var version);
        try
        {
            {
                using var response = await _api.SendAsync(request, HttpCompletionOption.ResponseHeadersRead, deadline.Token).ConfigureAwait(false);
                ClearExpiredSession(response, version);
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

    public Task<byte[]> GetSubtitlePreviewAsync(string fontName, int fontSize, string position, int outline, string color, int verticalOffset, int horizontalOffset, string text, CancellationToken token, bool personNotes = false)
    {
        var query = $"font_name={Uri.EscapeDataString(fontName)}&font_size={fontSize}&position={Uri.EscapeDataString(position)}&outline={outline}&color={Uri.EscapeDataString(color)}&vertical_offset={verticalOffset}&horizontal_offset={horizontalOffset}&text={Uri.EscapeDataString(text)}&person_notes={(personNotes ? "true" : "false")}";
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

    public async Task ListenEventsAsync(Func<Task> onEvent, CancellationToken token) =>
        await ListenTaskEventsAsync(_ => Task.CompletedTask, frame => frame.Event is not null
            ? onEvent() : Task.CompletedTask, token);

    public async Task ListenTaskEventsAsync(Func<long?, Task> onConnected, Func<TaskEventFrame, Task> onFrame,
                                            CancellationToken token)
    {
        using var idle = CancellationTokenSource.CreateLinkedTokenSource(token);
        idle.CancelAfter(TimeSpan.FromSeconds(35));
        using var request = NewRequest(HttpMethod.Get, "/api/v1/events", out var version);
        using var response = await _events.SendAsync(request, HttpCompletionOption.ResponseHeadersRead, idle.Token);
        ClearExpiredSession(response, version);
        if (!response.IsSuccessStatusCode) throw new InvalidOperationException(await ErrorFromResponseAsync(response, token));
        await onConnected(EventSequence(response));

        await using var stream = await response.Content.ReadAsStreamAsync(token);
        using var reader = new StreamReader(stream);
        var payload = new StringBuilder();
        while (!token.IsCancellationRequested)
        {
            idle.CancelAfter(TimeSpan.FromSeconds(35));
            var line = await reader.ReadLineAsync(idle.Token);
            if (line is null) throw new IOException("引擎事件流已断开");
            if (line.StartsWith("data:", StringComparison.Ordinal))
            {
                var offset = line.Length > 5 && line[5] == ' ' ? 6 : 5;
                payload.Append(line.AsSpan(offset)).Append('\n');
                if (payload.Length > 65536) throw new InvalidDataException("引擎事件长度异常");
            }
            else if (line.Length == 0 && payload.Length != 0)
            {
                TaskEvent? item = null;
                try { item = JsonSerializer.Deserialize<TaskEvent>(payload.ToString(), JsonOptions); }
                catch (JsonException) { }
                payload.Clear();
                await onFrame(new(item, null));
            }
            else if (line.StartsWith(": keep-alive ", StringComparison.Ordinal)
                     && long.TryParse(line.AsSpan(13), NumberStyles.None, CultureInfo.InvariantCulture, out var head))
                await onFrame(new(null, head));
        }
    }

    private static long? EventSequence(HttpResponseMessage response) =>
        response.Headers.TryGetValues("X-Jiying-Event-Sequence", out var values)
        && long.TryParse(values.FirstOrDefault(), NumberStyles.None, CultureInfo.InvariantCulture, out var value)
            ? value : null;

    private async Task<T> SendAsync<T>(HttpMethod method, string path, object? body, CancellationToken token)
    {
        using var request = NewRequest(method, path, out var version);
        if (body is not null) request.Content = JsonContent.Create(body, options: JsonOptions);
        try
        {
            using var response = await _api.SendAsync(request, token);
            ClearExpiredSession(response, version);
            if (!response.IsSuccessStatusCode) throw await ExceptionFromResponseAsync(response, token);
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
        using var request = NewRequest(HttpMethod.Get, path, out var version);
        try
        {
            using var response = await _api.SendAsync(request, token);
            ClearExpiredSession(response, version);
            if (!response.IsSuccessStatusCode) throw new InvalidOperationException(await ErrorFromResponseAsync(response, token));
            return await response.Content.ReadAsByteArrayAsync(token);
        }
        catch (HttpRequestException)
        {
            Disconnect(version);
            throw;
        }
    }

    private HttpRequestMessage NewRequest(HttpMethod method, string path, out long version)
    {
        var connection = ConnectionSnapshot();
        if (connection.Port is not int port) throw new InvalidOperationException("本地视频引擎未连接");
        version = connection.Version;
        var request = new HttpRequestMessage(method, $"http://127.0.0.1:{port}{path}");
        request.Headers.ConnectionClose = true;
        if (connection.Token is not null)
            request.Headers.Authorization = new AuthenticationHeaderValue("Bearer", connection.Token);
        return request;
    }

    private void ClearExpiredSession(HttpResponseMessage response, long version)
    {
        if (response.StatusCode == HttpStatusCode.Unauthorized &&
            response.Headers.WwwAuthenticate.Any(item => item.Scheme.Equals("Bearer", StringComparison.OrdinalIgnoreCase)))
            Disconnect(version);
    }

    private static string Escape(string value) => Uri.EscapeDataString(value);

    private static async Task<EngineApiException> ExceptionFromResponseAsync(HttpResponseMessage response, CancellationToken token)
    {
        var message = await ErrorFromResponseAsync(response, token);
        var code = "";
        try
        {
            using var doc = JsonDocument.Parse(await response.Content.ReadAsStringAsync(token));
            if (doc.RootElement.TryGetProperty("detail", out var detail) && detail.ValueKind == JsonValueKind.Object &&
                detail.TryGetProperty("code", out var value) && value.ValueKind == JsonValueKind.String)
                code = value.GetString() ?? "";
        }
        catch (JsonException) { }
        return new(message, code);
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
    }
}
