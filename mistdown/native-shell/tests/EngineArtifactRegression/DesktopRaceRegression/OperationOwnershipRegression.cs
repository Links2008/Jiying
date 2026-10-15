// 重生成提交后仍等待刷新；旧操作收尾不能解锁新字幕的在途保存。
using System.Net;
using System.Net.Http;
using System.Reflection;
using System.Text;
using System.Windows;
using System.Windows.Controls;
using Jiying.Desktop;

internal static class OperationOwnershipRegression
{
    private const BindingFlags Flags = BindingFlags.Instance | BindingFlags.NonPublic;

    public static async Task Run(MainWindow window)
    {
        var engine = typeof(MainWindow).GetField("_engine", Flags)!.GetValue(window)!;
        var apiField = engine.GetType().GetField("_api", Flags)!;
        ((HttpClient)apiField.GetValue(engine)!).Dispose();
        using var handler = new OwnershipHandler();
        apiField.SetValue(engine, new HttpClient(handler));
        var click = typeof(MainWindow).GetMethod("Artifact_Click", Flags)!;
        var first = new ArtifactItem { Id = 201, Kind = "subtitle", Path = "first.srt", Language = "en", TaskId = "ownership-a" };
        var second = new ArtifactItem { Id = 202, Kind = "subtitle", Path = "second.srt", Language = "en", TaskId = "ownership-b" };
        var text = (TextBox)window.FindName("SubtitleTextBox");
        var save = (Button)window.FindName("SubtitleSaveButton");
        try
        {
            click.Invoke(window, [new Button { Tag = first }, new RoutedEventArgs()]);
            await WaitUntil(() => text.Text == "baseline-a" && Visible(window), "第一份字幕没有打开");
            text.Text = "saved-caption-a";
            typeof(MainWindow).GetMethod("RerenderSubtitle_Click", Flags)!.Invoke(window, [window, new RoutedEventArgs()]);
            await handler.ListStarted.Task.WaitAsync(TimeSpan.FromSeconds(5));
            await WaitUntil(() => !Visible(window), "已提交的重生成没有关闭编辑器");
            click.Invoke(window, [new Button { Tag = second }, new RoutedEventArgs()]);
            await WaitUntil(() => text.Text == "baseline-b" && Visible(window), "第二份字幕没有打开");
            text.Text = "saved-caption-b";
            save.RaiseEvent(new RoutedEventArgs(Button.ClickEvent));
            await handler.SaveBStarted.Task.WaitAsync(TimeSpan.FromSeconds(5));
            handler.ReleaseList.TrySetResult();
            await WaitUntil(() => ((SemaphoreSlim)typeof(MainWindow).GetField("_refreshLock", Flags)!.GetValue(window)!).CurrentCount == 1,
                "旧重生成的刷新没有结束");
            await Task.Delay(100);
            save.RaiseEvent(new RoutedEventArgs(Button.ClickEvent));
            Require(handler.SaveBRequests == 1, "旧重生成收尾解锁了另一字幕的在途保存，产生重复写入请求");
            Require(text.IsReadOnly && !save.IsEnabled && (bool)typeof(MainWindow).GetField("_subtitleBusy", Flags)!.GetValue(window)!,
                "在途新保存被旧重生成解锁");
            handler.ReleaseSaveB.TrySetResult();
            await WaitUntil(() => !(bool)typeof(MainWindow).GetField("_subtitleBusy", Flags)!.GetValue(window)!, "新字幕保存没有结束");
            Require(handler.SavedBText == "saved-caption-b" && text.Text == "saved-caption-b", "保存到错误的字幕或覆盖新内容");
            typeof(MainWindow).GetMethod("CloseModal", Flags)!.Invoke(window, null);
            await WaitUntil(() => !Visible(window), "已保存的字幕编辑器不能关闭");
            Console.WriteLine("通过：实际 WPF 旧重生成刷新收尾不能解锁另一字幕的在途保存或产生重复写入。");
        }
        finally
        {
            handler.ReleaseList.TrySetResult();
            handler.ReleaseSaveB.TrySetResult();
        }
    }

    private static bool Visible(MainWindow window) => ((FrameworkElement)window.FindName("ModalOverlay")).Visibility == Visibility.Visible;
    private static void Require(bool condition, string message) { if (!condition) throw new Exception(message); }
    private static async Task WaitUntil(Func<bool> predicate, string message)
    {
        var deadline = DateTime.UtcNow + TimeSpan.FromSeconds(5);
        while (!predicate() && DateTime.UtcNow < deadline) await Task.Delay(20);
        Require(predicate(), message);
    }

    private sealed class OwnershipHandler : HttpMessageHandler
    {
        public TaskCompletionSource ListStarted { get; } = new(TaskCreationOptions.RunContinuationsAsynchronously);
        public TaskCompletionSource ReleaseList { get; } = new(TaskCreationOptions.RunContinuationsAsynchronously);
        public TaskCompletionSource SaveBStarted { get; } = new(TaskCreationOptions.RunContinuationsAsynchronously);
        public TaskCompletionSource ReleaseSaveB { get; } = new(TaskCreationOptions.RunContinuationsAsynchronously);
        public int SaveBRequests { get; private set; }
        public string SavedBText { get; private set; } = "";

        protected override async Task<HttpResponseMessage> SendAsync(HttpRequestMessage request, CancellationToken cancellationToken)
        {
            var path = request.RequestUri!.AbsolutePath;
            if (request.Method == HttpMethod.Put && path == "/api/v1/artifacts/202/content")
            {
                SaveBRequests++;
                using var document = System.Text.Json.JsonDocument.Parse(await request.Content!.ReadAsStringAsync(cancellationToken));
                var text = document.RootElement.GetProperty("text").GetString()!;
                SaveBStarted.TrySetResult();
                await ReleaseSaveB.Task;
                SavedBText = text;
                return Response("{\"ok\":true}");
            }
            if (path == "/api/v1/tasks")
            {
                ListStarted.TrySetResult();
                await ReleaseList.Task;
                return Response("[]");
            }
            if (request.Method == HttpMethod.Get) return Response(path.Contains("/201/", StringComparison.Ordinal)
                ? "{\"text\":\"baseline-a\"}" : "{\"text\":\"baseline-b\"}");
            return Response("{\"ok\":true}");
        }

        private static HttpResponseMessage Response(string body) => new(HttpStatusCode.OK)
        {
            Content = new StringContent(body, Encoding.UTF8, "application/json")
        };
    }
}
