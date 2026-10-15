// 使用隔离回环服务，检查事件流取消与旧引擎任务状态的保守判断。
using System.Net;
using System.Net.Sockets;
using System.Reflection;
using System.Text;
using System.Text.Json;
using Jiying.Desktop;

internal static class NetworkRegression
{
    public static async Task Run()
    {
        var failures = new List<string>();
        using (var server = new StatusServer("{\"detail\":\"暂不可用\"}", pauseErrorBody: true))
        using (var client = new EngineClient("unused-test-engine"))
        using (var stop = new CancellationTokenSource())
        {
            typeof(EngineClient).GetProperty("Port")!.SetValue(client, server.Port);
            var listening = client.ListenEventsAsync(() => Task.CompletedTask, stop.Token);
            await server.Paused.Task.WaitAsync(TimeSpan.FromSeconds(5));
            // 错误响应头已发出，留出事件客户端进入响应体读取的时间。
            await Task.Delay(250);
            stop.Cancel();
            try
            {
                await listening.WaitAsync(TimeSpan.FromSeconds(2));
                failures.Add("错误事件流应以取消结束");
            }
            catch (OperationCanceledException) { }
            catch (TimeoutException) { failures.Add("错误事件流读取响应体后不能及时取消"); }
            finally
            {
                server.Release.TrySetResult();
                try { await listening.WaitAsync(TimeSpan.FromSeconds(5)); }
                catch (Exception error) when (error is OperationCanceledException or InvalidOperationException or HttpRequestException) { }
            }
        }

        var method = typeof(EngineClient).GetMethod("HasActiveTasksAsync", BindingFlags.Static | BindingFlags.NonPublic)!;
        foreach (var (body, expected) in new[]
        {
            ("[]", false),
            ("[{\"status\":\"completed\"},{\"status\":\"failed\"},{\"status\":\"cancelled\"},{\"status\":\"interrupted\"}]", false),
            ("[{\"status\":\"running\"}]", true),
            ("[{\"status\":\"pending\"}]", true),
            ("[{\"status\":\"created\"}]", true),
            ("[{\"id\":\"missing-status\"}]", true),
            ("[{\"status\":null}]", true),
            ("[{\"status\":42}]", true),
            ("[null]", true),
            ("[{\"status\":\"future-active-state\"}]", true)
        })
        {
            using var server = new StatusServer(body);
            var active = await (Task<bool>)method.Invoke(null, [server.Port, CancellationToken.None])!;
            if (active != expected) failures.Add("无法确认旧引擎状态时不得认为可退出：" + body);
        }
        await CheckDiscoveryReplies(failures);
        await CheckInvalidatedProbe(failures);
        await CheckLateTransportFailure(failures);
        CheckConditionalDisconnect(failures);
        if (failures.Count > 0) throw new Exception(string.Join(Environment.NewLine, failures));
        Console.WriteLine("通过：错误事件流取消；空任务、四种终态、三种活动状态和缺失/异常/未知状态判断。");
        Console.WriteLine("通过：非对象发现回复隔离、在途端口探测失效、旧 API/预览/视频请求失败不清除新连接。");
    }

    private static void SetPort(EngineClient client, int port) =>
        typeof(EngineClient).GetProperty("Port")!.SetValue(client, port);

    private static void CheckConditionalDisconnect(List<string> failures)
    {
        using var client = new EngineClient("isolated-version-engine");
        SetPort(client, 12345);
        var oldVersion = client.ConnectionVersion;
        // 端口相同也可能已是重启后的引擎，不能只比较端口号。
        SetPort(client, 12345);
        if (client.Disconnect(oldVersion) || client.Port != 12345)
            failures.Add("旧连接版本清除了同端口的新连接");
        var currentVersion = client.ConnectionVersion;
        if (!client.Disconnect(currentVersion) || client.Port is not null || client.ConnectionVersion == currentVersion)
            failures.Add("当前连接版本未能断开并使在途探测失效");
    }

