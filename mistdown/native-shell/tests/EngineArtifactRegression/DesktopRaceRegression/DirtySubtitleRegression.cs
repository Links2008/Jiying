// 实际 WPF 编辑器关闭时必须保护未保存内容；仅自动回复测试自身的原生确认框。
using System.Net;
using System.Net.Http;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Text;
using System.Windows;
using System.Windows.Controls;
using Jiying.Desktop;

internal static class DirtySubtitleRegression
{
    private const BindingFlags Flags = BindingFlags.Instance | BindingFlags.NonPublic;

    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    private static extern nint FindWindow(string className, string title);

    [DllImport("user32.dll")]
    private static extern bool PostMessage(nint window, uint message, nint wParam, nint lParam);

    [DllImport("user32.dll")]
    private static extern nint GetWindow(nint window, uint command);

    public static async Task Run(MainWindow window)
    {
        var engine = typeof(MainWindow).GetField("_engine", Flags)!.GetValue(window)!;
        var apiField = engine.GetType().GetField("_api", Flags)!;
        ((HttpClient)apiField.GetValue(engine)!).Dispose();
        using var handler = new SubtitleHandler();
        apiField.SetValue(engine, new HttpClient(handler));
        var click = typeof(MainWindow).GetMethod("Artifact_Click", Flags)!;
        var artifact = new ArtifactItem { Id = 91, Kind = "subtitle", Path = "draft.srt", Language = "en", TaskId = "dirty-editor" };
        click.Invoke(window, [new Button { Tag = artifact }, new RoutedEventArgs()]);
        await WaitUntil(() => Text(window) == "saved-baseline", "隔离编辑器没有加载");
        ((TextBox)window.FindName("SubtitleTextBox")).Text = "unsaved-user-correction";
        using var stop = new CancellationTokenSource();
        var owner = new System.Windows.Interop.WindowInteropHelper(window).Handle;
        var answer = ReplyAsync(owner, "未保存的字幕", 7, stop.Token);
        try
        {
            typeof(MainWindow).GetMethod("CloseModal", Flags)!.Invoke(window, null);
            await Task.Delay(400);
            if (((FrameworkElement)window.FindName("ModalOverlay")).Visibility == Visibility.Collapsed)
            {
                click.Invoke(window, [new Button { Tag = artifact }, new RoutedEventArgs()]);
                await WaitUntil(() => Text(window) == "saved-baseline", "关闭后编辑器没有重新加载");
            }
            Require(Text(window) == "unsaved-user-correction", "关闭再打开字幕，未保存的修改被静默丢失");
            Require(await answer, "未保存修改关闭时没有提供保留选项");
            var keepEditor = ReplyAsync(owner, "未保存的字幕", 7, stop.Token);
            typeof(MainWindow).GetMethod("OpenModal", Flags)!.Invoke(window, [(FrameworkElement)window.FindName("DeleteDialog")]);
            Require(await keepEditor && Text(window) == "unsaved-user-correction" &&
                ((FrameworkElement)window.FindName("SubtitleDialog")).Visibility == Visibility.Visible,
                "切换对话框绕过了未保存字幕保护");
            var keepWindow = ReplyAsync(owner, "未保存的字幕", 7, stop.Token);
            window.Close();
            Require(window.IsVisible && Text(window) == "unsaved-user-correction",
                "主窗口退出时未保存字幕被静默丢失");
            Require(await keepWindow, "主窗口退出绕过了未保存字幕确认");
            // 保存成功后应能正常关闭，不能把编辑器变成不可关闭的对话框。
            Require(await (Task<bool>)typeof(MainWindow).GetMethod("SaveSubtitleAsync", Flags)!.Invoke(window, null)!, "保存草稿失败");
            typeof(MainWindow).GetMethod("CloseModal", Flags)!.Invoke(window, null);
            await WaitUntil(() => ((FrameworkElement)window.FindName("ModalOverlay")).Visibility == Visibility.Collapsed,
                "已保存的编辑器不能关闭");
            click.Invoke(window, [new Button { Tag = artifact }, new RoutedEventArgs()]);
            await WaitUntil(() => Text(window) == "unsaved-user-correction" &&
                ((FrameworkElement)window.FindName("SubtitleDialog")).Visibility == Visibility.Visible, "已保存的修改没有重新载入");
            ((TextBox)window.FindName("SubtitleTextBox")).Text = "explicitly-discarded-later-correction";
            var discard = ReplyAsync(owner, "未保存的字幕", 6, stop.Token);
            typeof(MainWindow).GetMethod("CloseModal", Flags)!.Invoke(window, null);
            Require(await discard, "显式丢弃没有弹出确认框");
            await WaitUntil(() => ((FrameworkElement)window.FindName("ModalOverlay")).Visibility == Visibility.Collapsed,
                "显式丢弃后编辑器不能关闭");
            click.Invoke(window, [new Button { Tag = artifact }, new RoutedEventArgs()]);
            await WaitUntil(() => Text(window) == "unsaved-user-correction" &&
                ((FrameworkElement)window.FindName("SubtitleDialog")).Visibility == Visibility.Visible, "丢弃操作覆盖了已保存的字幕");
            typeof(MainWindow).GetMethod("CloseModal", Flags)!.Invoke(window, null);
            await WaitUntil(() => ((FrameworkElement)window.FindName("ModalOverlay")).Visibility == Visibility.Collapsed,
                "已保存的编辑器不能关闭");
            Console.WriteLine("通过：实际 WPF 未保存字幕默认保留、保存后可关闭、显式丢弃不覆盖已保存内容。");
        }
        finally { stop.Cancel(); }
    }

    private static async Task<bool> ReplyAsync(nint owner, string title, nint command, CancellationToken token)
    {
        return await Task.Run(async () =>
        {
            var deadline = DateTime.UtcNow + TimeSpan.FromSeconds(5);
            while (DateTime.UtcNow < deadline && !token.IsCancellationRequested)
            {
                var dialog = FindWindow("#32770", title);
                if (dialog != 0 && GetWindow(dialog, 4) == owner)
                    return PostMessage(dialog, 0x0111, command, 0); // WM_COMMAND，IDYES 或 IDNO；只处理本测试窗口。
                await Task.Delay(20);
            }
            return false;
        });
    }

    private static string Text(MainWindow window) => ((TextBox)window.FindName("SubtitleTextBox")).Text;
    private static void Require(bool condition, string message) { if (!condition) throw new Exception(message); }

    private static async Task WaitUntil(Func<bool> predicate, string message)
    {
        var end = DateTime.UtcNow + TimeSpan.FromSeconds(5);
        while (!predicate() && DateTime.UtcNow < end) await Task.Delay(20);
        Require(predicate(), message);
    }

    private sealed class SubtitleHandler : HttpMessageHandler
    {
        private string _savedText = "saved-baseline";

        protected override async Task<HttpResponseMessage> SendAsync(HttpRequestMessage request, CancellationToken cancellationToken)
        {
            if (request.Method == HttpMethod.Put)
            {
                using var document = System.Text.Json.JsonDocument.Parse(await request.Content!.ReadAsStringAsync(cancellationToken));
                _savedText = document.RootElement.GetProperty("text").GetString()!;
            }
            return new HttpResponseMessage(HttpStatusCode.OK)
            {
                Content = new StringContent(System.Text.Json.JsonSerializer.Serialize(new { text = _savedText, ok = true }),
                    Encoding.UTF8, "application/json")
            };
        }
    }
}
