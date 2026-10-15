// 使用随机本机端口模拟就绪引擎和无响应邻居，不启动真实引擎。
using System.Diagnostics;
using System.Net;
using System.Net.Sockets;
using System.Reflection;
using System.Text;
using System.Text.Json;
using Jiying.Desktop;

internal static class EngineDiscoveryRegression
{
    internal static async Task Run(MainWindow window, Action<bool, string> check)
    {
        const BindingFlags flags = BindingFlags.Instance | BindingFlags.NonPublic;
        var engine = typeof(MainWindow).GetField("_engine", flags)!.GetValue(window)!;
        var type = engine.GetType();
        var path = (string)type.GetProperty("EnginePath")!.GetValue(engine)!;
        using var timeout = new CancellationTokenSource(TimeSpan.FromSeconds(5));
        var waiting = new TcpListener(IPAddress.Loopback, 0);
        var ready = new TcpListener(IPAddress.Loopback, 0);
        var foreign = new TcpListener(IPAddress.Loopback, 0);
        waiting.Start(); ready.Start(); foreign.Start();
        var waitingPort = ((IPEndPoint)waiting.LocalEndpoint).Port;
        var readyPort = ((IPEndPoint)ready.LocalEndpoint).Port;
        var foreignPort = ((IPEndPoint)foreign.LocalEndpoint).Port;
        var blocked = Task.Run(async () =>
        {
            try { using var client = await waiting.AcceptTcpClientAsync(timeout.Token); await Task.Delay(3000, timeout.Token); }
            catch (OperationCanceledException) { }
        });
        async Task Serve(TcpListener listener, string root, int delay)
        {
            using var client = await listener.AcceptTcpClientAsync(timeout.Token);
            using var stream = client.GetStream();
            using var reader = new System.IO.StreamReader(stream, Encoding.UTF8, false, 1024, true);
            string? line;
            while (!string.IsNullOrEmpty(line = await reader.ReadLineAsync(timeout.Token))) { }
            await Task.Delay(delay, timeout.Token);
            var body = Encoding.UTF8.GetBytes(JsonSerializer.Serialize(new { engine_root = root }));
            await stream.WriteAsync(Encoding.ASCII.GetBytes($"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {body.Length}\r\nConnection: close\r\n\r\n"), timeout.Token);
            await stream.WriteAsync(body, timeout.Token);
        }
        var served = Serve(ready, System.IO.Path.GetDirectoryName(path)!, 50);
        var foreignServed = Serve(foreign, System.IO.Path.Combine(System.IO.Path.GetTempPath(), "foreign-engine"), 0);
        try
        {
            var timer = Stopwatch.StartNew();
            var find = (Task<int?>)type.GetMethod("FindEngineAsync", flags)!.Invoke(engine, [timeout.Token, null, new[] { waitingPort, foreignPort, readyPort }])!;
            var found = await find;
            var elapsed = timer.Elapsed.TotalMilliseconds;
            check(found == readyPort, "较早响应的其他目录引擎不能抢占连接");
            check(elapsed < 450, $"已就绪引擎不等待无响应邻居的600毫秒超时，实际{elapsed:F1}毫秒");
            var report = Environment.GetEnvironmentVariable("JIYING_DISCOVERY_REPORT");
            if (!string.IsNullOrWhiteSpace(report))
                System.IO.File.WriteAllText(report, JsonSerializer.Serialize(new { elapsed_ms = elapsed, found_matching_engine = found == readyPort }));
            await Task.WhenAll(served, foreignServed);
            using var cancel = new CancellationTokenSource(50);
            var cancellationObserved = false;
            try
            {
                await (Task<int?>)type.GetMethod("FindEngineAsync", flags)!.Invoke(engine, [cancel.Token, null, new[] { waitingPort }])!;
            }
            catch (OperationCanceledException) { cancellationObserved = true; }
            check(cancellationObserved, "调用方取消后停止发现，不继续等待或建立连接");
        }
        finally { timeout.Cancel(); waiting.Stop(); ready.Stop(); foreign.Stop(); await blocked; }
    }
}
