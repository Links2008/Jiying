// 删除确认捕获目标与弹窗，晚到结果不能关闭新的弹窗或扩大批量确认范围。
using System.Collections.Concurrent;
using System.Net;
using System.Net.Http;
using System.Reflection;
using System.Text;
using System.Windows;
using System.Windows.Controls;
using Jiying.Desktop;

internal static class DeleteLifecycleRegression
{
    private const BindingFlags Flags = BindingFlags.Instance | BindingFlags.NonPublic;

    public static async Task Run(MainWindow window)
    {
        await RunLateResult(window);
        await RunBatchSnapshot(window);
    }

    private static async Task RunLateResult(MainWindow window)
    {
        using var handler = new DeleteHandler(pauseDelete: true);
        Install(window, handler);
        var cache = (ConcurrentDictionary<string, TaskItem>)typeof(MainWindow).GetField("_terminalDetails", Flags)!.GetValue(window)!;
        cache.Clear();
        cache["confirmed-a"] = MakeTask("confirmed-a", [new ArtifactItem { Id = 301, Kind = "subtitle", Path = "a.srt" }]);
        cache["unreviewed-b"] = MakeTask("unreviewed-b", []);
        Show(window, cache["confirmed-a"], false);
        ((RadioButton)window.FindName("DeleteSubtitleChoice")).IsChecked = true;
        typeof(MainWindow).GetMethod("ConfirmDelete_Click", Flags)!.Invoke(window, [window, new RoutedEventArgs()]);
        await handler.DeleteStarted.Task.WaitAsync(TimeSpan.FromSeconds(5));
        typeof(MainWindow).GetMethod("CloseModal", Flags)!.Invoke(window, null);
        await Until(() => !Visible(window), "删除中的旧弹窗没有关闭");
        Show(window, MakeTask("unreviewed-b", []), false);
        Require(((Button)window.FindName("ConfirmDeleteButton")).IsEnabled, "新确认弹窗继承了旧删除的禁用按钮");
        ((RadioButton)window.FindName("DeleteSubtitleChoice")).IsChecked = true;
        typeof(MainWindow).GetMethod("ConfirmDelete_Click", Flags)!.Invoke(window, [window, new RoutedEventArgs()]);
        await handler.DeleteBStarted.Task.WaitAsync(TimeSpan.FromSeconds(5));
        handler.ReleaseDelete.TrySetResult();
        await Until(() => handler.ListRequests > 0 && ((SemaphoreSlim)typeof(MainWindow).GetField("_refreshLock", Flags)!.GetValue(window)!).CurrentCount == 1,
            "旧删除没有刷新完成");
        await Task.Delay(250);
        Require(Visible(window) && ((FrameworkElement)window.FindName("DeleteDialog")).Visibility == Visibility.Visible,
            "旧删除请求完成后关闭了新任务的确认弹窗");
        Require(window.DoneTasks.Single(t => t.Id == "confirmed-a").Artifacts.Count == 0,
            "旧删除请求失效了新任务缓存，原任务继续显示已删除的字幕");
        ((Button)window.FindName("ConfirmDeleteButton")).RaiseEvent(new RoutedEventArgs(Button.ClickEvent));
        Require(handler.Requested.Count(id => id == "unreviewed-b") == 1,
            "旧删除收尾解锁了新弹窗的在途删除，导致重复提交");
        Require(!((Button)window.FindName("ConfirmDeleteButton")).IsEnabled, "新删除尚未结束却已允许重新提交");
        Require(handler.Deleted.SequenceEqual(["confirmed-a"]), "删除请求用了另一弹窗的目标");
        handler.ReleaseDeleteB.TrySetResult();
        await Until(() => !Visible(window), "新删除完成后弹窗没有关闭");
        Require(handler.Deleted.SequenceEqual(["confirmed-a", "unreviewed-b"]), "实际删除数量与两个确认不一致");
        Console.WriteLine("通过：实际 WPF 旧删除保持确认目标与缓存，不关闭或解锁新弹窗的在途删除。");
    }

    private static async Task RunBatchSnapshot(MainWindow window)
    {
        using var handler = new DeleteHandler(pauseDelete: false);
        Install(window, handler);
        Show(window, MakeTask("confirmed-a", []), true);
        typeof(MainWindow).GetMethod("ConfirmDelete_Click", Flags)!.Invoke(window, [window, new RoutedEventArgs()]);
        await Until(() => !Visible(window), "批量确认弹窗没有关闭");
        Require(handler.Deleted.SequenceEqual(["confirmed-a"]), "批量清理扩大到确认列表以外的新完成任务");
        Require(handler.ConfirmedIds?.SequenceEqual(["confirmed-a"]) == true,
            "批量请求没有传递界面确认的任务快照");
        Console.WriteLine("通过：实际 WPF 批量清理仅发送确认时任务编号，不含随后新完成任务。");
    }

