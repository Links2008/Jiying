// 使用实际客户端源码与隔离回环 HTTP，检查大视频保存、取消和旧文件保护。
using System.Net;
using System.Net.Sockets;
using System.Reflection;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using Jiying.Desktop;

await NetworkRegression.Run();

const long largeSize = 128L * 1024 * 1024 + 17;
var directory = Path.Combine(Path.GetTempPath(), "jiying-artifact-regression-" + Guid.NewGuid().ToString("N"));
Directory.CreateDirectory(directory);
using var server = new ArtifactServer(largeSize);
try
{
    using (var client = Connect(server.Port))
    {
        GC.Collect();
        GC.WaitForPendingFinalizers();
        var before = GC.GetTotalAllocatedBytes(true);
        var output = Path.Combine(directory, "large.mp4");
        await Save(client, 1, output, CancellationToken.None);
        var allocated = GC.GetTotalAllocatedBytes(true) - before;
        Require(new FileInfo(output).Length == largeSize, "大视频长度不完整");
        await using var input = File.OpenRead(output);
        Require(Convert.ToHexString(await SHA256.HashDataAsync(input)) == server.ExpectedHash(largeSize), "大视频内容不完整");
        Console.WriteLine(JsonSerializer.Serialize(new { bytes = largeSize, allocated_bytes = allocated }));
        Require(allocated < 16L * 1024 * 1024, "保存大视频不应分配整部视频大小的内存");
    }

    using (var client = Connect(server.Port))
    {
        var http = (HttpClient)typeof(EngineClient).GetField("_api", BindingFlags.NonPublic | BindingFlags.Instance)!.GetValue(client)!;
        http.MaxResponseContentBufferSize = 1024 * 1024;
        var output = Path.Combine(directory, "bounded.mp4");
        await Save(client, 1, output, CancellationToken.None);
        Require(new FileInfo(output).Length == largeSize, "大文件不能受到字节数组响应缓冲上限限制");
    }

    var oldContent = Encoding.UTF8.GetBytes("previous-valid-video");
    var cancelled = Path.Combine(directory, "cancelled.mp4");
    await File.WriteAllBytesAsync(cancelled, oldContent);
    using (var client = Connect(server.Port))
    using (var stop = new CancellationTokenSource())
    {
        // 只传输一块，声明超过 2 GB，验证大响应可以进入流式接收并及时取消。
        var saving = Save(client, 2, cancelled, stop.Token);
        await server.Paused.Task.WaitAsync(TimeSpan.FromSeconds(3));
        stop.Cancel();
        try { await saving.WaitAsync(TimeSpan.FromSeconds(3)); throw new Exception("取消未生效"); }
        catch (OperationCanceledException) { }
        finally { server.Release.TrySetResult(); }
        Require((await File.ReadAllBytesAsync(cancelled)).SequenceEqual(oldContent), "取消覆盖了旧视频");
    }

    foreach (var id in new[] { 3, 4 })
    {
        var output = Path.Combine(directory, $"failure-{id}.mp4");
        await File.WriteAllBytesAsync(output, oldContent);
        using var client = Connect(server.Port);
        var failed = false;
        try { await Save(client, id, output, CancellationToken.None); }
        catch (Exception error) when (error is IOException or HttpRequestException or InvalidOperationException) { failed = true; }
        Require(failed, "传输截断或 HTTP 错误必须报告失败");
        Require((await File.ReadAllBytesAsync(output)).SequenceEqual(oldContent), "失败覆盖了旧视频");
    }

    using (var client = Connect(server.Port))
    {
        var output = Path.Combine(directory, "chunked.mp4");
        await Save(client, 5, output, CancellationToken.None);
        Require(new FileInfo(output).Length == ArtifactServer.SmallSize, "分块响应长度不完整");
    }

    using (var client = Connect(server.Port))
    {
        var output = Path.Combine(directory, "existing-directory.mp4");
        Directory.CreateDirectory(output);
        await File.WriteAllBytesAsync(Path.Combine(output, "keep.txt"), oldContent);
        var failed = false;
        try { await Save(client, 5, output, CancellationToken.None); }
        catch (Exception error) when (error is IOException or UnauthorizedAccessException) { failed = true; }
        Require(failed && File.Exists(Path.Combine(output, "keep.txt")), "替换失败不能删除已有内容");
    }
    Require(!Directory.GetFiles(directory, ".jiying-save-*.tmp").Any(), "临时下载稿未清理");
    Console.WriteLine("通过：128 MiB 内容与内存边界、响应缓冲上限、超过 2 GB 响应取消、截断、HTTP 错误、分块响应和替换失败保护。");
}
finally { Directory.Delete(directory, true); }

