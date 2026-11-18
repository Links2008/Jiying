using System.IO;
using System.Net.Http;
using System.Text.Json;
using System.Windows;
using System.Windows.Controls;
using Jiying.Desktop;

internal static class LiveParseRegression
{
    internal static async Task Run(MainWindow window, int port, Action<bool, string> check, Action<string> capture)
    {
        var engine = window.Engine;
        var credential = Environment.GetEnvironmentVariable("JIYING_LIVE_ENGINE_TOKEN");
        if (credential is null) engine.Port = port;
        else
        {
            // 真实新版使用正常配对入口，独立进程启动由测试宿主管理。
                        var enginePath = Environment.GetEnvironmentVariable("JIYING_LIVE_ENGINE_PATH");
            if (enginePath is null) throw new InvalidOperationException("缺少隔离引擎路径");
            engine = new EngineClient(enginePath);
            var ensure = engine.EnsureConnectedAsync(false, CancellationToken.None);
            if (!await ensure || engine.Port != port)
            {
                ((IDisposable)engine).Dispose();
                throw new InvalidOperationException("未能验证隔离引擎的实际配对");
            }
            window.AttachEngine(engine);
            check(true, "WPF 从加密登记获取引擎凭据并连接到本次启动的引擎");
        }
        var input = (TextBox)window.FindName("UrlBox");
        input.Text = Environment.GetEnvironmentVariable("JIYING_LIVE_URL")
            ?? "https://www.bilibili.com/video/BV1NGZtBwELa";
        window.UpdateLayout();
        capture("JIYING_PARSE_INITIAL_SCREENSHOT");
        check(((Border)window.FindName("BilibiliLoginBar")).Visibility == Visibility.Collapsed,
            "真实 B 站链接在点击解析前不要求配置账号");
        await window.ProbeAsync(false);
        await Task.Delay(1500);
        window.UpdateLayout();
        check(((StackPanel)window.FindName("ProbePanel")).Visibility == Visibility.Visible,
            "真实接口返回视频结果并展示");
        check(window.ProbeFormats.Count > 0 && ((ComboBox)window.FindName("FormatList")).SelectedItem is FormatInfo,
            "真实画质列表已预选可用格式");
        check(((Button)window.FindName("SubmitButton")).IsVisible,
            "解析结果保留明确的开始下载按钮");
        check(((Border)window.FindName("BilibiliLoginBar")).Visibility == Visibility.Collapsed,
            "公开内容解析成功后不出现登录卡片");
        capture("JIYING_PARSE_LIVE_SCREENSHOT");

        var downloadDirectory = Environment.GetEnvironmentVariable("JIYING_LIVE_DOWNLOAD_DIR");
        if (string.IsNullOrWhiteSpace(downloadDirectory)) return;
        var prefix = Environment.GetEnvironmentVariable("JIYING_LIVE_FORMAT_PREFIX");
        var selected = string.IsNullOrWhiteSpace(prefix)
            ? window.ProbeFormats.FirstOrDefault(format => format.FormatId.StartsWith("bili-120-", StringComparison.Ordinal))
            : window.ProbeFormats.FirstOrDefault(format => format.FormatId.StartsWith(prefix, StringComparison.Ordinal));
        var expectedLabel = Environment.GetEnvironmentVariable("JIYING_LIVE_FORMAT_LABEL");
        check(expectedLabel is null ? selected?.Label.StartsWith("4K", StringComparison.Ordinal) == true
                                    : selected?.Label == expectedLabel, "B站原链接列出所需 4K 档");
        if (selected is null) return;

        ((TextBox)window.FindName("OutputPathBox")).Text = downloadDirectory;
        ((CheckBox)window.FindName("RecognizeCheck")).IsChecked = false;
        ((CheckBox)window.FindName("TranslateCheck")).IsChecked = false;
        ((ComboBox)window.FindName("FormatList")).SelectedItem = selected;
        window.UpdateLayout();
        capture("JIYING_LIVE_4K_SCREENSHOT");
        ((Button)window.FindName("SubmitButton")).RaiseEvent(new RoutedEventArgs(Button.ClickEvent));

        using var http = new HttpClient(new HttpClientHandler { UseProxy = false })
        {
            BaseAddress = new Uri($"http://127.0.0.1:{port}"), Timeout = TimeSpan.FromSeconds(5),
        };
        if (credential is not null)
            http.DefaultRequestHeaders.Authorization = new System.Net.Http.Headers.AuthenticationHeaderValue("Bearer", credential);
        using var deadline = new CancellationTokenSource(TimeSpan.FromSeconds(
            int.TryParse(Environment.GetEnvironmentVariable("JIYING_LIVE_DOWNLOAD_SECONDS"), out var seconds)
                ? Math.Clamp(seconds, 15, 360) : 90));
        var verifiedUrl = false;
        while (!deadline.IsCancellationRequested)
        {
            using var tasks = JsonDocument.Parse(await http.GetStringAsync("/api/v1/tasks", deadline.Token));
            var task = tasks.RootElement.EnumerateArray().SingleOrDefault(item =>
                item.GetProperty("resolution").GetString() == selected.FormatId);
            if (task.ValueKind != JsonValueKind.Undefined)
            {
                if (!verifiedUrl)
                {
                    check(task.GetProperty("url").GetString() == input.Text, "下载任务使用粘贴的 B站原链接");
                    verifiedUrl = true;
                }
                var status = task.GetProperty("status").GetString();
                if (status == "completed")
                {
                    check(Directory.EnumerateFiles(downloadDirectory, "*.mp4").Count() == 1,
                        "从 4K 档提交的任务实际生成了 MP4");
                    return;
                }
                if (status is "failed" or "cancelled" or "interrupted")
                {
                    check(false, "4K 下载失败：" + task.GetProperty("error").GetString());
                    return;
                }
            }
            await Task.Delay(350, deadline.Token);
        }
        check(false, "4K 下载任务未在时限内完成");
    }
}