    private static void Install(MainWindow window, HttpMessageHandler handler)
    {
        var engine = typeof(MainWindow).GetField("_engine", Flags)!.GetValue(window)!;
        var field = engine.GetType().GetField("_api", Flags)!;
        ((HttpClient)field.GetValue(engine)!).Dispose();
        field.SetValue(engine, new HttpClient(handler));
    }

    private static TaskItem MakeTask(string id, List<ArtifactItem> artifacts) => new()
    {
        Id = id, Title = id, Status = "completed", FinishedAt = "unchanged", Artifacts = artifacts
    };
    private static void Show(MainWindow window, TaskItem task, bool batch) => typeof(MainWindow)
        .GetMethod("ShowDeleteDialog", Flags)!.Invoke(window, [new[] { task }, batch]);
    private static bool Visible(MainWindow window) => ((FrameworkElement)window.FindName("ModalOverlay")).Visibility == Visibility.Visible;
    private static void Require(bool condition, string message) { if (!condition) throw new Exception(message); }
    private static async Task Until(Func<bool> predicate, string message)
    {
        var deadline = DateTime.UtcNow + TimeSpan.FromSeconds(5);
        while (!predicate() && DateTime.UtcNow < deadline) await System.Threading.Tasks.Task.Delay(20);
        Require(predicate(), message);
    }

    private sealed class DeleteHandler(bool pauseDelete) : HttpMessageHandler
    {
        public TaskCompletionSource DeleteStarted { get; } = new(TaskCreationOptions.RunContinuationsAsynchronously);
        public TaskCompletionSource ReleaseDelete { get; } = new(TaskCreationOptions.RunContinuationsAsynchronously);
        public TaskCompletionSource DeleteBStarted { get; } = new(TaskCreationOptions.RunContinuationsAsynchronously);
        public TaskCompletionSource ReleaseDeleteB { get; } = new(TaskCreationOptions.RunContinuationsAsynchronously);
        public int ListRequests { get; private set; }
        public List<string> Requested { get; } = [];
        public List<string> Deleted { get; } = [];
        public string[]? ConfirmedIds { get; private set; }
        private bool _deletedSubtitle;

        protected override async Task<HttpResponseMessage> SendAsync(HttpRequestMessage request, CancellationToken cancellationToken)
        {
            var path = request.RequestUri!.AbsolutePath;
            if (request.Method == HttpMethod.Delete)
            {
                var id = path.Split('/')[4];
                Requested.Add(id);
                if (id == "confirmed-a")
                {
                    DeleteStarted.TrySetResult();
                    if (pauseDelete) await ReleaseDelete.Task;
                }
                else
                {
                    DeleteBStarted.TrySetResult();
                    if (pauseDelete) await ReleaseDeleteB.Task;
                }
                Deleted.Add(id);
                _deletedSubtitle = true;
                return Response("{\"deleted_files\":[\"a.srt\"],\"skipped\":[]}");
            }
            if (path == "/api/v1/tasks/clear_done")
            {
                using var document = System.Text.Json.JsonDocument.Parse(await request.Content!.ReadAsStringAsync(cancellationToken));
                if (document.RootElement.TryGetProperty("task_ids", out var ids) && ids.ValueKind == System.Text.Json.JsonValueKind.Array)
                    ConfirmedIds = ids.EnumerateArray().Select(v => v.GetString()!).ToArray();
                Deleted.AddRange(ConfirmedIds ?? ["confirmed-a", "unreviewed-b"]);
                return Response("{\"deleted_files\":[\"a.srt\"],\"skipped\":[]}");
            }
            if (path == "/api/v1/tasks")
            {
                ListRequests++;
                return Response("[" + Summary("confirmed-a") + "," + Summary("unreviewed-b") + "]");
            }
            var target = path.Split('/').Last();
            var artifacts = target == "confirmed-a" && !_deletedSubtitle
                ? ",\"artifacts\":[{\"id\":301,\"kind\":\"subtitle\",\"path\":\"a.srt\"}]" : ",\"artifacts\":[]";
            return Response(Summary(target).TrimEnd('}') + artifacts + "}");
        }

        private static string Summary(string id) => "{\"id\":\"" + id + "\",\"title\":\"" + id +
            "\",\"status\":\"completed\",\"finished_at\":\"unchanged\"}";
        private static HttpResponseMessage Response(string body) => new(HttpStatusCode.OK)
        {
            Content = new StringContent(body, Encoding.UTF8, "application/json")
        };
        protected override void Dispose(bool disposing)
        {
            ReleaseDelete.TrySetResult();
            ReleaseDeleteB.TrySetResult();
            base.Dispose(disposing);
        }
    }
}
