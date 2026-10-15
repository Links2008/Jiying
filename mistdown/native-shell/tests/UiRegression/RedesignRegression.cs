// 展示素材全部为可控样本；只操作测试宿主的窗口与临时设置。
using System.Reflection;
using System.Text.Json;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Input;
using System.Windows.Media;
using System.Windows.Media.Imaging;
using Jiying.Desktop;

internal static class RedesignRegression
{
    public static async Task Run(MainWindow window, Action<bool, string> check)
    {
        const BindingFlags flags = BindingFlags.Instance | BindingFlags.NonPublic;
        T Field<T>(string name) => (T)typeof(MainWindow).GetField(name, flags)!.GetValue(window)!;
        void Call(string name, params object[] args) => typeof(MainWindow).GetMethod(name, flags)!.Invoke(window, args);
        var directory = Environment.GetEnvironmentVariable("JIYING_SETTINGS_OUTPUT")!;
        System.IO.Directory.CreateDirectory(directory);
        var ui = Field<object>("_ui");
        var theme = ui.GetType().GetProperty("Theme")!;
        check((typeof(MainWindow).GetField("_darkArtwork", flags)!.GetValue(window) is null) !=
              (typeof(MainWindow).GetField("_lightArtwork", flags)!.GetValue(window) is null),
              "启动只准备当前主题的背景，另一主题按需生成");
        Field<Slider>("BackgroundOpacitySlider").Value = 40;
        var contentType = typeof(MainWindow).Assembly.GetType("Jiying.Desktop.ArtifactContent")!;
        var source = "1\n00:00:00,000 --> 00:00:03,000\nWe can fix the subtitles here.\n\n2\n00:00:03,000 --> 00:00:06,000\nKeep the timing and change the words.\n\n3\n00:00:06,000 --> 00:00:09,000\nThe original gives us context.\n\n4\n00:00:09,000 --> 00:00:12,000\nSave it and generate the video again.\n";
        var translated = source.Replace("We can fix the subtitles here.", "可以在这里修正字幕。")
            .Replace("Keep the timing and change the words.", "时间轴保持不变，只需修改文字。")
            .Replace("The original gives us context.", "结合原文检查语境和用词。")
            .Replace("Save it and generate the video again.", "保存后，重新生成带字幕的视频。");
        object Content(string text, string reference) => JsonSerializer.Deserialize(JsonSerializer.Serialize(
            new { text, source_text = reference, source_language = "en", is_source = false }), contentType)!;
        window.Width = 1160;
        window.Height = 720;
        window.Left = window.Top = 0;
        window.Title = "即影原版材质预览（隔离）";
        await Task.Delay(200);
        foreach (var mode in new[] { "light", "dark" })
        {
            theme.SetValue(ui, mode);
            Call("ApplyAppearance");
            check(Field<Image>("BackgroundImage").Source is BitmapSource &&
                  window.FindName("MistLayer") is not null && window.FindName("GrainLayer") is not null,
                  "原来的磨砂材质与纹理已恢复 " + mode);
            Call("ShowView", "settings");
            Field<Expander>("SpeechSettings").IsExpanded = false;
            Field<Expander>("AdvancedSettings").IsExpanded = false;
            await Task.Delay(420);
            Keyboard.ClearFocus();
            window.UpdateLayout();
            Capture("设置-" + mode);
            check(Field<StackPanel>("SettingsContent").ActualWidth > 780,
                  "设置页面恢复原有宽度 " + mode);
            check(!Descendants(Field<Grid>("SettingsView")).OfType<Separator>().Any(), "设置不再逐行显示横线 " + mode);
            Field<Expander>("AdvancedSettings").IsExpanded = true;
            await Task.Delay(360);
            Field<ScrollViewer>("SettingsScroller").ScrollToBottom();
            await Task.Delay(100);
            Keyboard.ClearFocus();
            window.UpdateLayout();
            Capture("高级设置-" + mode);
            check(window.FindName("CookiesBrowserBox") is null && window.FindName("CookiesFileBox") is null, "高级 Cookie 入口已移除 " + mode);
            Field<ScrollViewer>("SettingsScroller").ScrollToTop();
            Call("LoadSubtitleEditor", Content(translated, source));
            Field<TextBlock>("SubtitleDialogTitle").Text = "修改字幕 · 中文";
            typeof(MainWindow).GetField("_subtitleSavedText", flags)!.SetValue(window, translated);
            Call("SetSubtitleBusy", false, "修改完成后保存，可直接重新生成视频。");
            Call("OpenModal", Field<FrameworkElement>("SubtitleDialog"));
            await Task.Delay(300);
            Keyboard.ClearFocus();
            window.UpdateLayout();
            Capture("字幕编辑-" + mode);
            var rows = Field<DataGrid>("SubtitleRows");
            check(rows.Items.OfType<SubtitleCue>().All(c => c.ReferenceText != "未找到对应原文"), "逐条原文与译文真实对齐 " + mode);
            var first = (DataGridRow)rows.ItemContainerGenerator.ContainerFromIndex(0);
            var input = Descendants(first).OfType<TextBox>().Single();
            input.Text = "可以直接点击文字修正字幕。";
            check(Field<TextBox>("SubtitleTextBox").Text.Contains("可以直接点击文字修正字幕。"), "点击行内输入同步保存稿 " + mode);
            Capture("字幕编辑中-" + mode);
            typeof(MainWindow).GetField("_editingArtifact", flags)!.SetValue(window, null);
            Call("CloseModal");
            await Task.Delay(200);
        }
        var longText = string.Concat(Enumerable.Range(1, 1500).Select(i =>
            $"{i}\n{TimeSpan.FromSeconds(i * 2):hh\\:mm\\:ss},000 --> {TimeSpan.FromSeconds(i * 2 + 1):hh\\:mm\\:ss},000\n较长字幕第 {i} 条\n\n"));
        Call("LoadSubtitleEditor", Content(longText, ""));
        Call("OpenModal", Field<FrameworkElement>("SubtitleDialog"));
        await Task.Delay(300);
        window.UpdateLayout();
        check(Field<DataGrid>("SubtitleRows").Items.Count == 1500 && Descendants(Field<DataGrid>("SubtitleRows")).OfType<DataGridRow>().Count() < 80,
              "1500 条字幕只创建当前可见行，避免整表控件占用内存");
        Call("CloseModal");
        Call("ShowView", "settings");

        void Capture(string name)
        {
            var bitmap = NativeWindowCapture.Capture(window);
            var encoder = new PngBitmapEncoder();
            encoder.Frames.Add(BitmapFrame.Create(bitmap));
            using var stream = System.IO.File.Create(System.IO.Path.Combine(directory, name + ".png"));
            encoder.Save(stream);
        }
    }

    private static IEnumerable<DependencyObject> Descendants(DependencyObject parent)
    {
        for (var i = 0; i < VisualTreeHelper.GetChildrenCount(parent); i++)
        {
            var child = VisualTreeHelper.GetChild(parent, i);
            yield return child;
            foreach (var nested in Descendants(child)) yield return nested;
        }
    }
}
