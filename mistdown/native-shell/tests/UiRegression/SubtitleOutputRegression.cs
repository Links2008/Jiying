using System.Net;
using System.Net.Http;
using System.Reflection;
using System.Text;
using System.Text.Json;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Input;
using Jiying.Desktop;

internal static class SubtitleOutputRegression
{
    public static void CheckPreviewDragGeometry(MainWindow window, Action<bool, string> check)
    {
        var surface = (FrameworkElement)window.FindName("SubtitlePreviewSurface");
        var marker = (FrameworkElement)window.FindName("SubtitleDragMarker");
        const BindingFlags member = BindingFlags.Instance | BindingFlags.NonPublic;
        var move = typeof(MainWindow).GetMethod("MoveSubtitleMarker", member)!;
        var release = typeof(MainWindow).GetMethod("SubtitlePreview_MouseUp", member)!;
        var dragging = typeof(MainWindow).GetField("_subtitleDragging", member)!;
        var width = surface.Width;
        var height = surface.Height;
        var visibility = marker.Visibility;
        try
        {
            foreach (var size in new[] { 1d, 0d })
            {
                surface.Width = size;
                surface.Height = size;
                surface.Measure(new Size(size, size));
                surface.Arrange(new Rect(0, 0, size, size));
                var works = true;
                try { move.Invoke(window, [new Point()]); }
                catch (TargetInvocationException) { works = false; }
                check(works && (size == 0
                        ? marker.Visibility != Visibility.Visible
                        : marker.Visibility == Visibility.Visible && Canvas.GetLeft(marker) == 0 && Canvas.GetTop(marker) == 0),
                    $"字幕预览 {size}×{size} 不产生非法拖动标记");
            }
            var ui = window.Settings;
            var offset = (ui.SubtitlePosition, ui.SubtitleHorizontalOffset, ui.SubtitleVerticalOffset);
            dragging.SetValue(window, true);
            var released = new MouseButtonEventArgs(Mouse.PrimaryDevice, 0, MouseButton.Left)
                { RoutedEvent = UIElement.PreviewMouseLeftButtonUpEvent };
            release.Invoke(window, [surface, released]);
            check(offset == (ui.SubtitlePosition, ui.SubtitleHorizontalOffset, ui.SubtitleVerticalOffset)
                && dragging.GetValue(window) is false, "预览在拖动途中失去尺寸时取消拖动且不改已保存位置");
        }
        finally
        {
            dragging.SetValue(window, false);
            surface.Width = width;
            surface.Height = height;
            marker.Visibility = visibility;
        }
    }

