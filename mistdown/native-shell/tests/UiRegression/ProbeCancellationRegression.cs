using System.IO;
using System.Net;
using System.Net.Sockets;
using System.Text;
using System.Windows;
using System.Windows.Controls;
using Jiying.Desktop;

internal static class ProbeCancellationRegression
{
    internal static async Task Run(MainWindow window, Action<bool, string> check)
    {
        var engine = window.Engine;
        var previousPort = engine.Port;
        var listener = new TcpListener(IPAddress.Loopback, 0);
        listener.Start();
        using var timeout = new CancellationTokenSource(TimeSpan.FromSeconds(5));
        var received = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
        var release = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
        var server = Task.Run(async () =>
        {
            using var client = await listener.AcceptTcpClientAsync(timeout.Token);
            using var stream = client.GetStream();
            using var reader = new StreamReader(stream, Encoding.UTF8, false, 1024, true);
            var request = await reader.ReadLineAsync(timeout.Token);
            string? header;
            while (!string.IsNullOrEmpty(header = await reader.ReadLineAsync(timeout.Token))) { }
            if (request?.Contains("/api/v1/probe") != true) throw new Exception("Unexpected probe request");
            received.TrySetResult();
            await release.Task.WaitAsync(timeout.Token);
            var body = Encoding.UTF8.GetBytes("""{"title":"Stale result","formats":[{"format_id":"test","label":"Test"}]}""");
            try
            {
                await stream.WriteAsync(Encoding.ASCII.GetBytes($"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {body.Length}\r\nConnection: close\r\n\r\n"), timeout.Token);
                await stream.WriteAsync(body, timeout.Token);
            }
            catch (IOException) { } // Cancellation may already have closed the client socket.
        });
        try
        {
            engine.Port = ((IPEndPoint)listener.LocalEndpoint).Port;
            window.UrlBox.Text = "https://example.com/late-result";
            var parsing = window.ProbeAsync(false);
            await received.Task.WaitAsync(timeout.Token);
            window.UrlBox.Clear();
            release.TrySetResult();
            await Task.WhenAll(parsing, server).WaitAsync(timeout.Token);
            await Task.Delay(400);
            check(window.ProbeResult is null && window.ProbePanel.Visibility == Visibility.Collapsed &&
                  window.ParseContent.VerticalAlignment == VerticalAlignment.Center,
                  "Late HTTP probe results cannot restore a URL that the user cleared");
        }
        finally { listener.Stop(); engine.Port = previousPort; release.TrySetResult(); }
    }
}