static EngineClient Connect(int port)
{
    var client = new EngineClient("unused-test-engine");
    typeof(EngineClient).GetProperty("Port")!.SetValue(client, port);
    return client;
}

static Task Save(EngineClient client, int id, string output, CancellationToken token)
{
    return client.SaveArtifactAsync(id, output, token);
}

static void Require(bool condition, string message)
{
    if (!condition) throw new Exception(message);
}

sealed class ArtifactServer : IDisposable
{
    public const int SmallSize = 1024 * 1024 + 17;
    private static readonly byte[] Block = Enumerable.Repeat((byte)0x5A, 64 * 1024).ToArray();
    private readonly HttpListener _listener = new();
    private readonly long _size;
    public int Port { get; }
    public TaskCompletionSource Paused { get; } = new(TaskCreationOptions.RunContinuationsAsynchronously);
    public TaskCompletionSource Release { get; } = new(TaskCreationOptions.RunContinuationsAsynchronously);

    public ArtifactServer(long size)
    {
        _size = size;
        using (var listener = new TcpListener(IPAddress.Loopback, 0))
        {
            listener.Start();
            Port = ((IPEndPoint)listener.LocalEndpoint).Port;
        }
        _listener.Prefixes.Add($"http://127.0.0.1:{Port}/");
        _listener.Start();
        _ = Listen();
    }

    private async Task Listen()
    {
        while (_listener.IsListening)
        {
            HttpListenerContext context;
            try { context = await _listener.GetContextAsync(); }
            catch (Exception error) when (error is HttpListenerException or ObjectDisposedException) { return; }
            _ = Respond(context);
        }
    }

    private async Task Respond(HttpListenerContext context)
    {
        try
        {
            var id = int.Parse(context.Request.Url!.AbsolutePath.Split('/')[4]);
            var response = context.Response;
            if (id == 4)
            {
                response.StatusCode = 404;
                var body = Encoding.UTF8.GetBytes("{\"detail\":\"未找到测试文件\"}");
                response.ContentLength64 = body.Length;
                await response.OutputStream.WriteAsync(body);
                return;
            }
            response.ContentType = "application/octet-stream";
            response.SendChunked = id == 5;
            if (!response.SendChunked) response.ContentLength64 = id == 2 ? 3L * 1024 * 1024 * 1024 : _size;
            var length = id == 5 ? SmallSize : _size;
            for (long sent = 0; sent < length;)
            {
                var count = (int)Math.Min(Block.Length, length - sent);
                await response.OutputStream.WriteAsync(Block.AsMemory(0, count));
                sent += count;
                if (id == 2)
                {
                    await response.OutputStream.FlushAsync();
                    Paused.TrySetResult();
                    await Release.Task;
                    return;
                }
                if (id == 3) return;
            }
        }
        catch (Exception error) when (error is IOException or HttpListenerException or ObjectDisposedException) { }
        finally { context.Response.Close(); }
    }

    public string ExpectedHash(long size)
    {
        using var hash = IncrementalHash.CreateHash(HashAlgorithmName.SHA256);
        for (long sent = 0; sent < size;)
        {
            var count = (int)Math.Min(Block.Length, size - sent);
            hash.AppendData(Block, 0, count);
            sent += count;
        }
        return Convert.ToHexString(hash.GetHashAndReset());
    }

    public void Dispose()
    {
        Release.TrySetResult();
        _listener.Close();
    }
}
