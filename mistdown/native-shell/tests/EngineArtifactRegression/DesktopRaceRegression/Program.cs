// 实际 WPF 窗口配合隔离 HTTP 处理器，重现字幕读取乱序；不读取用户设置或连接实际引擎。
using System.IO;
using System.Net;
using System.Net.Http;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Text;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Media;
using Jiying.Desktop;

internal static class Program
{
    private const BindingFlags Flags = BindingFlags.Instance | BindingFlags.NonPublic;

    [DllImport("user32.dll")]
    private static extern bool SetCursorPos(int x, int y);

    [STAThread]
    public static int Main()
    {
        Console.OutputEncoding = new UTF8Encoding(false);
        var directory = Path.Combine(Path.GetTempPath(), "Jiying.DesktopRaceRegression", Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(directory);
        Environment.SetEnvironmentVariable("JIYING_UI_SETTINGS_PATH", Path.Combine(directory, "native-ui.json"));
        RenderOptions.ProcessRenderMode = System.Windows.Interop.RenderMode.SoftwareOnly;
        var app = new Application { ShutdownMode = ShutdownMode.OnExplicitShutdown };
        app.Resources["BoolToVisibility"] = new BooleanToVisibilityConverter();
        app.DispatcherUnhandledException += (_, args) =>
        {
            Console.Error.WriteLine(args.Exception);
            args.Handled = true;
            app.Shutdown(1);
        };
        var window = new MainWindow();
        var loaded = typeof(MainWindow).GetMethod("Window_Loaded", Flags)!;
        window.Loaded -= (RoutedEventHandler)Delegate.CreateDelegate(typeof(RoutedEventHandler), window, loaded);
        // Wine 回归只在测试宿主中增加现有中文字体回退，原生 Windows 使用产品字体。
        if (Environment.GetEnvironmentVariable("WINEPREFIX") is not null)
            window.Resources["AppFont"] = new FontFamily(new Uri("pack://application:,,,/Jiying.Native;component/Assets/Fonts/"),
                "./#Sora, file:///C:/windows/Fonts/#Noto Sans CJK SC");
        var engine = typeof(MainWindow).GetField("_engine", Flags)!.GetValue(window)!;
        var apiField = engine.GetType().GetField("_api", Flags)!;
        ((HttpClient)apiField.GetValue(engine)!).Dispose();
        using var handler = new DelayedSubtitleHandler();
        apiField.SetValue(engine, new HttpClient(handler));
        engine.GetType().GetProperty("Port")!.SetValue(engine, 12345);
        window.Show();
        _ = SetCursorPos(5000, 5000);
        _ = app.Dispatcher.InvokeAsync(async () =>
        {
            try
            {
                if (Environment.GetEnvironmentVariable("JIYING_EVENT_CONNECTION_ONLY") == "1")
                {
                    await EventConnectionRegression.Run(window);
                    window.Close();
                    app.Shutdown(0);
                    return;
                }
                var click = typeof(MainWindow).GetMethod("Artifact_Click", Flags)!;
                var first = new ArtifactItem { Id = 1, Kind = "subtitle", Path = "first.srt", Language = "zh", TaskId = "first" };
                var second = new ArtifactItem { Id = 2, Kind = "subtitle", Path = "second.srt", Language = "en", TaskId = "second" };
                click.Invoke(window, [new Button { Tag = first }, new RoutedEventArgs()]);
                await handler.FirstStarted.Task.WaitAsync(TimeSpan.FromSeconds(5));
                click.Invoke(window, [new Button { Tag = second }, new RoutedEventArgs()]);
                await WaitUntil(() => Text(window) == "second-subtitle", "第二次点击的字幕没有显示");
                handler.ReleaseFirst.TrySetResult();
                await handler.FirstReturned.Task.WaitAsync(TimeSpan.FromSeconds(5));
                await Task.Delay(250);
                Require(Text(window) == "second-subtitle", "较早请求晚到覆盖了新字幕，可能把编辑内容保存进另一字幕");
                var editing = (ArtifactItem)typeof(MainWindow).GetField("_editingArtifact", Flags)!.GetValue(window)!;
                Require(editing.Id == 2, "编辑对象被旧请求替换");
                var saving = (Task<bool>)typeof(MainWindow).GetMethod("SaveSubtitleAsync", Flags)!.Invoke(window, null)!;
                Require(await saving && handler.LastSavedPath == "/api/v1/artifacts/2/content", "保存请求使用了另一字幕的编号");

                handler.ResetFirst();
                click.Invoke(window, [new Button { Tag = first }, new RoutedEventArgs()]);
                await handler.FirstStarted.Task.WaitAsync(TimeSpan.FromSeconds(5));
                typeof(MainWindow).GetMethod("OpenModal", Flags)!.Invoke(window, [(FrameworkElement)window.FindName("DeleteDialog")]);
                handler.ReleaseFirst.TrySetResult();
                await handler.FirstReturned.Task.WaitAsync(TimeSpan.FromSeconds(5));
                await Task.Delay(250);
                Require(((FrameworkElement)window.FindName("DeleteDialog")).Visibility == Visibility.Visible &&
                    ((FrameworkElement)window.FindName("SubtitleDialog")).Visibility == Visibility.Collapsed,
                    "较早字幕响应夺走新打开的对话框");

                handler.ResetFirst();
                click.Invoke(window, [new Button { Tag = first }, new RoutedEventArgs()]);
                await handler.FirstStarted.Task.WaitAsync(TimeSpan.FromSeconds(5));
                typeof(MainWindow).GetMethod("CloseModal", Flags)!.Invoke(window, null);
                handler.ReleaseFirst.TrySetResult();
                await handler.FirstReturned.Task.WaitAsync(TimeSpan.FromSeconds(5));
                await WaitUntil(() => ((FrameworkElement)window.FindName("ModalOverlay")).Visibility == Visibility.Collapsed,
                    "关闭对话框后，较早字幕响应又打开编辑器");
                Console.WriteLine("通过：实际 WPF 字幕乱序响应、保存对象、切换对话框及关闭后晚到响应保护。");

                var llmKey = (PasswordBox)window.FindName("LlmKeySettingsBox");
                var asrKey = (PasswordBox)window.FindName("AsrKeySettingsBox");
                llmKey.Password = "fake-submitted-llm-key";
                asrKey.Password = "fake-submitted-asr-key";
                var saveConfig = (Task<bool>)typeof(MainWindow).GetMethod("SaveEngineConfigAsync", Flags)!.Invoke(window, [false])!;
                await handler.ConfigStarted.Task.WaitAsync(TimeSpan.FromSeconds(5));
                llmKey.Password = "fake-new-unsaved-llm-key";
                asrKey.Password = "fake-new-unsaved-asr-key";
                handler.ReleaseConfig.TrySetResult();
                Require(await saveConfig, "隔离配置保存没有完成");
                Require(llmKey.Password == "fake-new-unsaved-llm-key" && asrKey.Password == "fake-new-unsaved-asr-key",
                    "旧保存请求清空了中途新输入且尚未保存的凭据");
                Require(((TextBlock)window.FindName("SettingsSaveStatus")).Text.Contains("尚未保存", StringComparison.Ordinal),
                    "新修改尚未保存时仍被提示已生效");
                var saveAgain = (Task<bool>)typeof(MainWindow).GetMethod("SaveEngineConfigAsync", Flags)!.Invoke(window, [false])!;
                Require(await saveAgain && llmKey.Password.Length == 0 && asrKey.Password.Length == 0,
                    "本次已提交且未再修改的假凭据应从输入框清除");
                Require(((TextBlock)window.FindName("SettingsSaveStatus")).Text == "已保存并生效",
                    "没有新修改时应显示本次设置已生效");
                Console.WriteLine("通过：实际 WPF 保存期间新输入的假凭据保留，并提示当前修改尚未保存。");
                await TaskRefreshRegression.Run(window);
                await DirtySubtitleRegression.Run(window);
                await OperationOwnershipRegression.Run(window);
                await DeleteLifecycleRegression.Run(window);
                await EventConnectionRegression.Run(window);
                window.Close();
                app.Shutdown(0);
            }
            catch (Exception error)
            {
                Console.Error.WriteLine(error);
                // 测试失败清理只抛弃隔离测试对象，避免确认框阻塞失败退出。
                typeof(MainWindow).GetField("_editingArtifact", Flags)!.SetValue(window, null);
                app.Shutdown(1);
            }
        });
        try { return app.Run(); }
        finally
        {
            handler.ReleaseFirst.TrySetResult();
            handler.ReleaseConfig.TrySetResult();
            Directory.Delete(directory, true);
        }
    }

    private static string Text(MainWindow window) => ((TextBox)window.FindName("SubtitleTextBox")).Text;
    private static void Require(bool condition, string message) { if (!condition) throw new Exception(message); }

    private static async Task WaitUntil(Func<bool> predicate, string message)
    {
        var end = DateTime.UtcNow + TimeSpan.FromSeconds(5);
        while (!predicate() && DateTime.UtcNow < end) await Task.Delay(20);
        Require(predicate(), message);
    }

    private sealed class DelayedSubtitleHandler : HttpMessageHandler
    {
        public TaskCompletionSource FirstStarted { get; private set; } = new(TaskCreationOptions.RunContinuationsAsynchronously);
        public TaskCompletionSource ReleaseFirst { get; private set; } = new(TaskCreationOptions.RunContinuationsAsynchronously);
        public TaskCompletionSource FirstReturned { get; private set; } = new(TaskCreationOptions.RunContinuationsAsynchronously);
        public string LastSavedPath { get; private set; } = "";
        public TaskCompletionSource ConfigStarted { get; } = new(TaskCreationOptions.RunContinuationsAsynchronously);
        public TaskCompletionSource ReleaseConfig { get; } = new(TaskCreationOptions.RunContinuationsAsynchronously);

        public void ResetFirst()
        {
            FirstStarted = new(TaskCreationOptions.RunContinuationsAsynchronously);
            ReleaseFirst = new(TaskCreationOptions.RunContinuationsAsynchronously);
            FirstReturned = new(TaskCreationOptions.RunContinuationsAsynchronously);
        }

        protected override async Task<HttpResponseMessage> SendAsync(HttpRequestMessage request, CancellationToken cancellationToken)
        {
            if (request.RequestUri!.AbsolutePath == "/api/v1/config")
            {
                ConfigStarted.TrySetResult();
                await ReleaseConfig.Task;
                return Response("saved-config");
            }
            if (request.Method == HttpMethod.Put)
            {
                LastSavedPath = request.RequestUri!.AbsolutePath;
                return Response("saved");
            }
            if (request.RequestUri!.AbsolutePath.Contains("/1/", StringComparison.Ordinal))
            {
                FirstStarted.TrySetResult();
                // 故意保留晚到响应，验证仅发出取消仍不足以保护当前编辑内容。
                await ReleaseFirst.Task;
                FirstReturned.TrySetResult();
                return Response("first-subtitle");
            }
            return Response("second-subtitle");
        }

        private static HttpResponseMessage Response(string text) => new(HttpStatusCode.OK)
        {
            Content = new StringContent("{\"text\":\"" + text + "\"}", Encoding.UTF8, "application/json")
        };
    }
}