    public static async Task Run(MainWindow window, Action<bool, string> check, Action<string> capture)
    {
        var output = window.FindName("SubtitleOutputBox") as ComboBox;
        check(output is not null, "界面提供明确的字幕输出选择框");
        if (output is null) return;
        var ui = window.Settings;
        var mode = window.SubtitleModeBox;
        check(output.Items.OfType<ComboBoxItem>().Select(item => item.Tag?.ToString())
            .SequenceEqual(new[] { "softmux", "burn" }), "软字幕与硬字幕模式均可选择");
        check(output.SelectedValue?.ToString() == "softmux", "新设置默认选中内嵌软字幕");
        mode.SelectedValue = "source";
        await Task.Delay(300);
        window.SyncDependentControls(false);
        check(window.BurnCard.Visibility == Visibility.Visible && output.IsEnabled,
            "生成字幕时显示可操作的输出选项");
        check(window.SubtitleOutputHint.Text.Contains("SRT") &&
            window.SubmitHint.Text.Contains("软字幕"), "软字幕说明与任务摘要明确保留 SRT");
        check(window.BurnSettingsPanel.Visibility == Visibility.Collapsed,
            "软字幕模式不显示硬烧录样式设置");
        foreach (var selected in new[] { "burn", "softmux" })
        {
            output.SelectedValue = selected;
            await Task.Delay(300);
            window.SyncDependentControls(false);
            var expected = selected == "burn";
            check((bool)ui.Burn == expected, "输出选项同步原有持久化字段：" + selected);
            check((window.BurnSettingsPanel.Visibility == Visibility.Visible) == expected,
                "硬字幕设置显隐跟随输出模式：" + selected);
            check(window.SubtitleOutputHint.Text.Contains(expected ? "重新编码" : "不重新编码"),
                "输出说明跟随选择：" + selected);
            var settingsPath = UiSettings.SettingsPath;
            using var saved = JsonDocument.Parse(System.IO.File.ReadAllText(settingsPath));
            check(saved.RootElement.GetProperty("burn").GetBoolean() == expected, "输出选择已保存：" + selected);
        }
        ui.Burn = true;
        window.ApplyUiSettings();
        check(output.SelectedValue?.ToString() == "burn", "历史硬字幕设置仍可正确恢复");
        output.SelectedValue = "softmux";
        mode.SelectedValue = "none";
        await Task.Delay(300);
        check(!output.IsEnabled && window.BurnCard.Visibility == Visibility.Collapsed,
            "仅下载模式隐藏字幕输出并清除硬烧录选择");
        mode.SelectedValue = "source";
        await Task.Delay(300);
        check(output.SelectedValue?.ToString() == "softmux", "重新启用字幕时使用软字幕");

        window.ProbeResult = new ProbeResult { Engine = "ytdlp", Title = "长播客字幕输出示例" };
        window.ProbeFormats.Add(new FormatInfo { FormatId = "source", Label = "1080p 原画质", Height = 1080 });
        window.FormatList.SelectedIndex = 0;
        window.ProbePanel.Visibility = Visibility.Visible;
        window.ProbeTitle.Text = "长播客字幕输出示例";
        window.ProbeMeta.Text = "隔离界面验证 · 无真实下载";
        window.SetProbeBusy(true);
        window.SetProbeBusy(false);
        window.Width = 1160;
        window.Height = 900;
        await Task.Delay(450);
        window.UpdateLayout();
        window.BurnCard.BringIntoView();
        await Task.Delay(150);
        capture("JIYING_SOFTMUX_SCREENSHOT");
        foreach (var (width, height, theme) in new[] { (1160, 800, "light"), (860, 600, "dark") })
        {
            ui.Theme = theme;
            window.ApplyAppearance();
            window.Width = width;
            window.Height = height;
            window.UpdateLayout();
            window.BurnCard.BringIntoView();
            await Task.Delay(350);
            var scroller = window.ParseScroller;
            var position = output.TranslatePoint(new Point(), scroller);
            check(output.IsVisible && position.Y >= 0 && position.Y + output.ActualHeight <= scroller.ActualHeight,
                "输出选项在视口中完整显示：" + theme);
            check(window.SubtitleOutputHint.ActualHeight > 0,
                "软字幕说明正常布局：" + theme);
            capture("JIYING_SOFTMUX_" + theme.ToUpperInvariant() + "_SCREENSHOT");
        }
        await CheckRequests(window, output, check);
    }

    private static async Task CheckRequests(MainWindow window, ComboBox output, Action<bool, string> check)
    {
        var engine = window.Engine;
        var previousApi = engine.RequestTransport;
        var previousPort = engine.Port;
        using var handler = new Handler();
        using var client = new HttpClient(handler);
        engine.RequestTransport = client;
        engine.Port = 12345;
        try
        {
            foreach (var selected in new[] { "softmux", "burn" })
            {
                output.SelectedValue = selected;
                handler.RequestBurn = null;
                window.Submit_Click(window.SubmitButton, new RoutedEventArgs());
                for (var i = 0; i < 100 && (bool)window.Submitting; i++)
                    await Task.Delay(20);
                check(handler.RequestBurn == (selected == "burn"), "实际创建任务请求匹配输出选项：" + selected);
            }
        }
        finally
        {
            engine.RequestTransport = previousApi;
            engine.Port = previousPort;
            output.SelectedValue = "softmux";
        }
    }

    private sealed class Handler : HttpMessageHandler
    {
        public bool? RequestBurn;
        protected override async Task<HttpResponseMessage> SendAsync(HttpRequestMessage request, CancellationToken token)
        {
            var path = request.RequestUri!.AbsolutePath;
            var body = "{}";
            if (path == "/api/v1/tasks" && request.Method == HttpMethod.Post)
            {
                using var json = JsonDocument.Parse(await request.Content!.ReadAsStringAsync(token));
                RequestBurn = json.RootElement.GetProperty("burn").GetBoolean();
                body = """{"id":"subtitle-output-test","status":"pending","title":"隔离输出测试"}""";
            }
            else if (path == "/api/v1/tasks") body = "[]";
            else if (path == "/api/v1/whisper/status") body = """{"downloaded":true,"status":"ready"}""";
            return new HttpResponseMessage(HttpStatusCode.OK) { Content = new StringContent(body, Encoding.UTF8, "application/json") };
        }
    }
}
