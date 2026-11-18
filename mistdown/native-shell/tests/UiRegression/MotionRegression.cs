using System.Diagnostics;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Media;
using System.Windows.Markup;
using Jiying.Desktop;

internal static class MotionRegression
{
    private sealed class CountingContent : FrameworkElement
    {
        public int MeasureCount { get; private set; }
        public int ArrangeCount { get; private set; }

        protected override Size MeasureOverride(Size available)
        {
            MeasureCount++;
            return new Size(100, double.IsFinite(available.Height) ? Math.Min(140, available.Height) : 140);
        }

        protected override Size ArrangeOverride(Size finalSize)
        {
            ArrangeCount++;
            return finalSize;
        }
    }

    internal static async Task Run(MainWindow window, Action<bool, string> check)
    {
        var contentProbe = new CountingContent();
        var viewport = new ExpansionContent { Child = contentProbe };
        var initialHeight = viewport.Freeze(300);
        contentProbe.InvalidateMeasure();
        var reversedHeight = viewport.Freeze(300);
        check(contentProbe.MeasureCount == 1 && initialHeight == reversedHeight,
              "同宽反切复用动画期间的内容测量");
        viewport.Freeze(280);
        check(contentProbe.MeasureCount == 2, "过渡期间宽度变化重新测量");
        viewport.Release();
        T Field<T>(string name) => (T)window.FindName(name);
        window.ShowView("parse");
        await Task.Delay(330);
        var processes = Process.GetProcessesByName("Jiying.Native");
        try
        {
            if (processes.Any(process => process.Threads.Count == 0))
            {
                var running = EngineClient.AppRunningIn(@"Z:\Jiying.NonexistentRegressionDirectory");
                check(!running, "Zero-thread residual client records do not block engine upgrades");
            }
        }
        finally { foreach (var process in processes) process.Dispose(); }

        async Task CheckFolding()
        {
            window.ShowView("settings");
            await Task.Delay(330);
            foreach (var name in new[] { "SpeechSettings", "AdvancedSettings" })
            {
                var group = Field<Expander>(name);
                group.ApplyTemplate();
                var body = (FrameworkElement)group.Template.FindName("ExpandSite", group);
                group.IsExpanded = false;
                check(await UiTiming.Until(() => body.Visibility == Visibility.Collapsed && double.IsNaN(body.Height)),
                      name + " starts from a settled collapsed state");
                group.IsExpanded = true;
                var opening = !SystemParameters.ClientAreaAnimation || await UiTiming.Until(() =>
                    body.Height > 0 && body.Height < (double)body.GetAnimationBaseValue(FrameworkElement.HeightProperty) - 0.5);
                var expanded = await UiTiming.Until(() =>
                    body.Visibility == Visibility.Visible && double.IsNaN(body.Height) && body.ActualHeight > 0);
                var fullHeight = body.ActualHeight;
                check(opening && expanded, name + " expands with a visible height transition");
                group.IsExpanded = false;
                check(!SystemParameters.ClientAreaAnimation || (body.Visibility == Visibility.Visible
                    && body.HasAnimatedProperties
                    && (double)body.GetAnimationBaseValue(FrameworkElement.HeightProperty) == 0),
                    name + " remains visible during closing transition");
                check(await UiTiming.Until(() => body.Visibility == Visibility.Collapsed && double.IsNaN(body.Height)),
                      name + " fully collapses after transition");
                group.IsExpanded = true;
                await Task.Delay(25);
                group.IsExpanded = false;
                await Task.Delay(25);
                group.IsExpanded = true;
                var recovered = await UiTiming.Until(() => body.Visibility == Visibility.Visible
                    && double.IsNaN(body.Height) && Math.Abs(body.ActualHeight - fullHeight) < 2);
                check(recovered, $"{name} recovers from rapid repeated toggles without clipping "
                    + $"(Height={body.ActualHeight:F1}, expected={fullHeight:F1}, animated={body.Height:F1}, width={body.ActualWidth:F1})");
                group.IsExpanded = false;
                check(await UiTiming.Until(() => body.Visibility == Visibility.Collapsed), name + " resets after rapid toggles");
            }
            if (!SystemParameters.ClientAreaAnimation) return;
            var settings = Field<Expander>("SpeechSettings");
            var original = settings.Content;
            var measured = new CountingContent();
            try
            {
                settings.Content = measured;
                var body = (FrameworkElement)settings.Template.FindName("ExpandSite", settings);
                settings.IsExpanded = true;
                var samples = 0;
                var measures = 0;
                var arranges = 0;
                var settled = await UiTiming.Until(() =>
                {
                    if (double.IsNaN(body.Height)) return true;
                    samples++;
                    measures = Math.Max(measures, measured.MeasureCount);
                    arranges = Math.Max(arranges, measured.ArrangeCount);
                    return false;
                });
                check(settled && samples > 0 && measures <= 2 && arranges <= 2,
                    $"折叠过渡固定内容布局约束，Measure={measures}，Arrange={arranges}，采样={samples}");
            }
            finally
            {
                settings.IsExpanded = false;
                var body = (FrameworkElement)settings.Template.FindName("ExpandSite", settings);
                await UiTiming.Until(() => body.Visibility == Visibility.Collapsed);
                settings.Content = original;
            }
        }
        var url = window.UrlBox;
        url.Text = "";
        window.UpdateLayout();
        var initialTop = window.ParseHero.TranslatePoint(new Point(), window.ParseView).Y;
        url.Text = "https://example.com/video";
        window.SetProbeBusy(true);
        window.ProbePanel.Visibility = Visibility.Visible;
        window.SetProbeBusy(false);
        await Task.Delay(450);
        check(window.ParseContent.VerticalAlignment == VerticalAlignment.Top, "Parsed results use the compact layout");
        url.Clear();
        await Task.Delay(450);
        window.UpdateLayout();
        check(window.ParseContent.VerticalAlignment == VerticalAlignment.Center &&
              Math.Abs(window.ParseHero.TranslatePoint(new Point(), window.ParseView).Y - initialTop) < 2,
              "Clearing a parsed URL restores the original centered layout");
        check(window.ParseHeading.Visibility == Visibility.Visible && window.ParseExamples.Visibility == Visibility.Visible &&
              window.ProbePanel.Visibility == Visibility.Collapsed, "Clearing URL restores heading and examples and hides results");
        using (var cancellation = new CancellationTokenSource())
        {
            url.Text = "https://example.com/pending";
            window.ProbeCancellation = cancellation;
            window.SetProbeBusy(true);
            url.Clear();
            check(cancellation.IsCancellationRequested && window.ParseLoading.Visibility == Visibility.Collapsed,
                  "Clearing URL cancels pending parsing and removes the spinner");
            window.ProbeCancellation = null;
        }
        await Task.Delay(450);
        check(window.ParseContent.VerticalAlignment == VerticalAlignment.Center, "Cancelled parsing returns to centered input");
        await ProbeCancellationRegression.Run(window, check);
        var fonts = window.FontNameBox;
        check(window.FontChoicesLoaded == false, "启动阶段不枚举和验证整套系统字体");
        window.FontNameBox_DropDownOpened(fonts, EventArgs.Empty);
        var tags = fonts.Items.OfType<ComboBoxItem>().Select(item => item.Tag?.ToString()).ToHashSet(StringComparer.OrdinalIgnoreCase);
        var installed = Fonts.SystemFontFamilies.Where(family => family.GetTypefaces().Any(face => face.TryGetGlyphTypeface(out _)))
            .Select(family => family.FamilyNames.TryGetValue(XmlLanguage.GetLanguage("en-us"), out var english) ? english : family.Source).ToArray();
        check(installed.All(tags.Contains) && tags.Contains("Noto Sans CJK SC"), "Subtitle font choices include installed physical fonts and the bundled CJK font");
        var count = fonts.Items.Count;
        window.FontNameBox_DropDownOpened(fonts, EventArgs.Empty);
        check(fonts.Items.Count == count, "重复打开字体列表不重复扫描或添加字体");
        var previousFont = fonts.SelectedValue;
        var extraFont = installed.FirstOrDefault(name => name == "Cambria") ?? installed.First(name => !new[] { "Arial", "Microsoft YaHei", "SimHei", "SimSun", "KaiTi" }.Contains(name));
        fonts.SelectedValue = extraFont;
        check(fonts.SelectedValue?.ToString() == extraFont, "Additional installed fonts can be selected for subtitles");
        window.FontSearchBox.Text = extraFont;
        check(fonts.Items.OfType<ComboBoxItem>().All(item => item.Tag?.ToString()?.Contains(extraFont, StringComparison.OrdinalIgnoreCase) == true),
              "Font search filters installed font names without changing selection");
        check(fonts.SelectedValue?.ToString() == extraFont, "Filtering fonts preserves the current subtitle font");
        window.FontSearchBox.Clear();
        check(fonts.Items.Count >= installed.Length, "Clearing font search restores the full font list");
        Console.WriteLine($"INFO System fonts={installed.Length}; selectable fonts={fonts.Items.Count}");
        fonts.SelectedValue = previousFont;
        await CheckFolding();
        window.ShowView("parse");
        await Task.Delay(330);
    }
}
