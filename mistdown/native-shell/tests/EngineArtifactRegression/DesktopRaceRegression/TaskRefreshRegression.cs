// 用实际窗口重现：字幕保存使缓存失效后，在途旧详情不能再次长期进入缓存。
using System.Net;
using System.Net.Http;
using System.Reflection;
using System.Text;
using System.Windows.Controls;
using Jiying.Desktop;

internal static class TaskRefreshRegression
{
    private const BindingFlags Flags = BindingFlags.Instance | BindingFlags.NonPublic;

    public static async Task Run(MainWindow window)
    {
        var engine = typeof(MainWindow).GetField("_engine", Flags)!.GetValue(window)!;
        var apiField = engine.GetType().GetField("_api", Flags)!;
        ((HttpClient)apiField.GetValue(engine)!).Dispose();
        using var handler = new TaskStateHandler();
        apiField.SetValue(engine, new HttpClient(handler));
        window.QueueTasks.Clear();
        window.DoneTasks.Clear();
        var cache = (System.Collections.Concurrent.ConcurrentDictionary<string, TaskItem>)typeof(MainWindow)
            .GetField("_terminalDetails", Flags)!.GetValue(window)!;
        cache.Clear();
        var refreshMethod = typeof(MainWindow).GetMethod("RefreshTasksAsync", Flags)!;
        var refresh = (Task)refreshMethod.Invoke(window, null)!;
        await handler.DetailStarted.Task.WaitAsync(TimeSpan.FromSeconds(5));
        var artifact = new ArtifactItem { Id = 77, Kind = "subtitle", Path = "edited.srt", Language = "en", TaskId = "edit-race" };
        typeof(MainWindow).GetField("_editingArtifact", Flags)!.SetValue(window, artifact);
        ((TextBox)window.FindName("SubtitleTextBox")).Text = "edited-subtitle";
        var saving = (Task<bool>)typeof(MainWindow).GetMethod("SaveSubtitleAsync", Flags)!.Invoke(window, null)!;
        Require(await saving, "隔离字幕保存没有完成");
        handler.ReleaseDetail.TrySetResult();
        await refresh.WaitAsync(TimeSpan.FromSeconds(5));
        await ((Task)refreshMethod.Invoke(window, null)!).WaitAsync(TimeSpan.FromSeconds(5));
        Require(handler.DetailRequests >= 2, "字幕保存后，晚到的旧详情再次进入缓存，刷新没有重新读取");
        Require(window.DoneTasks.Single().Artifacts.Single().Size == 40,
            "刷新持续显示字幕保存前的旧产物信息");
        Console.WriteLine("通过：实际 WPF 字幕保存与在途详情请求交错后，刷新使用新产物信息。");
        await RunQueued(window);
    }

    private static async Task RunQueued(MainWindow window)
    {
        await WaitUntil(() => (int)typeof(MainWindow).GetField("_refreshQueued", Flags)!.GetValue(window)! == 0,
            "上一轮隔离刷新没有结束");
        var engine = typeof(MainWindow).GetField("_engine", Flags)!.GetValue(window)!;
        var apiField = engine.GetType().GetField("_api", Flags)!;
        ((HttpClient)apiField.GetValue(engine)!).Dispose();
        using var handler = new TaskStateHandler();
        apiField.SetValue(engine, new HttpClient(handler));
        window.DoneTasks.Clear();
        ((System.Collections.Concurrent.ConcurrentDictionary<string, TaskItem>)typeof(MainWindow)
            .GetField("_terminalDetails", Flags)!.GetValue(window)!).Clear();
        var queueRefresh = typeof(MainWindow).GetMethod("QueueRefresh", Flags)!;
        queueRefresh.Invoke(window, null);
        await handler.DetailStarted.Task.WaitAsync(TimeSpan.FromSeconds(5));
        var artifact = new ArtifactItem { Id = 77, Kind = "subtitle", Path = "edited.srt", Language = "en", TaskId = "edit-race" };
        typeof(MainWindow).GetField("_editingArtifact", Flags)!.SetValue(window, artifact);
        ((TextBox)window.FindName("SubtitleTextBox")).Text = "edited-again-subtitle";
        Require(await (Task<bool>)typeof(MainWindow).GetMethod("SaveSubtitleAsync", Flags)!.Invoke(window, null)!,
            "隔离字幕保存没有完成");
        // SSE 在当前刷新尚未结束时再发通知，仍必须保留一次后续读取。
        queueRefresh.Invoke(window, null);
        handler.ReleaseDetail.TrySetResult();
        await WaitUntil(() => handler.DetailRequests >= 2 && window.DoneTasks.FirstOrDefault()?.Artifacts.FirstOrDefault()?.Size == 40,
            "刷新进行中收到的新请求被丢弃，任务与产物仍没有同步");
        await WaitUntil(() => (int)typeof(MainWindow).GetField("_refreshQueued", Flags)!.GetValue(window)! == 0,
            "隔离事件刷新没有结束");
        Console.WriteLine("通过：实际 WPF 刷新进行中收到新通知，保留并执行后续刷新。");
    }

    private static async Task WaitUntil(Func<bool> predicate, string message)
    {
        var end = DateTime.UtcNow + TimeSpan.FromSeconds(3);
        while (!predicate() && DateTime.UtcNow < end) await Task.Delay(20);
        Require(predicate(), message);
    }

    private static void Require(bool condition, string message) { if (!condition) throw new Exception(message); }

    private sealed class TaskStateHandler : HttpMessageHandler
    {
        public TaskCompletionSource DetailStarted { get; } = new(TaskCreationOptions.RunContinuationsAsynchronously);
        public TaskCompletionSource ReleaseDetail { get; } = new(TaskCreationOptions.RunContinuationsAsynchronously);
        public int DetailRequests { get; private set; }
        private int _size = 1;

        protected override async Task<HttpResponseMessage> SendAsync(HttpRequestMessage request, CancellationToken cancellationToken)
        {
            var path = request.RequestUri!.AbsolutePath;
            if (request.Method == HttpMethod.Put && path == "/api/v1/artifacts/77/content")
            {
                _size = 40;
                return Response("{\"ok\":true}");
            }
            const string task = "\"id\":\"edit-race\",\"status\":\"completed\",\"finished_at\":\"2026-10-13T00:00:00\",\"title\":\"isolated-task\"";
            if (path == "/api/v1/tasks") return Response("[{" + task + "}]");
            if (path == "/api/v1/tasks/edit-race")
            {
                // 服务端已生成旧响应，但传输尚未完成；随后保存不改变任务完成时间。
                var size = _size;
                DetailRequests++;
                if (DetailRequests == 1)
                {
                    DetailStarted.TrySetResult();
                    await ReleaseDetail.Task;
                }
                return Response("{" + task + ",\"artifacts\":[{\"id\":77,\"kind\":\"subtitle\",\"path\":\"edited.srt\",\"lang\":\"en\",\"size\":" + size + "}]}");
            }
            return Response("{\"text\":\"edited-subtitle\"}");
        }

        private static HttpResponseMessage Response(string body) => new(HttpStatusCode.OK)
        {
            Content = new StringContent(body, Encoding.UTF8, "application/json")
        };

        protected override void Dispose(bool disposing)
        {
            ReleaseDetail.TrySetResult();
            base.Dispose(disposing);
        }
    }
}
