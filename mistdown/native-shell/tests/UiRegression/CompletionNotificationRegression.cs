// 模拟刚创建就已完成的任务，验证提醒去重；系统注册在此隔离宿主中关闭。
using System.Net;
using System.Net.Http;
using System.Text;
using System.Text.Json;
using System.Windows.Controls;
using Jiying.Desktop;

internal static class CompletionNotificationRegression
{
    public static async Task Run(MainWindow window, Action<bool, string> check)
    {
        var engine = window.Engine;
        var previous = engine.RequestTransport;
        var previousPort = engine.Port;
        var ui = window.Settings;
        var previousNotify = ui.Notify;
        using var handler = new Handler();
        using var client = new HttpClient(handler);
        engine.RequestTransport = client;
        engine.Port = 12345;
        window.TaskList.Clear();
        var toast = (StackPanel)window.FindName("ToastHost");
        toast.Children.Clear();
        ui.Notify = true;
        try
        {
            async Task Refresh() => await window.RefreshTasksAsync();
            handler.Tasks = [new() { Id = "history", Title = "旧任务", Status = "completed" }];
            await Refresh();
            check(toast.Children.Count == 0, "首次加载的完成历史不会重复通知");
            handler.Tasks.Add(new() { Id = "fast-completed", Title = "快速完成任务", Status = "completed" });
            await Refresh();
            check(toast.Children.Count == 1, "未经过进行中轮询的快速完成任务仍能通知");
            await Refresh();
            check(toast.Children.Count == 1, "重复刷新同一次完成不会重复通知");
            handler.Tasks[1].Status = "running";
            await Refresh();
            handler.Tasks[1].Status = "completed";
            await Refresh();
            check(toast.Children.Count == 2, "重新生成结束后可再次发出完成提醒");
            ui.Notify = false;
            handler.Tasks.Add(new() { Id = "disabled-completed", Status = "completed" });
            await Refresh();
            check(toast.Children.Count == 2, "关闭完成通知后不会发送提醒");
        }
        finally
        {
            engine.RequestTransport = previous;
            engine.Port = previousPort;
            ui.Notify = previousNotify;
            toast.Children.Clear();
            window.TaskList.Clear();
        }
    }

    private sealed class Handler : HttpMessageHandler
    {
        internal List<TaskItem> Tasks = [];
        protected override Task<HttpResponseMessage> SendAsync(HttpRequestMessage request, CancellationToken token)
        {
            var path = request.RequestUri!.AbsolutePath;
            var body = path == "/api/v1/tasks" ? JsonSerializer.Serialize(Tasks) :
                JsonSerializer.Serialize(Tasks.Single(t => t.Id == path.Split('/').Last()));
            return Task.FromResult(new HttpResponseMessage(HttpStatusCode.OK)
            { Content = new StringContent(body, Encoding.UTF8, "application/json") });
        }
    }
}
