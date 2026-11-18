// 用实际 WPF 窗口与隔离 HTTP 处理器验收任务列表的增量同步。
using System.Net;
using System.Net.Http;
using System.Text;
using System.Text.Json;
using System.Windows.Controls;
using Jiying.Desktop;

internal static class TaskRefreshRegression
{

    public static async Task Run(MainWindow window)
    {
        var engine = window.Engine;
        ((HttpClient)engine.RequestTransport).Dispose();
        using var handler = new TaskStateHandler();
        engine.RequestTransport = new HttpClient(handler);
        Task<bool> Refresh() => window.RefreshTasksAsync();
        Require(await Refresh(), "首次任务快照请求失败");
        Require(handler.FullRequests == 1 && handler.TargetedRequests == 0 && handler.DetailRequests == 0 &&
                window.DoneTasks.Count == 1500 && window.QueueTasks.Count == 1, "1500 条历史应由单个请求载入");
        var card = window.QueueTasks.Single();
        var original = window.DoneTasks.First(t => t.Id == "edit-race");
        var history = (ListView)window.FindName("DoneList");
        history.SelectedItem = original;
        var notified = new List<string?>();
        card.PropertyChanged += (_, args) => notified.Add(args.PropertyName);

        for (var index = 1; index <= 40; index++)
            Feed(window, new { type = "progress", task_id = "active", sequence = 10 + index,
                stage = "download", fraction = (index + 40) / 100d, speed_bps = 1024 * 512 });
        await WaitUntil(() => Math.Abs(card.ProgressValue - 80) < .01, "合并进度未应用最后一帧");
        Require(handler.FullRequests == 1 && handler.TargetedRequests == 0 &&
                ReferenceEquals(card, window.QueueTasks.Single()) && notified.Count(name => name == nameof(TaskItem.ProgressValue)) == 1,
                "高频进度刷新了整表、重复通知或替换了任务卡");
        Feed(window, new { type = "progress", task_id = "active", sequence = 49, stage = "download", fraction = .1 });
        await Task.Delay(140);
        Require(Math.Abs(card.ProgressValue - 80) < .01, "乱序进度覆盖了当前值");

        handler.Sequence = 51;
        handler.Completed = true;
        Feed(window, new { type = "task", task_id = "active", sequence = 51, status = "completed" });
        await WaitUntil(() => window.QueueTasks.Count == 0 && window.DoneTasks.Count == 1501, "终态未移动到已完成");
        Require(handler.TargetedRequests == 1 && handler.FullRequests == 1 && handler.DetailRequests == 0 &&
                ReferenceEquals(window.DoneTasks.First(task => task.Id == "active"), card) &&
                ReferenceEquals(history.SelectedItem, original), "完成任务应只读取对应摘要并保留历史选中项");

        handler.BlockNextSnapshot = true;
        var stale = Refresh();
        await handler.SnapshotStarted.Task.WaitAsync(TimeSpan.FromSeconds(5));
        var artifact = new ArtifactItem { Id = 77, Kind = "subtitle", Path = "edited.srt", Language = "en", TaskId = "edit-race" };
        window.EditingSubtitle = artifact;
        ((TextBox)window.FindName("SubtitleTextBox")).Text = "edited-subtitle";
        var saving = window.SaveSubtitleAsync();
        Require(await saving, "隔离字幕保存失败");
        handler.ReleaseSnapshot.TrySetResult();
        Require(await stale, "隔离的旧快照请求失败");
        await WaitUntil(() => original.Artifacts.Single().Size == 40, "在途旧快照覆盖了字幕保存");
        Require(handler.TargetedRequests == 2 && handler.DetailRequests == 0 && ReferenceEquals(history.SelectedItem, original),
                "字幕保存后应只刷新对应卡片，并保留选中项");

        // 模拟事件遗漏后连接仍在。心跳上的序号必须主动触发恢复。
        handler.Sequence = 55;
        handler.Removed = true;
        Feed(window, null, 55);
        await WaitUntil(() => window.DoneTasks.Count == 1500 && !window.DoneTasks.Any(t => t.Id == "active"),
            "心跳发现事件缺口后未恢复删除");
        Require(handler.FullRequests == 3 && handler.DetailRequests == 0, "恢复应只需一个批量快照");
        Console.WriteLine("通过：1500 条历史只发 1 次请求；40 条进度合为 1 次绑定更新且 0 次查询；终态、字幕保存、乱序和心跳缺口恢复通过。");
    }