    private static async Task CheckDiscoveryReplies(List<string> failures)
    {
        var check = typeof(EngineClient).GetMethod("CheckPortAsync", BindingFlags.Instance | BindingFlags.NonPublic)!;
        foreach (var body in new[] { "[]", "null", "42", "true", "\"其他本地服务\"" })
        {
            using var server = new StatusServer(body);
            using var client = new EngineClient("isolated-discovery-engine");
            try
            {
                var result = await (Task<int?>)check.Invoke(client, [server.Port, CancellationToken.None, null])!;
                if (result is not null) failures.Add("发现回复必须是引擎对象，不能接受：" + body);
            }
            catch (InvalidOperationException) { failures.Add("其他端口的合法非对象 JSON 阻断引擎发现：" + body); }
        }
    }

    private static async Task CheckInvalidatedProbe(List<string> failures)
    {
        var directory = Path.Combine(Path.GetTempPath(), "jiying-connection-probe-" + Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(directory);
        var enginePath = Path.Combine(directory, "test-engine");
        await File.WriteAllTextAsync(enginePath, "仅用来满足文件存在检查，不启动进程");
        try
        {
            foreach (var action in new[] { "stable", "disconnect", "reconnect" })
            {
                using var server = new StatusServer(JsonSerializer.Serialize(new { engine_root = directory }), pauseBody: true);
                using var next = new StatusServer("[]");
                using var client = new EngineClient(enginePath);
                SetPort(client, server.Port);
                var probing = client.EnsureConnectedAsync(false, CancellationToken.None);
                await server.Paused.Task.WaitAsync(TimeSpan.FromSeconds(3));
                if (action != "stable") client.Disconnect();
                if (action == "reconnect") SetPort(client, next.Port);
                server.Release.TrySetResult();
                var connected = await probing.WaitAsync(TimeSpan.FromSeconds(3));
                if (connected != (action == "stable")) failures.Add("在途端口探测未正确识别连接版本：" + action);
                int? expectedPort = action switch { "stable" => server.Port, "reconnect" => next.Port, _ => null };
                if (client.Port != expectedPort) failures.Add("旧端口探测覆盖了后续连接状态：" + action);
            }
        }
        finally { Directory.Delete(directory, true); }
    }

    private static async Task CheckLateTransportFailure(List<string> failures)
    {
        var directory = Path.Combine(Path.GetTempPath(), "jiying-connection-transport-" + Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(directory);
        try
        {
            foreach (var kind in new[] { "api", "preview", "video" })
            foreach (var reconnect in new[] { false, true })
            {
                using var old = new BrokenTransportServer();
                using var next = new StatusServer("[]");
                using var client = new EngineClient("isolated-transport-engine");
                var output = Path.Combine(directory, kind + ".mp4");
                await File.WriteAllTextAsync(output, "原有成品");
                SetPort(client, old.Port);
                Task request = kind switch
                {
                    "api" => client.GetTasksAsync(CancellationToken.None),
                    "preview" => client.GetSubtitlePreviewAsync("test", 18, "bottom", 1, "#FFFFFF", 0, 0, "test", CancellationToken.None),
                    _ => client.SaveArtifactAsync(1, output, CancellationToken.None)
                };
                await old.Paused.Task.WaitAsync(TimeSpan.FromSeconds(3));
                if (reconnect)
                {
                    client.Disconnect();
                    SetPort(client, next.Port);
                }
                old.Release.TrySetResult();
                try
                {
                    await request.WaitAsync(TimeSpan.FromSeconds(5));
                    failures.Add("旧传输失败必须报告异常：" + kind);
                }
                catch (HttpRequestException) { }
                if (client.Port != (reconnect ? next.Port : null))
                    failures.Add(reconnect ? "旧传输失败清除了重连后的端口：" + kind : "当前传输失败未标记连接断开：" + kind);
                else if (reconnect && (await client.GetTasksAsync(CancellationToken.None)).Count != 0)
                    failures.Add("重连端口未能继续读取：" + kind);
                if (await File.ReadAllTextAsync(output) != "原有成品") failures.Add("旧传输失败破坏了已有成品：" + kind);
                if (Directory.GetFiles(directory, ".jiying-save-*.tmp").Length != 0) failures.Add("旧传输失败残留临时稿：" + kind);
            }
        }
        finally { Directory.Delete(directory, true); }
    }

    // 不发送响应头，待测试切换连接后复位旧 TCP；后续自动重试同样立即失败。
    private sealed class BrokenTransportServer : IDisposable
    {
        private readonly TcpListener _listener = new(IPAddress.Loopback, 0);
        private readonly CancellationTokenSource _stop = new();
        private readonly Task _accepting;
        public int Port { get; }
        public TaskCompletionSource Paused { get; } = new(TaskCreationOptions.RunContinuationsAsynchronously);
        public TaskCompletionSource Release { get; } = new(TaskCreationOptions.RunContinuationsAsynchronously);

        public BrokenTransportServer()
        {
            _listener.Start();
            Port = ((IPEndPoint)_listener.LocalEndpoint).Port;
            _accepting = Accept();
        }

        private async Task Accept()
        {
            try
            {
                while (!_stop.IsCancellationRequested)
                {
                    using var client = await _listener.AcceptTcpClientAsync(_stop.Token);
                    var buffer = new byte[4096];
                    await client.GetStream().ReadAtLeastAsync(buffer, minimumBytes: 1, throwOnEndOfStream: false, cancellationToken: _stop.Token);
                    Paused.TrySetResult();
                    await Release.Task.WaitAsync(_stop.Token);
                    client.Client.LingerState = new LingerOption(true, 0);
                }
            }
            catch (Exception error) when (error is OperationCanceledException or SocketException or IOException or ObjectDisposedException) { }
        }

        public void Dispose()
        {
            Release.TrySetResult();
            _stop.Cancel();
            _listener.Stop();
            _accepting.GetAwaiter().GetResult();
            _stop.Dispose();
        }
    }

    private sealed class StatusServer : IDisposable
    {
        private readonly HttpListener _listener = new();
        private readonly string _body;
        private readonly bool _pauseErrorBody;
        private readonly bool _pauseBody;
        public int Port { get; }
        public TaskCompletionSource Paused { get; } = new(TaskCreationOptions.RunContinuationsAsynchronously);
        public TaskCompletionSource Release { get; } = new(TaskCreationOptions.RunContinuationsAsynchronously);

        public StatusServer(string body, bool pauseErrorBody = false, bool pauseBody = false)
        {
            _body = body;
            _pauseErrorBody = pauseErrorBody;
            _pauseBody = pauseBody || pauseErrorBody;
            using (var socket = new TcpListener(IPAddress.Loopback, 0))
            {
                socket.Start();
                Port = ((IPEndPoint)socket.LocalEndpoint).Port;
            }
            _listener.Prefixes.Add($"http://127.0.0.1:{Port}/");
            _listener.Start();
            _ = Respond();
        }

        private async Task Respond()
        {
            HttpListenerContext? context = null;
            try
            {
                context = await _listener.GetContextAsync();
                var bytes = Encoding.UTF8.GetBytes(_body);
                context.Response.StatusCode = _pauseErrorBody ? 503 : 200;
                context.Response.ContentLength64 = bytes.Length;
                await context.Response.OutputStream.WriteAsync(bytes.AsMemory(0, 1));
                await context.Response.OutputStream.FlushAsync();
                Paused.TrySetResult();
                if (_pauseBody) await Release.Task;
                await context.Response.OutputStream.WriteAsync(bytes.AsMemory(1));
            }
            catch (Exception error) when (error is IOException or HttpListenerException or ObjectDisposedException) { }
            finally { context?.Response.Close(); }
        }

        public void Dispose()
        {
            Release.TrySetResult();
            _listener.Close();
        }
    }
}
