using System.Diagnostics;
using System.Reflection;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Media;
using System.Windows.Markup;
using Jiying.Desktop;

internal static class MotionRegression
{
    internal static async Task Run(MainWindow window, Action<bool, string> check)
    {
        T Field<T>(string name) => (T)typeof(MainWindow).GetField(name, BindingFlags.Instance | BindingFlags.NonPublic | BindingFlags.Public)!.GetValue(window)!;
        void Call(string name, params object[] args) => typeof(MainWindow).GetMethod(name, BindingFlags.Instance | BindingFlags.NonPublic)!.Invoke(window, args);
        Call("ShowView", "parse");
        await Task.Delay(330);
        var processes = Process.GetProcessesByName("Jiying.Native");
        try
        {
            if (processes.Any(process => process.Threads.Count == 0))
            {
                var engineType = typeof(MainWindow).Assembly.GetType("Jiying.Desktop.EngineClient")!;
                var running = (bool)engineType.GetMethod("AppRunningIn", BindingFlags.Static | BindingFlags.NonPublic)!
                    .Invoke(null, [@"Z:\Jiying.NonexistentRegressionDirectory"])!;
                check(!running, "Zero-thread residual client records do not block engine upgrades");
            }
        }
        finally { foreach (var process in processes) process.Dispose(); }

        async Task CheckFolding()
        {
            Call("ShowView", "settings");
            await Task.Delay(330);
            foreach (var name in new[] { "SpeechSettings", "AdvancedSettings" })
            {
                var group = Field<Expander>(name);
                group.IsExpanded = false;
                await Task.Delay(260);
                group.ApplyTemplate();
                var body = (FrameworkElement)group.Template.FindName("ExpandSite", group);
                group.IsExpanded = true;
                await Task.Delay(45);
                window.UpdateLayout();
                var openingHeight = body.ActualHeight;
                await Task.Delay(300);
                window.UpdateLayout();
                var fullHeight = body.ActualHeight;
                check(fullHeight > 0 && (!SystemParameters.ClientAreaAnimation || (openingHeight > 0 && openingHeight < fullHeight)), name + " expands with a visible height transition");
                group.IsExpanded = false;
                await Task.Delay(35);
                check(!SystemParameters.ClientAreaAnimation || (body.Visibility == Visibility.Visible && body.ActualHeight > 0), name + " remains visible during closing transition");
                await Task.Delay(270);
                check(body.Visibility == Visibility.Collapsed, name + " fully collapses after transition");
                group.IsExpanded = true;
                await Task.Delay(25);
                group.IsExpanded = false;
                await Task.Delay(25);
                group.IsExpanded = true;
                await Task.Delay(300);
                window.UpdateLayout();
                check(body.Visibility == Visibility.Visible && Math.Abs(body.ActualHeight - fullHeight) < 2, name + " recovers from rapid repeated toggles without clipping");
                group.IsExpanded = false;
                await Task.Delay(270);
            }
        }
        var url = Field<TextBox>("UrlBox");
        url.Text = "";
        window.UpdateLayout();
        var initialTop = Field<StackPanel>("ParseHero").TranslatePoint(new Point(), Field<Grid>("ParseView")).Y;
        url.Text = "https://example.com/video";
        Call("SetProbeBusy", true);
        Field<FrameworkElement>("ProbePanel").Visibility = Visibility.Visible;
        Call("SetProbeBusy", false);
        await Task.Delay(450);
        check(Field<StackPanel>("ParseContent").VerticalAlignment == VerticalAlignment.Top, "Parsed results use the compact layout");
        url.Clear();
        await Task.Delay(450);
        window.UpdateLayout();
        check(Field<StackPanel>("ParseContent").VerticalAlignment == VerticalAlignment.Center &&
              Math.Abs(Field<StackPanel>("ParseHero").TranslatePoint(new Point(), Field<Grid>("ParseView")).Y - initialTop) < 2,
              "Clearing a parsed URL restores the original centered layout");
        check(Field<TextBlock>("ParseHeading").Visibility == Visibility.Visible && Field<FrameworkElement>("ParseExamples").Visibility == Visibility.Visible &&
              Field<FrameworkElement>("ProbePanel").Visibility == Visibility.Collapsed, "Clearing URL restores heading and examples and hides results");
        using (var cancellation = new CancellationTokenSource())
        {
            url.Text = "https://example.com/pending";
            typeof(MainWindow).GetField("_probeStop", BindingFlags.Instance | BindingFlags.NonPublic)!.SetValue(window, cancellation);
            Call("SetProbeBusy", true);
            url.Clear();
            check(cancellation.IsCancellationRequested && Field<FrameworkElement>("ParseLoading").Visibility == Visibility.Collapsed,
                  "Clearing URL cancels pending parsing and removes the spinner");
            typeof(MainWindow).GetField("_probeStop", BindingFlags.Instance | BindingFlags.NonPublic)!.SetValue(window, null);
        }
        await Task.Delay(450);
        check(Field<StackPanel>("ParseContent").VerticalAlignment == VerticalAlignment.Center, "Cancelled parsing returns to centered input");
        await ProbeCancellationRegression.Run(window, check);
        var fonts = Field<ComboBox>("FontNameBox");
        check(Field<bool>("_fontChoicesLoaded") == false, "启动阶段不枚举和验证整套系统字体");
        Call("FontNameBox_DropDownOpened", fonts, EventArgs.Empty);
        var tags = fonts.Items.OfType<ComboBoxItem>().Select(item => item.Tag?.ToString()).ToHashSet(StringComparer.OrdinalIgnoreCase);
        var installed = Fonts.SystemFontFamilies.Where(family => family.GetTypefaces().Any(face => face.TryGetGlyphTypeface(out _)))
            .Select(family => family.FamilyNames.TryGetValue(XmlLanguage.GetLanguage("en-us"), out var english) ? english : family.Source).ToArray();
        check(installed.All(tags.Contains) && tags.Contains("Noto Sans CJK SC"), "Subtitle font choices include installed physical fonts and the bundled CJK font");
        var count = fonts.Items.Count;
        Call("FontNameBox_DropDownOpened", fonts, EventArgs.Empty);
        check(fonts.Items.Count == count, "重复打开字体列表不重复扫描或添加字体");
        var previousFont = fonts.SelectedValue;
        var extraFont = installed.FirstOrDefault(name => name == "Cambria") ?? installed.First(name => !new[] { "Arial", "Microsoft YaHei", "SimHei", "SimSun", "KaiTi" }.Contains(name));
        fonts.SelectedValue = extraFont;
        check(fonts.SelectedValue?.ToString() == extraFont, "Additional installed fonts can be selected for subtitles");
        Field<TextBox>("FontSearchBox").Text = extraFont;
        check(fonts.Items.OfType<ComboBoxItem>().All(item => item.Tag?.ToString()?.Contains(extraFont, StringComparison.OrdinalIgnoreCase) == true),
              "Font search filters installed font names without changing selection");
        check(fonts.SelectedValue?.ToString() == extraFont, "Filtering fonts preserves the current subtitle font");
        Field<TextBox>("FontSearchBox").Clear();
        check(fonts.Items.Count >= installed.Length, "Clearing font search restores the full font list");
        Console.WriteLine($"INFO System fonts={installed.Length}; selectable fonts={fonts.Items.Count}");
        fonts.SelectedValue = previousFont;
        await CheckFolding();
        Call("ShowView", "parse");
        await Task.Delay(330);
    }
}