    private static void Feed(MainWindow window, object? payload, long? head = null)
    {
        var item = payload is null ? null : JsonSerializer.Deserialize<TaskEvent>(JsonSerializer.Serialize(payload));
        window.ReceiveTaskEvent(new TaskEventFrame(item, head));
    }

    private static async Task WaitUntil(Func<bool> predicate, string message)
    {
        var end = DateTime.UtcNow + TimeSpan.FromSeconds(4);
        while (!predicate() && DateTime.UtcNow < end) await Task.Delay(20);
        Require(predicate(), message);
    }

    private static void Require(bool condition, string message) { if (!condition) throw new Exception(message); }

    private sealed class TaskStateHandler : HttpMessageHandler
    {
        public TaskCompletionSource SnapshotStarted { get; } = new(TaskCreationOptions.RunContinuationsAsynchronously);
        public TaskCompletionSource ReleaseSnapshot { get; } = new(TaskCreationOptions.RunContinuationsAsynchronously);
        public int DetailRequests { get; private set; }
        public int FullRequests { get; private set; }
        public int TargetedRequests { get; private set; }
        public int Sequence { get; set; } = 10;
        public bool BlockNextSnapshot { get; set; }
        public bool Completed { get; set; }
        public bool Removed { get; set; }
        private int _size = 1;

        protected override async Task<HttpResponseMessage> SendAsync(HttpRequestMessage request, CancellationToken token)
        {
            var path = request.RequestUri!.AbsolutePath;
            if (request.Method == HttpMethod.Put && path == "/api/v1/artifacts/77/content")
            {
                _size = 40;
                Sequence++;
                return Response("{\"ok\":true}");
            }
            if (path != "/api/v1/tasks")
            {
                DetailRequests++;
                return new HttpResponseMessage(HttpStatusCode.NotFound);
            }
            var query = Uri.UnescapeDataString(request.RequestUri.Query);
            Require(query.Contains("include_artifacts=true"), "列表请求必须附带产物");
            var target = query.Split('&').FirstOrDefault(part => part.StartsWith("ids=", StringComparison.Ordinal))?[4..];
            if (target is null) FullRequests++;
            else TargetedRequests++;
            var ids = target?.Split(',') ?? Enumerable.Range(0, 1499).Select(index => $"history-{index}")
                .Concat(["edit-race", "active"]).ToArray();
            var cards = ids.Where(id => id != "active" || !Removed).Select(id => new
            {
                id, title = id, status = id != "active" || Completed ? "completed" : "running",
                created_at = "2026-10-13T00:00:00", subtitle_mode = "none",
                stages = id == "active" ? new[] { new { stage = "download", status = Completed ? "done" : "running" } } : [],
                progress = id == "active" && !Completed ? new { stage = "download", fraction = .2 } : null,
                artifacts = id == "edit-race"
                    ? new[] { new { id = 77, kind = "subtitle", path = "edited.srt", lang = "en", size = _size } } : []
            }).ToArray();
            var response = Response(JsonSerializer.Serialize(cards).Replace("\"progress\":null", "\"progress\":{}"));
            response.Headers.Add("X-Jiying-Event-Sequence", Sequence.ToString());
            if (BlockNextSnapshot)
            {
                BlockNextSnapshot = false;
                SnapshotStarted.TrySetResult();
                await ReleaseSnapshot.Task.WaitAsync(token);
            }
            return response;
        }

        private static HttpResponseMessage Response(string body) => new(HttpStatusCode.OK)
        {
            Content = new StringContent(body, Encoding.UTF8, "application/json")
        };
    }
}
