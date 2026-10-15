using System.IO;
using System.Net;
using System.Net.Sockets;
using System.Reflection;
using System.Text;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Controls.Primitives;
using System.Windows.Media;
using System.Windows.Media.Imaging;
using Jiying.Desktop;

internal static class Program
{
    [STAThread]
    static int Main()
    {
        var subtitleWorkflowOnly = Environment.GetEnvironmentVariable("JIYING_SUBTITLE_WORKFLOW_ONLY") == "1";
        if (subtitleWorkflowOnly) Console.OutputEncoding = new UTF8Encoding(false);
        var failures = new List<string>();
        var glyphFonts = new HashSet<string>();
        var enhancedTask = System.Text.Json.JsonSerializer.Deserialize<TaskItem>(
            """{"status":"running","subtitle_mode":"none","enhance":1,"enhance_scale":2,"progress":{"stage":"enhance","fraction":0.5},"stages":[{"stage":"download","status":"done"},{"stage":"enhance","status":"running"}]}""");
        if (enhancedTask?.Enhance != 1 || enhancedTask.StageText != "旧版处理")
        {
            Console.Error.WriteLine("FAIL 旧版增强任务仍可读取，但不提供超分阶段");
            return 1;
        }
        var app = new Application();
        app.DispatcherUnhandledException += (_, e) => { failures.Add(e.Exception.ToString()); e.Handled = true; };
        app.Resources["BoolToVisibility"] = new BooleanToVisibilityConverter();
        var settingsPath = Path.Combine(Path.GetTempPath(), "Jiying.UiRegression", Guid.NewGuid().ToString("N"), "native-ui.json");
        var previousSettingsPath = Environment.GetEnvironmentVariable("JIYING_UI_SETTINGS_PATH");
        Environment.SetEnvironmentVariable("JIYING_UI_SETTINGS_PATH", settingsPath);
        var originalSettings = File.Exists(settingsPath) ? File.ReadAllBytes(settingsPath) : null;
        var settingsType = typeof(MainWindow).Assembly.GetType("Jiying.Desktop.UiSettings")!;
        var effectiveSettingsPath = (string)settingsType.GetProperty("SettingsPath", BindingFlags.Static | BindingFlags.NonPublic)!.GetValue(null)!;
        if (effectiveSettingsPath != settingsPath)
        {
            Console.Error.WriteLine("FAIL UI regression requires isolated settings before opening a window");
            Environment.SetEnvironmentVariable("JIYING_UI_SETTINGS_PATH", previousSettingsPath);
            return 1;
        }
        var window = new MainWindow();
        var loaded = typeof(MainWindow).GetMethod("Window_Loaded", BindingFlags.Instance | BindingFlags.NonPublic)!;
        window.Loaded -= (RoutedEventHandler)Delegate.CreateDelegate(typeof(RoutedEventHandler), window, loaded);
        // 专项 Wine 宿主只替换测试中文回退；完整 Windows 回归仍检查产品字体。
        if (subtitleWorkflowOnly && Environment.GetEnvironmentVariable("WINEPREFIX") is not null)
            window.Resources["AppFont"] = new FontFamily(new Uri("pack://application:,,,/Jiying.Native;component/Assets/Fonts/"),
                "./#Sora, file:///C:/windows/Fonts/#Noto Sans CJK SC");
        window.Show();
        _ = app.Dispatcher.InvokeAsync(async () =>
        {
            try
            {
                await Task.Delay(600);
                if (Environment.GetEnvironmentVariable("JIYING_NAV_OUTLINE_ONLY") == "1")
                { await NavOutlineRegression.Run(window, Check); return; }
                if (Environment.GetEnvironmentVariable("JIYING_DISCOVERY_ONLY") == "1")
                { await EngineDiscoveryRegression.Run(window, Check); return; }
                if (Environment.GetEnvironmentVariable("JIYING_RESULT_MOTION_ONLY") == "1")
                { await ResultMotionRegression.Run(window, Check); return; }
                if (Environment.GetEnvironmentVariable("JIYING_NAVIGATION_ONLY") == "1")
                { await NavigationRegression.Run(window, Check); return; }
                if (Environment.GetEnvironmentVariable("JIYING_REDESIGN_ONLY") == "1")
                { await RedesignRegression.Run(window, Check); return; }
                if (subtitleWorkflowOnly) { await CheckSubtitleEntry(); return; }
                if (Environment.GetEnvironmentVariable("JIYING_WEBSITE_CHECKS") == "1")
                { await CheckWebsiteSessions(); return; }
                await QrLoadingRegression.Run(window, Check);
                if (Environment.GetEnvironmentVariable("JIYING_QR_ONLY") == "1") return;
                await XiaohongshuLoginRegression.Run(window, Check);
                if (Environment.GetEnvironmentVariable("JIYING_XHS_ONLY") == "1") return;
                await MotionRegression.Run(window, Check);
                if (Environment.GetEnvironmentVariable("JIYING_MOTION_ONLY") == "1") return;
                await NavOutlineRegression.Run(window, Check);
                await CheckSubtitleEntry();
                if (Environment.GetEnvironmentVariable("JIYING_SUBTITLE_ONLY") == "1") return;
                CheckPlatformLogin();
                await CheckSettingsUx();
                await CompletionNotificationRegression.Run(window, Check);
                if (Environment.GetEnvironmentVariable("JIYING_LIGHTWEIGHT_ONLY") == "1")
                {
                    var normalize = typeof(MainWindow).GetMethod("NormalizeUrl", BindingFlags.Static | BindingFlags.NonPublic)!;
                    Check((string)normalize.Invoke(null, ["复制打开抖音 https://v.douyin.com/abc/ 看视频！"])! == "https://v.douyin.com/abc/", "Copied share text extracts its video link");
                    Check((string)normalize.Invoke(null, ["https://www.douyin.com/?recommend=1&modal_id=6961737553342991651"])! == "https://www.douyin.com/video/6961737553342991651", "Douyin modal links retain the selected video");
                    Check(window.FindName("AdvancedSettings") is Expander { IsExpanded: false }, "Low-frequency settings start collapsed");
                    Check(window.FindName("SpeechSettings") is Expander { IsExpanded: false }, "Speech and translation start collapsed in one page");
                    Check(Field<StackPanel>("AsrCloudSettings").Visibility == Visibility.Collapsed && Field<StackPanel>("LlmCloudSettings").Visibility == Visibility.Collapsed, "Inactive cloud parameters are hidden");
                    SetStatus("GpuStatus", "ApplyGpuStatus", """{"supported":false,"status":"idle","size_bytes":553162896}""");
                    Check(!Field<Button>("DownloadGpuButton").IsEnabled && Field<TextBlock>("GpuStatusText").Text.Contains("CPU"), "CPU-only machines do not download GPU components");
                    SetStatus("GpuStatus", "ApplyGpuStatus", """{"supported":true,"status":"downloading","fraction":0.4,"size_bytes":553162896}""");
                    Check(!Field<Button>("DownloadGpuButton").IsEnabled && Field<ProgressBar>("GpuProgress").Visibility == Visibility.Visible, "GPU download progress prevents duplicate requests");
                    SetStatus("GpuStatus", "ApplyGpuStatus", """{"supported":true,"status":"failed","error":"test","size_bytes":553162896}""");
                    Check(Field<Button>("DownloadGpuButton").IsEnabled && Field<TextBlock>("GpuStatusText").Text.Contains("CPU"), "GPU failures keep CPU available and offer retry");
                    SetStatus("GpuStatus", "ApplyGpuStatus", """{"supported":true,"status":"idle","size_bytes":553162896}""");
                    typeof(MainWindow).GetField("_loadingControls", BindingFlags.Instance | BindingFlags.NonPublic)!.SetValue(window, true);
                    Field<TextBox>("AsrBaseSettingsBox").Text = "https://saved.example/asr";
                    Field<TextBox>("LlmBaseSettingsBox").Text = "https://saved.example/llm";
                    Field<ComboBox>("AsrProviderSettingsBox").SelectedValue = "cloud";
                    Field<ComboBox>("TranslationSettingsBox").SelectedValue = "llm";
                    Call("SyncDependentControls", false);
                    Check(Field<StackPanel>("AsrCloudSettings").Visibility == Visibility.Visible && Field<StackPanel>("LlmCloudSettings").Visibility == Visibility.Visible && Field<Grid>("GpuSettingsRow").Visibility == Visibility.Collapsed, "Cloud controls appear only for selected services");
                    Field<ComboBox>("AsrProviderSettingsBox").SelectedValue = "faster-whisper";
                    Field<ComboBox>("TranslationSettingsBox").SelectedValue = "null";
                    Call("SyncDependentControls", false);
                    Check(Field<TextBox>("AsrBaseSettingsBox").Text.Contains("saved.example") && Field<TextBox>("LlmBaseSettingsBox").Text.Contains("saved.example"), "Collapsing inactive controls preserves saved configuration");
                    typeof(MainWindow).GetField("_loadingControls", BindingFlags.Instance | BindingFlags.NonPublic)!.SetValue(window, false);
                    Call("ShowView", "settings");
                    await Task.Delay(550);
                    var output = Environment.GetEnvironmentVariable("JIYING_SETTINGS_OUTPUT");
                    if (!string.IsNullOrWhiteSpace(output))
                    {
                        var uiSettings = typeof(MainWindow).GetField("_ui", BindingFlags.Instance | BindingFlags.NonPublic)!.GetValue(window)!;
                        var uiTheme = uiSettings.GetType().GetProperty("Theme")!;
                        foreach (var dimensions in new[] { (1160, 720), (860, 560) })
                        foreach (var mode in new[] { "dark", "light" })
                        {
                            window.Width = dimensions.Item1;
                            window.Height = dimensions.Item2;
                            uiTheme.SetValue(uiSettings, mode);
                            Call("ApplyAppearance");
                            Field<ScrollViewer>("SettingsScroller").ScrollToTop();
                            window.UpdateLayout();
                            await Task.Delay(160);
                            Environment.SetEnvironmentVariable("JIYING_SETTINGS_CAPTURE", Path.Combine(output, $"即影设置-{mode}-{dimensions.Item1}x{dimensions.Item2}.png"));
                            Capture("JIYING_SETTINGS_CAPTURE");
                            Field<Expander>("SpeechSettings").IsExpanded = true;
                            Field<Expander>("GpuAdvancedSettings").IsExpanded = true;
                            Field<Expander>("AdvancedSettings").IsExpanded = true;
                            window.UpdateLayout();
                            await Task.Delay(300);
                            Environment.SetEnvironmentVariable("JIYING_SETTINGS_CAPTURE", Path.Combine(output, $"即影设置展开-{mode}-{dimensions.Item1}x{dimensions.Item2}.png"));
                            Capture("JIYING_SETTINGS_CAPTURE");
                            foreach (var name in new[] { "WhisperSettingsBox", "DownloadWhisperButton", "DownloadGpuButton", "ThemeDarkButton", "ThemeAutoButton" })
                            {
                                var control = Field<FrameworkElement>(name);
                                var controlPosition = control.TranslatePoint(new Point(), Field<ScrollViewer>("SettingsScroller"));
                                Check(control.ActualWidth > 0 && controlPosition.X >= 0 && controlPosition.X + control.ActualWidth <= Field<ScrollViewer>("SettingsScroller").ActualWidth + 1, $"{name} fits {mode} {dimensions.Item1}");
                            }
                            Field<Expander>("SpeechSettings").IsExpanded = false;
                            Field<Expander>("GpuAdvancedSettings").IsExpanded = false;
                            Field<ScrollViewer>("SettingsScroller").ScrollToBottom();
                            window.UpdateLayout();
                            await Task.Delay(300);
                            var previewScroller = Field<ScrollViewer>("SettingsScroller");
                            Check(previewScroller.VerticalOffset > 0 && Math.Abs(previewScroller.VerticalOffset - previewScroller.ScrollableHeight) < 1,
                                  $"Expanded advanced settings remain reachable in {mode} {dimensions.Item1}");
                            Environment.SetEnvironmentVariable("JIYING_SETTINGS_CAPTURE", Path.Combine(output, $"即影高级设置-{mode}-{dimensions.Item1}x{dimensions.Item2}.png"));
                            Capture("JIYING_SETTINGS_CAPTURE");
                            Field<Expander>("AdvancedSettings").IsExpanded = false;
                        }
                    }
                    return;
                }
                await CheckLoginConfigRecovery();
                if (Environment.GetEnvironmentVariable("JIYING_LOGIN_CONFIG_ONLY") == "1") return;
                await CheckWhisperPreflight();
                Check(window.FindName("EnhanceCard") is null, "Anime 超分入口已移除");
                Check(window.FindName("RefreshQualityButton") is Button, "画质可在解析页强制刷新");
                Check(window.FindName("FormatList") is ComboBox { Height: <= 44 }, "画质使用紧凑原生下拉，无需滚动页面");
                var start = Field<Button>("SubmitButton");
                start.ApplyTemplate();
                Check(start.Template.FindName("Sheen", start) is null, "开始按钮没有覆盖文字的扫光");
                Check(window.FindName("WhisperPrompt") is FrameworkElement { Visibility: Visibility.Collapsed } &&
                      window.FindName("PrepareWhisperButton") is Button, "缺模型时可在提交处提醒并就地下载");
                var exactSize = new FormatInfo { FileSize = 2147483648 };
                var estimatedSize = new FormatInfo { FileSize = 2147483648, SizeIsEstimate = true };
                Check(exactSize.SizeText == "2 GiB" && estimatedSize.SizeText == "约 2 GiB" &&
                      new FormatInfo().SizeText == "大小未知", "体积区分精确、估算与未知并使用正确单位");
                Check(window.FindName("BilibiliLoginBar") is FrameworkElement &&
                      window.FindName("ParseAuthSourceBox") is ComboBox &&
                      window.FindName("ReadLoginButton") is Button &&
                      window.FindName("BrowseCookiesButton") is Button &&
                      window.FindName("ParseCookiesFileBox") is TextBox &&
                      window.FindName("ProbeQualityHint") is TextBlock,
                      "解析页提供登录来源、Cookie 文件和画质提示");
                Check(window.FindName("ConnectBilibiliButton") is Button connectBilibili &&
                      connectBilibili.Style == window.Resources["GhostButton"] &&
                      window.FindName("BilibiliLoginAdvanced") is Expander { IsExpanded: false },
                      "B 站默认提供连接按钮，其他连接方式初始折叠");
                Check(window.FindName("BilibiliQrPanel") is FrameworkElement { Visibility: Visibility.Collapsed } &&
                      window.FindName("BilibiliQrImage") is Image && window.FindName("BilibiliQrStatus") is TextBlock,
                      "扫码面板、二维码与状态控件存在且初始隐藏");
                var defaultPath = (string)settingsType.GetProperty("DefaultOutputDirectory", BindingFlags.Static | BindingFlags.NonPublic)!.GetValue(null)!;
                Check(defaultPath == (Directory.Exists(@"D:\") ? @"D:\即影\Downloads" : Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.MyVideos), "即影")),
                      "New installations prefer D drive and fall back when D is absent");
                Directory.CreateDirectory(Path.GetDirectoryName(settingsPath)!);
                File.WriteAllText(settingsPath, """{"outputDirectory":"D:\\即影\\Downloads"}""");
                var migrated = settingsType.GetMethod("Load")!.Invoke(null, null)!;
                Check((string)settingsType.GetProperty("OutputDirectory")!.GetValue(migrated)! ==
                      (Directory.Exists(@"D:\") ? @"D:\即影\Downloads" : defaultPath),
                      "D drive default is kept even before its download folder is created");
                var customPath = Path.GetTempPath();
                File.WriteAllText(settingsPath, System.Text.Json.JsonSerializer.Serialize(new { outputDirectory = customPath }));
                var custom = settingsType.GetMethod("Load")!.Invoke(null, null)!;
                Check((string)settingsType.GetProperty("OutputDirectory")!.GetValue(custom)! == customPath,
                      "Existing custom output directories are preserved");
                SetStatus("WhisperStatus", "ApplyWhisperStatus", """{"model":"small","status":"downloaded","downloaded":true}""");
                Check(!Field<TextBlock>("WhisperStatusText").Text.Contains("已载入") &&
                      Field<TextBlock>("WhisperStatusText").Text.Contains("首次识别"), "Cached Whisper files are not reported as loaded");
                SetStatus("WhisperStatus", "ApplyWhisperStatus", """{"model":"small","status":"loading","downloaded":true}""");
                Check(Field<ProgressBar>("WhisperProgress").IsIndeterminate && !Field<Button>("DownloadWhisperButton").IsEnabled,
                      "Whisper loading has a distinct busy state");
                SetStatus("WhisperStatus", "ApplyWhisperStatus", """{"model":"small","status":"load_failed","downloaded":true,"message":"模型已下载，但载入失败：测试错误"}""");
                Check(Field<TextBlock>("WhisperStatusText").Text.Contains("载入失败") && Field<Button>("DownloadWhisperButton").IsEnabled,
                      "Whisper load failure preserves the download and offers retry");
                SetStatus("WhisperStatus", "ApplyWhisperStatus", """{"model":"small","status":"ready","downloaded":true,"message":"small 已载入"}""");
                Check(Field<TextBlock>("WhisperStatusText").Text == "small 已载入", "Whisper ready status respects the engine message");
                Check(window.FontFamily.Source.Contains("Sora"), "Original UI font stack");
                InspectVisual(window);
                Check(glyphFonts.Any(f => f.Contains("sora", StringComparison.OrdinalIgnoreCase)), "Visible Latin text uses bundled Sora glyphs");
                Check(glyphFonts.Any(f => f.Contains("msyh", StringComparison.OrdinalIgnoreCase)), "Visible Chinese text uses original Microsoft YaHei fallback");
                Console.WriteLine("GLYPH FONTS " + string.Join(", ", glyphFonts));
                var family = new FontFamily(new Uri("pack://application:,,,/Jiying.Native;component/Assets/Fonts/"), "./#Sora");
                Console.WriteLine("FACES " + string.Join(",", family.GetTypefaces().Select(t => $"{t.Weight}:{string.Join('/', t.FaceNames.Values)}")));
                foreach (var weight in new[] { FontWeights.Normal, FontWeights.Medium, FontWeights.SemiBold, FontWeights.Bold })
                {
                    var face = new Typeface(family, FontStyles.Normal, weight, FontStretches.Normal);
                    Check(face.TryGetGlyphTypeface(out var glyph) && glyph.Weight == weight, $"Sora maps requested weight {weight} without substituting bold");
                    Console.WriteLine($"FONT {weight} => {glyph?.Weight} {glyph?.FontUri}");
                }
                Call("ShowView", "settings");
                await Task.Delay(550);
                var slider = Field<Slider>("ConcurrencySlider");
                slider.ApplyTemplate();
                var track = (Track)slider.Template.FindName("PART_Track", slider);
                Check(track.ActualHeight >= track.Thumb.ActualHeight, "Slider thumb fits its track");
                var move = Field<TranslateTransform>("NavThumbMove");
                var before = move.Y;
                Call("ShowView", "done");
                Check(Math.Abs(move.Y - before) < 2, "Navigation starts at its current position");
                await Task.Delay(40);
                before = move.Y;
                Call("ShowView", "queue");
                Check(Math.Abs(move.Y - before) < 2, "Interrupted navigation remains continuous");
                Call("ShowView", "settings");
                Call("ShowView", "parse");
                await Task.Delay(650);
                var pages = new[] { "ParseView", "QueueView", "DoneView", "SettingsView" }.Select(Field<FrameworkElement>).ToArray();
                Check(pages.Count(p => p.Visibility == Visibility.Visible) == 1 && pages[0].Visibility == Visibility.Visible, "Rapid switching leaves exactly the requested page");
                Check(pages[0].IsHitTestVisible && pages[0].Opacity == 1 && pages[0].Effect is null, "Rapid switching clears stale effects and restores input");
                var button = Field<Button>("ProbeButton");
                var content = button.Content;
                var urlBox = Field<TextBox>("UrlBox");
                urlBox.ApplyTemplate();
                Check(((Border)urlBox.Template.FindName("Chrome", urlBox)).BorderThickness == new Thickness(0), "Parse URL input has no nested frame");
                var loginBar = Field<FrameworkElement>("BilibiliLoginBar");
                foreach (var url in new[] { "https://www.bilibili.com/video/BV1tFZZBQE57", "https://b23.tv/example" })
                {
                    urlBox.Text = url;
                    Check(loginBar.Visibility == Visibility.Visible, "B 站链接显示解析页登录入口");
                }
                urlBox.Text = "https://example.com/video";
                Check(loginBar.Visibility == Visibility.Collapsed, "普通链接隐藏 B 站登录入口");
                urlBox.Text = "";
                var authSource = Field<ComboBox>("ParseAuthSourceBox");
                var parseCookiesFile = Field<TextBox>("ParseCookiesFileBox");
                Check(window.FindName("CookiesBrowserBox") is null && window.FindName("CookiesFileBox") is null,
                      "高级设置已移除重复 Cookie 登录入口");
                Check(authSource.Items.OfType<ComboBoxItem>().Select(item => item.Tag?.ToString())
                          .SequenceEqual(new[] { "", "edge", "chrome", "firefox", "file" }),
                      "解析页保留已有登录来源与扫码后的会话读取");
                authSource.SelectedValue = "file";
                Check(parseCookiesFile.Visibility == Visibility.Visible && parseCookiesFile.IsReadOnly &&
                      Field<Button>("BrowseCookiesButton").Visibility == Visibility.Visible, "解析页选择文件仍提供路径和选择按钮");
                foreach (var source in new[] { "edge", "chrome", "firefox", "" })
                {
                    parseCookiesFile.Text = Path.Combine(Path.GetDirectoryName(settingsPath)!, "cookies-test.txt");
                    authSource.SelectedValue = source;
                    Check(parseCookiesFile.Text == "" && parseCookiesFile.Visibility == Visibility.Collapsed,
                          $"解析页切换登录来源 {source} 仅清理尚未提交的文件选择");
                }
                var recognize = Field<CheckBox>("RecognizeCheck");
                var translate = Field<CheckBox>("TranslateCheck");
                var subtitleMode = Field<ComboBox>("SubtitleModeBox");
                var subtitleAdvanced = Field<Expander>("SubtitleAdvanced");
                Check(subtitleMode.Items.Count == 3 && !subtitleAdvanced.IsExpanded,
                      "Subtitle choices start with three modes and collapsed advanced settings");
                subtitleMode.SelectedValue = "source";
                await Task.Delay(270);
                Check(recognize.IsChecked == true && translate.IsChecked != true &&
                      Field<FrameworkElement>("TranslationLanguageRow").Visibility == Visibility.Collapsed,
                      "Source subtitle mode enables recognition without translation choices");
                subtitleMode.SelectedValue = "translate";
                await Task.Delay(270);
                Check(recognize.IsChecked == true && translate.IsChecked == true &&
                      Field<ComboBox>("TranslationParseBox").SelectedValue?.ToString() == "llm" &&
                      Field<FrameworkElement>("TranslationLanguageRow").Visibility == Visibility.Visible,
                      "Translation mode enables recognition and its translation provider together");
                Field<CheckBox>("BurnCheck").IsChecked = true;
                subtitleMode.SelectedValue = "none";
                await Task.Delay(270);
                Check(recognize.IsChecked != true && translate.IsChecked != true &&
                      Field<CheckBox>("BurnCheck").IsChecked != true &&
                      Field<FrameworkElement>("BurnCard").Visibility == Visibility.Collapsed,
                      "No-subtitle mode clears dependent switches and hides burn settings");
                translate.IsChecked = false;
                recognize.IsChecked = false;
                Call("SyncDependentControls", false);
                recognize.IsChecked = true;
                Call("SyncDependentControls", true);
                var recognitionOptions = Field<FrameworkElement>("RecognitionOptions");
                Check(recognitionOptions.Visibility == Visibility.Visible && (!SystemParameters.ClientAreaAnimation || !double.IsPositiveInfinity(recognitionOptions.MaxHeight)), "Recognition options expand with motion");
                recognize.IsChecked = false;
                Call("SyncDependentControls", true);
                recognize.IsChecked = true;
                Call("SyncDependentControls", true);
                await Task.Delay(270);
                Check(recognitionOptions.Visibility == Visibility.Visible && double.IsPositiveInfinity(recognitionOptions.MaxHeight), "Interrupted recognition motion settles in the latest state");
                recognize.IsChecked = false;
                Call("SyncDependentControls", false);
                translate.IsChecked = true;
                Call("SubtitleMode_Changed", translate, new RoutedEventArgs());
                var translationRow = Field<FrameworkElement>("TranslationLanguageRow");
                Check(recognize.IsChecked == true && translationRow.Visibility == Visibility.Visible && (!SystemParameters.ClientAreaAnimation || !double.IsPositiveInfinity(translationRow.MaxHeight)), "Translation switch expands dependent options with motion");
                await Task.Delay(270);
                Check(double.IsPositiveInfinity(translationRow.MaxHeight), "Translation motion finishes without clipping content");
                translate.IsChecked = false;
                recognize.IsChecked = false;
                Call("SyncDependentControls", false);
                var hero = Field<FrameworkElement>("ParseHero");
                var heroTop = hero.TranslatePoint(new Point(), Field<FrameworkElement>("ParseView")).Y;
                Call("SetProbeBusy", true);
                Check(Field<FrameworkElement>("ParseLoading").Visibility == Visibility.Visible, "Loading skeleton is visible during analysis");
                await Task.Delay(170);
                var skeleton = Field<FrameworkElement>("ParseLoading");
                var block = Descendants(skeleton).OfType<Control>().First(c => c.Style == window.Resources["SkeletonBlock"]);
                var sweep = (TranslateTransform)block.Template.FindName("Sweep", block);
                var position = sweep.X;
                await Task.Delay(190);
                Check(!SystemParameters.ClientAreaAnimation || Math.Abs(sweep.X - position) > .05, "Visible skeleton shimmer advances over time");
                await Task.Delay(100);
                Check(hero.TranslatePoint(new Point(), Field<FrameworkElement>("ParseView")).Y < heroTop - 40, "First analysis moves the centered hero to the top");
                var spinner = Field<FrameworkElement>("ProbeSpinner");
                var rotation = Field<RotateTransform>("ProbeSpinnerRotate");
                var angle = rotation.Angle;
                await Task.Delay(100);
                Check(spinner.IsVisible && (!SystemParameters.ClientAreaAnimation || Math.Abs(rotation.Angle - angle) > 10), "Analysis displays a rotating vector loader");
                Call("SetProbeBusy", false);
                Check(!spinner.IsVisible, "Analysis completion hides the loader");
                Check(ReferenceEquals(content, button.Content) && content is Panel, "Analysis preserves the vector button content");
                Check(Field<FrameworkElement>("ParseLoading").Visibility == Visibility.Collapsed, "Loading skeleton disappears after analysis");
                Check(window.WindowStyle != WindowStyle.None, "Native caption styles remain available to DWM");
                urlBox.Text = "https://www.bilibili.com/video/BV1tFZZBQE57";
                var selectedFormat = new FormatInfo { FormatId = "raw-format-id", Label = "4K / 2160p", Height = 2160,
                    Fps = 60, VideoCodec = "av01", DynamicRange = "HDR10", Protocol = "https", FileSize = 1048576 };
                Check(selectedFormat.DetailText.Contains("60 fps") && selectedFormat.DetailText.Contains("AV1") &&
                      selectedFormat.DetailText.Contains("HDR10"), "画质附加信息包含帧率、编码与 HDR");
                window.ProbeFormats.Add(selectedFormat);
                foreach (var height in new[] { 2160, 1080, 720, 480, 360 })
                    window.ProbeFormats.Add(new FormatInfo { FormatId = "other-format-" + height,
                        Label = height == 2160 ? "4K / 2160p" : $"{height}p", Height = height,
                        Fps = 30, VideoCodec = "avc1", DynamicRange = "SDR", FileSize = 524288 });
                var probeType = typeof(MainWindow).Assembly.GetType("Jiying.Desktop.ProbeResult")!;
                typeof(MainWindow).GetField("_probe", BindingFlags.Instance | BindingFlags.NonPublic)!.SetValue(window,
                    System.Text.Json.JsonSerializer.Deserialize("""{"engine":"ytdlp","title":"B 站 4K 视频预览"}""", probeType));
                Field<FrameworkElement>("ProbePanel").Visibility = Visibility.Visible;
                Field<TextBlock>("ProbeTitle").Text = "B 站 4K 视频预览";
                Field<TextBlock>("ProbeMeta").Text = "视频已解析 · 4K / 2160p";
                Field<TextBlock>("ProbeQualityHint").Text = "已读取到 4K 画质。登录来源变更后可刷新画质。";
                Field<TextBlock>("ProbeQualityHint").Visibility = Visibility.Visible;
                Field<TextBlock>("ParseAuthHint").Text = "扫码连接后自动刷新画质。4K 取决于视频与账号权限。";
                Call("SetProbeBusy", false);
                Field<ComboBox>("FormatList").SelectedIndex = 0;
                subtitleMode.SelectedValue = "translate";
                await Task.Delay(270);
                Check(Field<TextBlock>("SubmitHint").Text.Contains(selectedFormat.Label), "下载摘要明确显示所选画质");
                var previewBody = Field<StackPanel>("ProbePreviewBody");
                var formatList = Field<ComboBox>("FormatList");
                var submitButton = Field<Button>("SubmitButton");
                Check(previewBody.Children.IndexOf(formatList) >= 0 &&
                      previewBody.Children.IndexOf(Field<Grid>("SubmitActions")) == previewBody.Children.IndexOf(formatList) + 1,
                      "开始下载按钮紧邻解析预览中的画质列表");
                Check(formatList.Height == 40 && formatList.MaxDropDownHeight >= 400, "紧凑画质选择将选项展开到浮层，不挤占下载按钮");
                Check(Math.Abs(Field<TextBlock>("ParseHeading").FontSize - 24) < .1 &&
                      Field<FrameworkElement>("ParseHeading").Visibility == Visibility.Collapsed &&
                      Field<FrameworkElement>("ParseDescription").Visibility == Visibility.Collapsed &&
                      Field<FrameworkElement>("ParseExamples").Visibility == Visibility.Collapsed,
                      "解析完成后隐藏页首标题、介绍和示例，为画质与下载保留空间");
                var screenshotUi = typeof(MainWindow).GetField("_ui", BindingFlags.Instance | BindingFlags.NonPublic)!.GetValue(window)!;
                var screenshotTheme = screenshotUi.GetType().GetProperty("Theme")!;
                var originalTheme = screenshotTheme.GetValue(screenshotUi);
                window.Width = 1160;
                window.Height = 720;
                var desktopScroller = Field<ScrollViewer>("ParseScroller");
                desktopScroller.ScrollToTop();
                window.UpdateLayout();
                var desktopSubmitPosition = submitButton.TranslatePoint(new Point(), desktopScroller);
                Console.WriteLine($"DESKTOP CTA y={desktopSubmitPosition.Y:0.#}, height={submitButton.ActualHeight:0.#}, viewport={desktopScroller.ActualHeight:0.#}, scroll={desktopScroller.VerticalOffset:0.#}");
                Check(Math.Abs(Field<FrameworkElement>("ProbePreviewThumb").Width - 264) < .1 &&
                      Math.Abs(Field<FrameworkElement>("ProbePreviewThumb").Height - 148) < .1,
                      "桌面预览保留 264×148 缩略图");
                Check(Math.Abs(desktopScroller.VerticalOffset) < .1 && desktopSubmitPosition.Y >= 0 &&
                      desktopSubmitPosition.Y + submitButton.ActualHeight <= desktopScroller.ActualHeight + 1,
                      "桌面解析完成后无需滚动即可看到开始下载按钮");
                for (var i = 0; i < 20; i++) window.ProbeFormats.Add(new FormatInfo { FormatId = $"variant-{i}", Label = $"1080p {i + 1}", Fps = 30 });
                window.UpdateLayout();
                Check(Math.Abs(submitButton.TranslatePoint(new Point(), desktopScroller).Y - desktopSubmitPosition.Y) < .1 &&
                      TextSearch.GetTextPath(formatList) == "Label", "大量画质不增加页面高度，支持输入画质名称直接定位");
                foreach (var screenshotMode in new[] { "dark", "light" })
                {
                    screenshotTheme.SetValue(screenshotUi, screenshotMode);
                    Call("ApplyAppearance");
                    window.UpdateLayout();
                    await Task.Delay(120);
                    Capture("JIYING_PARSE_" + screenshotMode.ToUpperInvariant() + "_SCREENSHOT");
                }
                screenshotTheme.SetValue(screenshotUi, originalTheme);
                Call("ApplyAppearance");
                window.Width = 860;
                window.Height = 560;
                window.UpdateLayout();
                var thumbBounds = Field<FrameworkElement>("NavThumb").TranslatePoint(new Point(), Field<FrameworkElement>("NavHost"));
                Check(Math.Abs(thumbBounds.X) < 1, "Compact navigation indicator aligns with the selected first item");
                Check(Grid.GetRow(Field<FrameworkElement>("SkeletonLines")) == 1, "Compact loading skeleton stacks below its thumbnail");
                Check(Grid.GetRow(previewBody) == 0 && Grid.GetColumn(previewBody) == 1 &&
                      Math.Abs(Field<ColumnDefinition>("ProbeThumbColumn").Width.Value - 140) < .1 &&
                      Math.Abs(Field<FrameworkElement>("ProbePreviewThumb").Width - 120) < .1 &&
                      Math.Abs(Field<FrameworkElement>("ProbePreviewThumb").Height - 67.5) < .1,
                      "紧凑预览使用左侧小缩略图和右侧画质，避免大图挤出下载按钮");
                Check(Math.Abs(Field<FrameworkElement>("ProbePanel").Margin.Top - 30) < .1, "Preview keeps the original 30-pixel top spacing");
                Check(Math.Abs(Field<FrameworkElement>("DirectoryDialog").Width - 560) < .1 && Field<FrameworkElement>("SubtitleDialog").Width > 640 && Field<FrameworkElement>("SubtitleDialog").Width <= 940 && Math.Abs(Field<FrameworkElement>("DeleteDialog").Width - 520) < .1, "字幕对照窗口扩宽，并保持目录与删除窗口尺寸");
                formatList.IsDropDownOpen = true;
                window.UpdateLayout();
                await Task.Delay(80);
                var formatItem = formatList.ItemContainerGenerator.ContainerFromIndex(0) as ComboBoxItem;
                Check(formatItem is not null, "Format metadata renders without writing to read-only properties");
                var formatText = formatItem is null ? "" : string.Join(" ", Descendants(formatItem).OfType<TextBlock>().Select(t => t.Text));
                Check(formatText.Contains("4K / 2160p") && formatText.Contains("60 fps") && formatText.Contains("HDR10") &&
                      !formatText.Contains("raw-format-id") && !formatText.Contains("https"),
                      "画质列表显示 4K、帧率和 HDR，隐藏原始格式标识与协议");
                formatList.IsDropDownOpen = false;
                var thumbnailClip = Field<Border>("ProbePreviewThumb").Clip as RectangleGeometry;
                Check(Field<Image>("ProbeThumbnail") is not null && thumbnailClip is not null && Math.Abs(thumbnailClip.RadiusX - 12) < .1, "Parsed thumbnail image is clipped to a true rounded rectangle");
                submitButton.BringIntoView();
                await Task.Delay(100);
                window.UpdateLayout();
                Capture("JIYING_PARSE_COMPACT_SCREENSHOT");
                window.Width = 1160;
                window.Height = 720;
                Field<ScrollViewer>("ParseScroller").ScrollToBottom();
                window.UpdateLayout();
                await Task.Delay(100);
                Capture("JIYING_PARSE_SUBTITLE_SCREENSHOT");
                window.Width = 860;
                window.Height = 560;
                subtitleAdvanced.IsExpanded = true;
                window.UpdateLayout();
                Check(subtitleMode.SelectedValue?.ToString() == "translate" &&
                      Field<ComboBox>("WhisperParseBox").SelectedValue?.ToString() == "small",
                      "Opening advanced settings preserves the subtitle mode and model");
                foreach (var name in new[] { "LlmBaseParseBox", "LlmModelParseBox", "AsrBaseParseBox", "AsrModelParseBox" })
                {
                    var control = Field<FrameworkElement>(name);
                    var host = Field<FrameworkElement>("ParseLayout");
                    var left = control.TranslatePoint(new Point(), host).X;
                    Check(control.ActualWidth > 0 && left >= 0 && left + control.ActualWidth <= host.ActualWidth + 1,
                          name + " fits when advanced settings are expanded at the minimum width");
                }
                subtitleAdvanced.IsExpanded = false;
                var parseScroller = Field<ScrollViewer>("ParseScroller");
                submitButton.BringIntoView();
                window.UpdateLayout();
                await Task.Delay(80);
                var submitPosition = Field<FrameworkElement>("SubmitButton").TranslatePoint(new Point(), parseScroller);
                Check(submitPosition.Y >= 0 && submitPosition.Y + Field<FrameworkElement>("SubmitButton").ActualHeight <= parseScroller.ActualHeight + 1,
                      "The task submit button remains reachable in the minimum window size");
                var toastHost = Field<StackPanel>("ToastHost");
                Call("ShowToast", "任务已创建", "测试提示一");
                var firstToast = toastHost.Children[^1];
                Call("ShowToast", "检查失败", "测试提示二");
                Check(toastHost.Children.Contains(firstToast) &&
                      !ReferenceEquals(firstToast, toastHost.Children[^1]),
                      "Original toast overlay stacks concurrent messages");
                toastHost.Children.Clear();
                ((CancellationTokenSource)typeof(MainWindow).GetField("_stop", BindingFlags.Instance | BindingFlags.NonPublic)!.GetValue(window)!).Cancel();
                var engine = typeof(MainWindow).GetField("_engine", BindingFlags.Instance | BindingFlags.NonPublic)!.GetValue(window)!;
                var enginePort = engine.GetType().GetProperty("Port")!;
                enginePort.SetValue(engine, 1);
                var previousFailures = failures.Count;
                await (Task)typeof(MainWindow).GetMethod("SaveEngineConfigAsync", BindingFlags.Instance | BindingFlags.NonPublic)!.Invoke(window, [false])!;
                Field<TextBox>("UrlBox").Text = "https://example.com/video";
                Call("Probe_Click", Field<Button>("ProbeButton"), new RoutedEventArgs());
                Call("DownloadWhisper_Click", Field<Button>("DownloadWhisperButton"), new RoutedEventArgs());
                await Task.Delay(80);
                enginePort.SetValue(engine, null);
                Check(failures.Count == previousFailures && toastHost.Children.Count == 0,
                      "Closing cancellation does not escape async void handlers or config lock acquisition");
                window.QueueTasks.Clear();
                window.QueueTasks.Add(new TaskItem { Id = "ui-check", Title = "UI check", Status = "running", Progress = new TaskProgress { Fraction = .5 } });
                Field<ListView>("QueueList").Visibility = Visibility.Visible;
                Field<FrameworkElement>("QueueEmpty").Visibility = Visibility.Collapsed;
                Call("ShowView", "queue");
                await Task.Delay(550);
                window.UpdateLayout();
                Check(Field<ListView>("QueueList").ItemContainerGenerator.ContainerFromIndex(0) is ListViewItem, "Task card renders read-only progress without a two-way binding crash");
                var card = Field<ListView>("QueueList").ItemContainerGenerator.ContainerFromIndex(0);
                var originalTask = window.QueueTasks[0];
                var stageStrip = Descendants((DependencyObject)card!).OfType<ItemsControl>()
                    .First(c => c.ItemsSource is List<StageView>);
                var firstStage = stageStrip.ItemContainerGenerator.ContainerFromIndex(0);
                typeof(MainWindow).GetMethod("SyncTasks", BindingFlags.Static | BindingFlags.NonPublic)!
                    .Invoke(null, [window.QueueTasks, new[] { new TaskItem { Id = "ui-check", Title = "UI check", Status = "running", Progress = new TaskProgress { Fraction = .75 } } }]);
                window.UpdateLayout();
                Check(ReferenceEquals(originalTask, window.QueueTasks[0]) &&
                      ReferenceEquals(card, Field<ListView>("QueueList").ItemContainerGenerator.ContainerFromIndex(0)) &&
                      Math.Abs(window.QueueTasks[0].ProgressValue - 75) < .1,
                      "Queue polling preserves the existing task card");
                Check(firstStage is not null && ReferenceEquals(firstStage, stageStrip.ItemContainerGenerator.ContainerFromIndex(0)),
                      "Progress refresh preserves existing stage visuals");
                var notifications = new List<string?>();
                originalTask.PropertyChanged += (_, e) => notifications.Add(e.PropertyName);
                typeof(MainWindow).GetMethod("SyncTasks", BindingFlags.Static | BindingFlags.NonPublic)!
                    .Invoke(null, [window.QueueTasks, new[] { new TaskItem { Id = "ui-check", Title = "UI check", Status = "running", Progress = new TaskProgress { Fraction = .75 } } }]);
                Check(notifications.Count == 0, "Unchanged task polling avoids binding refreshes");
                typeof(MainWindow).GetMethod("SyncTasks", BindingFlags.Static | BindingFlags.NonPublic)!
                    .Invoke(null, [window.QueueTasks, new[] { new TaskItem { Id = "ui-check", Title = "UI check", Status = "running", Progress = new TaskProgress { Fraction = .80 } } }]);
                Check(notifications.Contains(nameof(TaskItem.ProgressValue)) && !notifications.Contains("") &&
                      !notifications.Contains(nameof(TaskItem.StageViews)) && Math.Abs(originalTask.ProgressValue - 80) < .1,
                      "Progress changes update only progress bindings");
                var fresh = new TaskItem { Status = "completed", Error = "" };
                typeof(MainWindow).GetMethod("CopyTaskDetail", BindingFlags.Static | BindingFlags.NonPublic)!
                    .Invoke(null, [fresh, new TaskItem { Status = "completed", Error = "old failure" }]);
                Check(fresh.Error == "", "Cached details cannot restore an error cleared by retry");
                window.Width = 1160;
                window.Height = 720;
                window.UpdateLayout();
                foreach (var name in new[] { "Queue", "Done" })
                {
                    var badge = Field<Border>($"{name}Badge");
                    var nav = Field<Button>($"{name}Nav");
                    badge.Visibility = Visibility.Visible;
                    Field<TextBlock>($"{name}BadgeText").Text = "2";
                    window.UpdateLayout();
                    var right = badge.TranslatePoint(new Point(badge.ActualWidth, 0), nav).X;
                    Check(nav.ActualWidth - right < 18 && badge.ActualWidth <= 32, $"{name} count badge stays at the rail edge");
                }
                window.Width = 860;
                window.Height = 560;
                window.UpdateLayout();
                var screenshotPath = Environment.GetEnvironmentVariable("JIYING_UI_SCREENSHOT");
                if (!string.IsNullOrWhiteSpace(screenshotPath))
                {
                    var bitmap = new RenderTargetBitmap((int)window.ActualWidth, (int)window.ActualHeight, 96, 96, PixelFormats.Pbgra32);
                    bitmap.Render(window);
                    var encoder = new PngBitmapEncoder();
                    encoder.Frames.Add(BitmapFrame.Create(bitmap));
                    using var stream = File.Create(screenshotPath);
                    encoder.Save(stream);
                }
                foreach (var name in new[] { "LlmBaseParseBox", "LlmModelParseBox", "AsrBaseParseBox", "AsrModelParseBox" })
                {
                    var control = Field<FrameworkElement>(name);
                    var host = Field<FrameworkElement>("ParseLayout");
                    var left = control.TranslatePoint(new Point(), host).X;
                    Check(left >= 0 && left + control.ActualWidth <= host.ActualWidth + 1, name + " fits the minimum window width");
                }
                Call("ShowView", "settings");
                await Task.Delay(550);
                var scroller = Field<ScrollViewer>("SettingsScroller");
                var scrollBar = (ScrollBar)scroller.Template.FindName("PART_VerticalScrollBar", scroller);
                Check(Math.Abs(scrollBar.ActualWidth - 10) < .1, "Scrollbar keeps the original 10-pixel layout width");
                scroller.ScrollToBottom();
                window.UpdateLayout();
                Check(scroller.VerticalOffset > 0 && Math.Abs(scroller.VerticalOffset - scroller.ScrollableHeight) < 1, "Settings can scroll to the last controls");
                var ui = typeof(MainWindow).GetField("_ui", BindingFlags.Instance | BindingFlags.NonPublic)!.GetValue(window)!;
                var theme = ui.GetType().GetProperty("Theme")!;
                var opacity = ui.GetType().GetProperty("BackgroundOpacity")!;
                var oldTheme = theme.GetValue(ui);
                var oldOpacity = opacity.GetValue(ui);
                theme.SetValue(ui, "dark");
                opacity.SetValue(ui, 40);
                Call("ApplyAppearance");
                var softTint = ((SolidColorBrush)window.Resources["WindowBrush"]).Color.A;
                Check(Math.Abs(window.Opacity - 1) < .001, "背景透明度不改变文字和控件整体透明度");
                opacity.SetValue(ui, 100);
                Call("ApplyAppearance");
                Check(((SolidColorBrush)window.Resources["WindowBrush"]).Color.A >= softTint, "浓度滑块控制背板层次");
                Check(Field<Image>("BackgroundImage").Source is BitmapSource && window.FindName("GrainLayer") is not null, "恢复原来的背景纹理和磨砂颗粒");
                Check(((SolidColorBrush)window.Resources["WindowBrush"]).Color.R == 13 && ((SolidColorBrush)window.Resources["WindowBrush"]).Color.G == 17, "深色背板恢复原版色调");
                Check(((SolidColorBrush)window.Resources["WindowBrush"]).Color.A > 0, "Dark shell prevents desktop colors bleeding through");
                var settingsInput = Field<TextBox>("LlmBaseSettingsBox");
                Check(Math.Abs(settingsInput.Height - 34) < .001 && settingsInput.VerticalAlignment == VerticalAlignment.Center, "Text inputs share the 34-pixel control baseline");
                Call("UpdateWindowClip");
                Check(Field<Grid>("RootGrid").Clip is RectangleGeometry { RadiusX: 12, RadiusY: 12 }, "Restored window clips content to rounded corners");
                theme.SetValue(ui, "light");
                Call("ApplyThemeWithTransition");
                Check(Field<Image>("ThemeTransitionLayer").Source is BitmapSource && Field<Image>("ThemeTransitionLayer").Visibility == Visibility.Visible, "Theme switch starts from a captured full-window frame");
                await Task.Delay(300);
                Check(Field<Image>("ThemeTransitionLayer").Visibility == Visibility.Collapsed, "Theme crossfade clears its snapshot after the transition");
                Check(((SolidColorBrush)window.Resources["WindowBrush"]).Color.R == 252 && ((SolidColorBrush)window.Resources["TextBrush"]).Color.R == 48, "浅色背板和文字恢复原版色调");
                var defaultOutput = (string)ui.GetType().GetProperty("DefaultOutputDirectory", BindingFlags.Static | BindingFlags.NonPublic)!.GetValue(null)!;
                Check(defaultOutput == defaultPath, "Default download directory stays portable");
                theme.SetValue(ui, oldTheme);
                opacity.SetValue(ui, oldOpacity);
                Call("ApplyAppearance");
                var doneScreenshot = Environment.GetEnvironmentVariable("JIYING_DONE_SCREENSHOT");
                if (!string.IsNullOrWhiteSpace(doneScreenshot))
                {
                    window.Width = 1160;
                    window.Height = 720;
                    window.DoneTasks.Add(new TaskItem { Id = "done-check", Title = "示例视频", Status = "completed", CreatedAt = "2026-09-24T20:18:00", OutputDirectory = @"D:\即影\Downloads" });
                    Field<TextBlock>("QueueBadgeText").Text = "1";
                    Field<TextBlock>("DoneBadgeText").Text = "1";
                    Field<TextBlock>("DoneCountText").Text = "共 1 条记录";
                    Field<ListView>("DoneList").Visibility = Visibility.Visible;
                    Field<FrameworkElement>("DoneEmpty").Visibility = Visibility.Collapsed;
                    Call("ShowView", "done");
                    await Task.Delay(320);
                    window.UpdateLayout();
                    var doneItem = Field<ListView>("DoneList").ItemContainerGenerator.ContainerFromIndex(0) as ListViewItem;
                    Check(doneItem?.Template.LoadContent() is ContentPresenter, "Completed card has no square hover chrome behind rounded edges");
                    var bitmap = new RenderTargetBitmap((int)window.ActualWidth, (int)window.ActualHeight, 96, 96, PixelFormats.Pbgra32);
                    bitmap.Render(window);
                    var encoder = new PngBitmapEncoder();
                    encoder.Frames.Add(BitmapFrame.Create(bitmap));
                    using var stream = File.Create(doneScreenshot);
                    encoder.Save(stream);
                }
            }
            catch (Exception ex) { failures.Add(ex.ToString()); }
            finally
            {
                // 回归清理仅抛弃隔离编辑对象，失败时不等待未保存字幕的人工确认。
                typeof(MainWindow).GetField("_editingArtifact", BindingFlags.Instance | BindingFlags.NonPublic)!.SetValue(window, null);
                window.Close();
                if (originalSettings is null)
                {
                    File.Delete(settingsPath);
                    Directory.Delete(Path.GetDirectoryName(settingsPath)!);
                }
                else File.WriteAllBytes(settingsPath, originalSettings);
                Environment.SetEnvironmentVariable("JIYING_UI_SETTINGS_PATH", previousSettingsPath);
                app.Shutdown();
            }
        });
        app.Run();
        foreach (var failure in failures) Console.Error.WriteLine("FAIL " + failure);
        return failures.Count == 0 ? 0 : 1;

        T Field<T>(string name) => (T)(typeof(MainWindow).GetField(name, BindingFlags.Instance | BindingFlags.NonPublic)?.GetValue(window)
            ?? throw new Exception("Missing field: " + name));
        void Call(string name, params object[] args) => (typeof(MainWindow).GetMethod(name, BindingFlags.Instance | BindingFlags.NonPublic)
            ?? throw new Exception("Missing method: " + name)).Invoke(window, args);
        void SetStatus(string typeName, string method, string json)
        {
            var type = typeof(MainWindow).Assembly.GetType("Jiying.Desktop." + typeName)!;
            Call(method, System.Text.Json.JsonSerializer.Deserialize(json, type)!);
        }
        void Capture(string variable)
        {
            var path = Environment.GetEnvironmentVariable(variable);
            if (string.IsNullOrWhiteSpace(path)) return;
            Directory.CreateDirectory(Path.GetDirectoryName(path)!);
            var bitmap = new RenderTargetBitmap((int)window.ActualWidth, (int)window.ActualHeight, 96, 96, PixelFormats.Pbgra32);
            bitmap.Render(window);
            var encoder = new PngBitmapEncoder();
            encoder.Frames.Add(BitmapFrame.Create(bitmap));
            using var stream = File.Create(path);
            encoder.Save(stream);
        }
        async Task CheckSubtitleEntry()
        {
            var ordinary = new ArtifactItem { Id = 101, Kind = "subtitle", Language = "en", Path = "video.en.srt", TaskId = "edit-test" };
            var bilingual = new ArtifactItem { Id = 102, Kind = "bilingual", Language = "zh+en", Path = "video.zh+en.srt", TaskId = "edit-test" };
            Check(ordinary.IsSubtitle && bilingual.IsSubtitle, "单语与双语字幕均可编辑");
            window.DoneTasks.Clear();
            window.DoneTasks.Add(new TaskItem
            {
                Id = "edit-test", Title = "下载完成后修正字幕", Status = "completed", SubtitleMode = "burn",
                Artifacts = [ordinary, bilingual, new ArtifactItem { Id = 103, Kind = "subtitle_backup", Path = "backup.srt" }]
            });
            Field<FrameworkElement>("DoneEmpty").Visibility = Visibility.Collapsed;
            Field<FrameworkElement>("DoneList").Visibility = Visibility.Visible;
            Field<TextBlock>("DoneCountText").Text = "共 1 条记录";
            Call("ShowView", "done");
            await Task.Delay(350);
            window.UpdateLayout();
            var edits = Descendants(Field<ListView>("DoneList")).OfType<Button>()
                .Where(button => button.Tag is ArtifactItem && button.IsVisible).ToArray();
            Check(edits.Length == 2, "完成的下载显示两个字幕修改入口，隐藏备份文件");
            Check(edits.FirstOrDefault()?.Tag == bilingual, "双语字幕修改入口优先显示");
            Check(edits.Any() && edits.All(button => button.IsEnabled && button.Content?.ToString()?.Contains("修改字幕") == true), "字幕入口以清楚的文字呈现");
            Check(edits.Any() && edits.All(button => button.Focusable && button.IsTabStop), "字幕入口支持键盘焦点");
            Capture("JIYING_SUBTITLE_DONE_SCREENSHOT");
            if (edits.Length > 0) await CheckSubtitleWorkflow(edits[0]);
            window.DoneTasks.Clear();
            window.QueueTasks.Clear();
            Field<System.Collections.Concurrent.ConcurrentDictionary<string, TaskItem>>("_terminalDetails").TryRemove("edit-test", out _);
            Field<System.Collections.Concurrent.ConcurrentDictionary<string, string>>("_knownStatuses").TryRemove("edit-test", out _);
            Call("ShowView", "parse");
            await Task.Delay(350);
        }
        async Task CheckSubtitleWorkflow(Button edit)
        {
            var engine = typeof(MainWindow).GetField("_engine", BindingFlags.Instance | BindingFlags.NonPublic)!.GetValue(window)!;
            var port = engine.GetType().GetProperty("Port")!;
            using var stop = new CancellationTokenSource();
            var listener = new TcpListener(IPAddress.Loopback, 0);
            listener.Start();
            var requests = new System.Collections.Concurrent.ConcurrentQueue<(string Method, string Path, string Body)>();
            var saveReached = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
            var releaseSave = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
            var rejectSave = false;
            var rejectRender = false;
            var renderAccepted = false;
            var originalText = "1\n00:00:00,000 --> 00:00:01,000\n识别错字\nWrong\n\n";
            var corrected = originalText.Replace("识别错字", "识别正确").Replace("Wrong", "Corrected");
            var serverTask = Task.Run(ServeSubtitleRequests);
            try
            {
                port.SetValue(engine, ((IPEndPoint)listener.LocalEndpoint).Port);
                edit.RaiseEvent(new RoutedEventArgs(Button.ClickEvent));
                await Until(() => Field<FrameworkElement>("SubtitleDialog").Visibility == Visibility.Visible);
                var text = Field<TextBox>("SubtitleTextBox");
                Check(text.Text == originalText && Field<TextBlock>("SubtitleDialogTitle").Text.Contains("双语"), "真实点击双语入口读取完整字幕");
                await Task.Delay(350);
                window.UpdateLayout();
                Check(Field<StackPanel>("ToastHost").Children.Count == 0, "正常打开字幕时没有错误提示");
                Capture("JIYING_SUBTITLE_EDITOR_SCREENSHOT");
                var width = window.Width;
                var height = window.Height;
                var ui = typeof(MainWindow).GetField("_ui", BindingFlags.Instance | BindingFlags.NonPublic)!.GetValue(window)!;
                var theme = ui.GetType().GetProperty("Theme")!;
                var originalTheme = theme.GetValue(ui);
                foreach (var mode in new[] { "dark", "light" })
                {
                theme.SetValue(ui, mode);
                Call("ApplyAppearance");
                foreach (var dimensions in new[] { (860d, 560d), (1200d, 820d) })
                {
                    window.Width = dimensions.Item1;
                    window.Height = dimensions.Item2;
                    text.Text = string.Concat(Enumerable.Repeat(originalText, 100));
                    window.UpdateLayout();
                    await Task.Delay(100);
                    var overlay = Field<FrameworkElement>("ModalOverlay");
                    foreach (var name in new[] { "SubtitleRows", "SubtitleSaveButton", "SubtitleRenderButton", "SubtitleHint" })
                    {
                        var element = Field<FrameworkElement>(name);
                        var bounds = element.TransformToAncestor(overlay).TransformBounds(new Rect(element.RenderSize));
                        Check(bounds.Top >= 0 && bounds.Bottom <= overlay.ActualHeight + 1 &&
                              bounds.Left >= 0 && bounds.Right <= overlay.ActualWidth + 1, $"{name} 在 {mode}/{dimensions.Item1} 宽窗口中完整可见");
                    }
                    if (mode == "light" && dimensions.Item1 == 1200) Capture("JIYING_SUBTITLE_LIGHT_SCREENSHOT");
                }
                }
                theme.SetValue(ui, originalTheme);
                Call("ApplyAppearance");
                window.Width = width;
                window.Height = height;
                var rows = Field<DataGrid>("SubtitleRows");
                Check(rows.Visibility == Visibility.Visible && text.Visibility == Visibility.Collapsed, "正常字幕使用逐条列表");
                ((SubtitleCue)rows.Items[0]).Text = "识别正确\nCorrected";
                Check(text.Text == corrected, "逐条文字修改同步保存稿并保留时间轴");
                var save = Field<Button>("SubtitleSaveButton");
                var render = Field<Button>("SubtitleRenderButton");
                save.RaiseEvent(new RoutedEventArgs(Button.ClickEvent));
                await saveReached.Task.WaitAsync(TimeSpan.FromSeconds(3));
                Check(!save.IsEnabled && !render.IsEnabled && text.IsReadOnly, "保存期间锁定输入和操作");
                save.RaiseEvent(new RoutedEventArgs(Button.ClickEvent));
                releaseSave.TrySetResult();
                await Until(() => !Field<bool>("_subtitleBusy"));
                Check(requests.Count(request => request.Method == "PUT") == 1 && text.Text == corrected, "重复保存只发出一次请求，并保留修改内容");
                Check(Field<TextBlock>("SubtitleHint").Text.Contains("已保存") && render.IsEnabled, "保存后仍能继续修改和生成视频");
                rejectSave = true;
                render.RaiseEvent(new RoutedEventArgs(Button.ClickEvent));
                await Until(() => !Field<bool>("_subtitleBusy"));
                Check(!requests.Any(request => request.Path.EndsWith("/rerender")) && text.Text == corrected &&
                      Field<TextBlock>("SubtitleHint").Text.Contains("保存失败"), "字幕保存失败时阻止生成并保留修改");
                rejectSave = false;
                rejectRender = true;
                render.RaiseEvent(new RoutedEventArgs(Button.ClickEvent));
                await Until(() => !Field<bool>("_subtitleBusy"));
                Check(Field<TextBlock>("SubtitleHint").Text.Contains("未启动") && render.IsEnabled && text.Text == corrected, "视频生成提交失败后保留字幕并允许重试");
                rejectRender = false;
                Field<StackPanel>("ToastHost").Children.Clear();
                render.RaiseEvent(new RoutedEventArgs(Button.ClickEvent));
                await Until(() => Field<FrameworkElement>("ModalOverlay").Visibility == Visibility.Collapsed);
                var submitted = requests.Last(request => request.Path.EndsWith("/rerender"));
                using var payload = System.Text.Json.JsonDocument.Parse(submitted.Body);
                Check(payload.RootElement.GetProperty("artifact_id").GetInt32() == 102 &&
                      payload.RootElement.GetProperty("background").GetBoolean(), "生成请求准确指定已编辑双语字幕并在后台执行");
                Check(Field<FrameworkElement>("QueueView").Visibility == Visibility.Visible, "提交后进入传输队列查看生成进度");
                await Until(() => window.QueueTasks.Count == 1 && window.QueueTasks[0].Progress.Stage == "render");
                Check(window.QueueTasks[0].Status == "running" &&
                      !Descendants(Field<StackPanel>("ToastHost")).OfType<TextBlock>().Any(block => block.Text == "操作失败"), "生成任务刷新为进行中，进度读取没有错误");
                Check(requests.Where(request => request.Method == "PUT").All(request =>
                      System.Text.Json.JsonSerializer.Deserialize<Dictionary<string, string>>(request.Body)!["text"] == corrected), "所有保存请求均包含修正后的文字");
            }
            finally
            {
                releaseSave.TrySetResult();
                stop.Cancel();
                listener.Stop();
                await serverTask;
                port.SetValue(engine, null);
                Field<StackPanel>("ToastHost").Children.Clear();
            }

            async Task Until(Func<bool> ready)
            {
                var deadline = DateTime.UtcNow.AddSeconds(3);
                while (!ready() && DateTime.UtcNow < deadline) await Task.Delay(10);
                if (!ready()) throw new TimeoutException("字幕操作未在规定时间内完成");
            }

            async Task ServeSubtitleRequests()
            {
                try
                {
                    while (!stop.IsCancellationRequested)
                    {
                        using var client = await listener.AcceptTcpClientAsync(stop.Token);
                        using var stream = client.GetStream();
                        var headerBytes = new List<byte>();
                        var next = new byte[1];
                        while (await stream.ReadAsync(next, stop.Token) > 0)
                        {
                            headerBytes.Add(next[0]);
                            if (headerBytes.Count >= 4 && headerBytes.TakeLast(4).SequenceEqual(new byte[] { 13, 10, 13, 10 })) break;
                            if (headerBytes.Count > 16384) throw new IOException("请求头超过测试上限");
                        }
                        var headers = Encoding.ASCII.GetString(headerBytes.ToArray()).Split("\r\n");
                        var request = headers[0].Split(' ');
                        if (request.Length < 2) continue;
                        var length = headers.Where(header => header.StartsWith("Content-Length:", StringComparison.OrdinalIgnoreCase))
                            .Select(header => int.Parse(header.Split(':')[1].Trim())).FirstOrDefault();
                        var input = new byte[length];
                        await stream.ReadExactlyAsync(input, stop.Token);
                        if (headers.Any(header => header.Contains("Transfer-Encoding:", StringComparison.OrdinalIgnoreCase) && header.Contains("chunked")))
                        {
                            using var chunks = new MemoryStream();
                            while (true)
                            {
                                var count = Convert.ToInt32((await ReadLine(stream)).Split(';')[0], 16);
                                if (count == 0) { await ReadLine(stream); break; }
                                var chunk = new byte[count];
                                await stream.ReadExactlyAsync(chunk, stop.Token);
                                chunks.Write(chunk);
                                await ReadLine(stream);
                            }
                            input = chunks.ToArray();
                        }
                        requests.Enqueue((request[0], request[1], Encoding.UTF8.GetString(input)));
                        var fixture = new
                        {
                            id = "edit-test", title = "下载完成后修正字幕", status = renderAccepted ? "running" : "completed", subtitle_mode = "burn",
                            artifacts = new[] { new { id = 101, kind = "subtitle", lang = "en", path = "video.en.srt" },
                                                new { id = 102, kind = "bilingual", lang = "zh+en", path = "video.zh+en.srt" } },
                            progress = new { stage = renderAccepted ? "render" : "", fraction = .4 }
                        };
                        var status = "200 OK";
                        var body = request[0] == "GET" && request[1].EndsWith("/content")
                            ? System.Text.Json.JsonSerializer.Serialize(new { text = originalText })
                            : request[1] == "/api/v1/tasks" ? System.Text.Json.JsonSerializer.Serialize(new[] { fixture })
                            : request[1] == "/api/v1/tasks/edit-test" ? System.Text.Json.JsonSerializer.Serialize(fixture)
                            : "{\"ok\":true,\"status\":\"running\"}";
                        if (request[0] == "PUT")
                        {
                            saveReached.TrySetResult();
                            await releaseSave.Task.WaitAsync(stop.Token);
                            if (rejectSave) { status = "422 Unprocessable Entity"; body = "{\"detail\":\"测试字幕格式错误\"}"; }
                        }
                        if (request[1].EndsWith("/rerender") && rejectRender)
                        {
                            status = "503 Service Unavailable";
                            body = "{\"detail\":\"测试生成提交失败\"}";
                        }
                        else if (request[1].EndsWith("/rerender")) renderAccepted = true;
                        var bytes = Encoding.UTF8.GetBytes(body);
                        await stream.WriteAsync(Encoding.ASCII.GetBytes($"HTTP/1.1 {status}\r\nContent-Type: application/json\r\nContent-Length: {bytes.Length}\r\nConnection: close\r\n\r\n"), stop.Token);
                        await stream.WriteAsync(bytes, stop.Token);
                    }
                }
                catch (OperationCanceledException) when (stop.IsCancellationRequested) { }
                catch (SocketException) when (stop.IsCancellationRequested) { }
                catch (IOException) when (stop.IsCancellationRequested) { }
            }
            async Task<string> ReadLine(NetworkStream stream)
            {
                var line = new List<byte>();
                var value = new byte[1];
                while (await stream.ReadAsync(value, stop.Token) > 0)
                {
                    if (value[0] == 10) return Encoding.ASCII.GetString(line.ToArray()).TrimEnd('\r');
                    line.Add(value[0]);
                    if (line.Count > 8192) throw new IOException("分块请求头超过测试上限");
                }
                throw new IOException("请求未完整发送");
            }
        }
        async Task CheckWebsiteSessions()
        {
            var output = Environment.GetEnvironmentVariable("JIYING_WEBSITE_OUTPUT")
                ?? throw new InvalidOperationException("Website tests require an isolated output directory");
            Directory.CreateDirectory(output);
            Environment.SetEnvironmentVariable("JIYING_PLATFORM_PROFILE", Path.Combine(output, "profile"));
            var urls = new[] {
                ("xiaohongshu", "https://www.xiaohongshu.com/explore/6a9cc05b000000001103099d?xsec_token=AB45e8lRoH6lEGwp6MzFZWa9TtmNTmodjdEutsY5fWKfU=&xsec_source=pc_feed"),
                ("douyin", "https://www.douyin.com/video/6961737553342991651") };
            foreach (var pair in urls)
            {
                if (Environment.GetEnvironmentVariable("JIYING_PLATFORM_ONLY") is string only && only != pair.Item1) continue;
                if (pair.Item1 == "xiaohongshu")
                { await XiaohongshuWebsiteRegression.Run(window, pair.Item2, output, Check); continue; }
                if (pair.Item1 == "douyin")
                { await DouyinWebsiteRegression.Run(window, output, Check); continue; }
            }
        }

        void CheckPlatformLogin()
        {
            var input = Field<TextBox>("UrlBox");
            var previous = input.Text;
            foreach (var pair in new[] { ("https://www.douyin.com/?recommend=1", "抖音"),
                         ("https://xhslink.com/o/sample", "小红书") })
            {
                input.Text = pair.Item1;
                window.UpdateLayout();
                Check(Field<Border>("BilibiliLoginBar").Visibility == Visibility.Visible,
                      pair.Item2 + "解析页提供官网连接入口");
                Check(window.FindName("ConnectPlatformButton") is Button { Visibility: Visibility.Visible } button
                      && button.Content.ToString()!.Contains(pair.Item2), pair.Item2 + "连接按钮标明目标平台");
                Check(Field<Button>("ConnectBilibiliButton").Visibility == Visibility.Collapsed, "其他平台不显示 B 站扫码");
            }
            input.Text = "https://www.bilibili.com/video/BV1MP4y1m7Ci";
            Check(Field<Button>("ConnectBilibiliButton").Visibility == Visibility.Visible, "保留现有 B 站扫码入口");
            input.Text = "https://douyin.com.example.test/video/123";
            Check(Field<Border>("BilibiliLoginBar").Visibility == Visibility.Collapsed, "同名外站不显示平台连接入口");
            input.Text = previous;
            Check(PlatformConnectionWindow.SelectedVideoUrl("douyin", "https://www.douyin.com/?recommend=1", "") == "",
                  "推荐页必须由用户选择具体视频");
            Check(PlatformConnectionWindow.SelectedVideoUrl("douyin", "https://www.douyin.com/?modal_id=12345", "").Contains("modal_id=12345"),
                  "官网选中的抖音视频保留 ID");
            var xhs = "https://www.xiaohongshu.com/explore/6411cf99000000001300b6d9?xsec_token=A%2BB%3D";
            Check(PlatformConnectionWindow.SelectedVideoUrl("xiaohongshu", xhs, "") == xhs,
                  "小红书官网视频保留完整分享令牌");
            var profileNote = "https://www.xiaohongshu.com/user/profile/63439913000000001901f49a/674051740000000007027a15?xsec_token=A%2BB%3D";
            Check(PlatformConnectionWindow.SelectedVideoUrl("xiaohongshu", profileNote, "") == profileNote,
                  "个人主页内打开的视频保留笔记和完整参数");
            var featured = "https://jingxuan.douyin.com/m/video/7672218225355062409";
            Check(PlatformConnectionWindow.PlatformForUrl(featured) == "douyin" &&
                  PlatformConnectionWindow.SelectedVideoUrl("douyin", featured, "") == featured,
                  "抖音精选入口与官网选片识别一致");
            Check(PlatformConnectionWindow.PlatformForUrl("https://open.douyin.com/player/video?vid=12345") == "douyin",
                  "抖音官方嵌入链接识别为抖音");
        }

        void CheckDefaultOutputPaths()
        {
            var resolve = settingsType.GetMethod("ResolveDefaultOutputDirectory", BindingFlags.Static | BindingFlags.NonPublic);
            var normalize = settingsType.GetMethod("NormalizeOutputDirectory", BindingFlags.Static | BindingFlags.NonPublic);
            Check(resolve is not null && normalize is not null, "Default output policy can handle computers with and without D drive");
            if (resolve is null || normalize is null) return;
            var videos = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.MyVideos), "即影");
            var dPath = @"D:\即影\Downloads";
            Check((string)resolve.Invoke(null, [true])! == dPath && (string)resolve.Invoke(null, [false])! == videos,
                  "D drive is preferred and missing D drive falls back to user Videos");
            Check((string)normalize.Invoke(null, ["", true])! == dPath && (string)normalize.Invoke(null, ["", false])! == videos,
                  "Empty paths use the available drive default");
            Check((string)normalize.Invoke(null, [videos, true])! == dPath,
                  "The old automatic Videos default migrates to D drive");
            Check((string)normalize.Invoke(null, [dPath, true])! == dPath && (string)normalize.Invoke(null, [dPath, false])! == videos,
                  "Uncreated D download directory is retained and missing D drive falls back");
            Check((string)normalize.Invoke(null, [@"C:\MyDownloads", true])! == @"C:\MyDownloads",
                  "Manually selected download directories are preserved");
        }

        async Task CheckSettingsUx()
        {
            CheckDefaultOutputPaths();
            Call("ShowView", "settings");
            await Task.Delay(320);
            window.UpdateLayout();
            foreach (var name in new[] { "SpeechSettings", "AdvancedSettings" })
            {
                var group = Field<Expander>(name);
                group.ApplyTemplate();
                var header = group.Template.FindName("HeaderSite", group) as ToggleButton;
                Check(header is not null && header.ActualWidth >= group.ActualWidth - 4 && header.ActualHeight >= 44,
                      name + " has a full-width accessible disclosure header");
                if (header is null) continue;
                typeof(ToggleButton).GetMethod("OnClick", BindingFlags.Instance | BindingFlags.NonPublic)!.Invoke(header, null);
                Check(group.IsExpanded, name + " opens by clicking its header");
                typeof(ToggleButton).GetMethod("OnClick", BindingFlags.Instance | BindingFlags.NonPublic)!.Invoke(header, null);
                Check(!group.IsExpanded, name + " closes by clicking its header");
                Check(header.Focus() && header.IsKeyboardFocused, name + " supports keyboard focus");
                var peer = new System.Windows.Automation.Peers.ToggleButtonAutomationPeer(header);
                var toggle = (System.Windows.Automation.Provider.IToggleProvider)peer.GetPattern(System.Windows.Automation.Peers.PatternInterface.Toggle)!;
                toggle.Toggle();
                Check(group.IsExpanded, name + " opens through accessibility toggle");
                toggle.Toggle();
                Check(!group.IsExpanded, name + " closes through accessibility toggle");
            }
            Check(window.FindName("SpeechSettings") is Expander { IsExpanded: false }, "语音与翻译默认折叠");
            Check(window.FindName("GpuAdvancedSettings") is Expander { IsExpanded: false }, "可选 GPU 加速默认折叠");
            Check(window.FindName("SaveSettingsButton") is Button, "设置提供明确的保存和重试入口");
            var engine = typeof(MainWindow).GetField("_engine", BindingFlags.Instance | BindingFlags.NonPublic)!.GetValue(window)!;
            var port = engine.GetType().GetProperty("Port")!;
            var loading = typeof(MainWindow).GetField("_loadingControls", BindingFlags.Instance | BindingFlags.NonPublic)!;
            loading.SetValue(window, true);
            Field<TextBox>("AsrBaseSettingsBox").Text = "https://saved.example/asr";
            Field<PasswordBox>("AsrKeySettingsBox").Password = "test-key";
            Field<ComboBox>("WhisperSettingsBox").SelectedValue = "base";
            Call("SyncDependentControls", false);
            Check(Field<TextBlock>("SpeechSettingsSummary").Text.Contains("base"), "折叠标题显示当前识别模型");
            loading.SetValue(window, false);
            foreach (var success in new[] { false, true })
            {
                var listener = new TcpListener(IPAddress.Loopback, 0);
                listener.Start();
                using var stop = new CancellationTokenSource(TimeSpan.FromSeconds(6));
                var reached = new TaskCompletionSource<string>(TaskCreationOptions.RunContinuationsAsynchronously);
                var release = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
                var server = Task.Run(async () =>
                {
                    using var client = await listener.AcceptTcpClientAsync(stop.Token);
                    using var stream = client.GetStream();
                    using var reader = new StreamReader(stream, Encoding.UTF8, false, 1024, true);
                    var request = await reader.ReadLineAsync(stop.Token);
                    var length = 0;
                    var chunked = false;
                    string? header;
                    while (!string.IsNullOrEmpty(header = await reader.ReadLineAsync(stop.Token)))
                    {
                        if (header.StartsWith("Content-Length:", StringComparison.OrdinalIgnoreCase)) length = int.Parse(header.Split(':')[1].Trim());
                        if (header.StartsWith("Transfer-Encoding:", StringComparison.OrdinalIgnoreCase)) chunked = header.Contains("chunked");
                    }
                    var content = new StringBuilder();
                    do
                    {
                        if (chunked) length = Convert.ToInt32(await reader.ReadLineAsync(stop.Token), 16);
                        if (length == 0) break;
                        var chars = new char[length];
                        await reader.ReadBlockAsync(chars.AsMemory(), stop.Token);
                        content.Append(chars);
                        if (chunked) await reader.ReadLineAsync(stop.Token);
                    } while (chunked);
                    if (request != "POST /api/v1/config HTTP/1.1") throw new Exception("Unexpected settings request");
                    reached.SetResult(content.ToString());
                    await release.Task.WaitAsync(stop.Token);
                    var body = Encoding.UTF8.GetBytes(success ? "{}" : "{\"detail\":\"test failure\"}");
                    var status = success ? "200 OK" : "503 Service Unavailable";
                    await stream.WriteAsync(Encoding.ASCII.GetBytes($"HTTP/1.1 {status}\r\nContent-Type: application/json\r\nContent-Length: {body.Length}\r\nConnection: close\r\n\r\n"), stop.Token);
                    await stream.WriteAsync(body, stop.Token);
                });
                try
                {
                    port.SetValue(engine, ((IPEndPoint)listener.LocalEndpoint).Port);
                    var save = (Task<bool>)typeof(MainWindow).GetMethod("SaveEngineConfigAsync", BindingFlags.Instance | BindingFlags.NonPublic)!.Invoke(window, [false])!;
                    var payload = await reached.Task.WaitAsync(stop.Token);
                    Check(Field<TextBlock>("SettingsSaveStatus").Text.Contains("正在保存"), "请求完成前显示正在保存");
                    Check(payload.Contains("saved.example/asr") && payload.Contains("test-key"), "折叠设置仍提交接口与凭证");
                    Check(!payload.Contains("\"cookies\"") && !payload.Contains("cookies_from_browser"), "普通设置保存不提交登录字段，保留扫码会话");
                    release.SetResult();
                    Check(await save == success, "保存返回服务端的实际结果");
                    Check(success ? Field<TextBlock>("SettingsSaveStatus").Text.Contains("已保存") && Field<PasswordBox>("AsrKeySettingsBox").Password == "" :
                        Field<TextBlock>("SettingsSaveStatus").Text.Contains("保存失败") && Field<PasswordBox>("AsrKeySettingsBox").Password == "test-key", "成功清空凭证输入，失败保留修改并提示重试");
                }
                finally
                {
                    release.TrySetResult();
                    stop.Cancel();
                    listener.Stop();
                    await server;
                    port.SetValue(engine, null);
                    Field<StackPanel>("ToastHost").Children.Clear();
                }
            }
        }

        async Task CheckLoginConfigRecovery()
        {
            var engine = typeof(MainWindow).GetField("_engine", BindingFlags.Instance | BindingFlags.NonPublic)!.GetValue(window)!;
            var port = engine.GetType().GetProperty("Port")!;
            var loginStop = typeof(MainWindow).GetField("_bilibiliLoginStop", BindingFlags.Instance | BindingFlags.NonPublic)!;
            var image = new PngBitmapEncoder();
            image.Frames.Add(BitmapFrame.Create(BitmapSource.Create(1, 1, 96, 96, PixelFormats.Bgra32, null, new byte[] { 0, 0, 0, 255 }, 4)));
            using var png = new MemoryStream();
            image.Save(png);
            var startResponse = System.Text.Json.JsonSerializer.Serialize(new { session_id = "test-login", image_base64 = Convert.ToBase64String(png.ToArray()), status = "pending", message = "等待扫码" });
            foreach (var cancelled in new[] { false, true })
            {
                using var serverStop = new CancellationTokenSource();
                var listener = new TcpListener(IPAddress.Loopback, 0);
                listener.Start();
                var requests = new System.Collections.Concurrent.ConcurrentQueue<string>();
                var configReached = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
                var releaseConfig = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
                var serverTask = Task.Run(ServeLoginConfig);
                try
                {
                    port.SetValue(engine, ((IPEndPoint)listener.LocalEndpoint).Port);
                    Field<TextBox>("UrlBox").Text = "https://www.bilibili.com/video/test-login-config";
                    Call("ConnectBilibili_Click", Field<Button>("ConnectBilibiliButton"), new RoutedEventArgs());
                    await configReached.Task.WaitAsync(TimeSpan.FromSeconds(6));
                    if (cancelled)
                    {
                        Call("ConnectBilibili_Click", Field<Button>("ConnectBilibiliButton"), new RoutedEventArgs());
                        await Task.Delay(250);
                        Check(loginStop.GetValue(window) is null && Field<Button>("ConnectBilibiliButton").Content?.ToString() == "连接 B 站",
                              "扫码成功后取消配置读取可立即恢复连接按钮");
                    }
                    releaseConfig.TrySetResult();
                    for (var i = 0; i < 100 && loginStop.GetValue(window) is not null; i++) await Task.Delay(20);
                    Check(!requests.Any(request => request.Contains("/probe") || request == "POST /api/v1/config"),
                          cancelled ? "取消配置读取后不重探或写入旧配置" : "配置读取失败后不重探或写入旧配置");
                }
                finally
                {
                    releaseConfig.TrySetResult();
                    serverStop.Cancel();
                    listener.Stop();
                    await serverTask;
                    port.SetValue(engine, null);
                    Field<TextBox>("UrlBox").Text = "";
                    Field<StackPanel>("ToastHost").Children.Clear();
                }

                async Task ServeLoginConfig()
                {
                    try
                    {
                        while (!serverStop.IsCancellationRequested)
                        {
                            using var client = await listener.AcceptTcpClientAsync(serverStop.Token);
                            using var stream = client.GetStream();
                            using var reader = new StreamReader(stream, Encoding.UTF8, false, 1024, leaveOpen: true);
                            var request = (await reader.ReadLineAsync(serverStop.Token))?.Split(' ');
                            if (request is null || request.Length < 2) continue;
                            var length = 0;
                            string? header;
                            while (!string.IsNullOrEmpty(header = await reader.ReadLineAsync(serverStop.Token)))
                                if (header.StartsWith("Content-Length:", StringComparison.OrdinalIgnoreCase))
                                    length = int.Parse(header.Split(':')[1].Trim());
                            if (length > 0) await reader.ReadBlockAsync(new char[length].AsMemory(), serverStop.Token);
                            requests.Enqueue(request[0] + " " + request[1]);
                            var status = "200 OK";
                            var body = request[1].EndsWith("/start") ? startResponse :
                                request[1].EndsWith("/poll") ? """{"status":"connected","message":"测试连接已确认"}""" :
                                """{"engine":"ytdlp","title":"test","formats":[{"format_id":"test","label":"1080p"}]}""";
                            if (request[0] == "GET" && request[1] == "/api/v1/config")
                            {
                                configReached.TrySetResult();
                                if (cancelled) await releaseConfig.Task.WaitAsync(serverStop.Token);
                                status = "503 Service Unavailable";
                                body = """{"detail":{"message":"测试配置读取失败"}}""";
                            }
                            var payload = Encoding.UTF8.GetBytes(body);
                            var response = Encoding.ASCII.GetBytes($"HTTP/1.1 {status}\r\nContent-Type: application/json\r\nContent-Length: {payload.Length}\r\nConnection: close\r\n\r\n");
                            await stream.WriteAsync(response, serverStop.Token);
                            await stream.WriteAsync(payload, serverStop.Token);
                        }
                    }
                    catch (OperationCanceledException) when (serverStop.IsCancellationRequested) { }
                    catch (SocketException) when (serverStop.IsCancellationRequested) { }
                    catch (IOException) when (cancelled || serverStop.IsCancellationRequested) { }
                }
            }
        }
        async Task CheckWhisperPreflight()
        {
            var engine = typeof(MainWindow).GetField("_engine", BindingFlags.Instance | BindingFlags.NonPublic)!.GetValue(window)!;
            var port = engine.GetType().GetProperty("Port")!;
            var loading = typeof(MainWindow).GetField("_loadingControls", BindingFlags.Instance | BindingFlags.NonPublic)!;
            var originalRecognize = Field<CheckBox>("RecognizeCheck").IsChecked;
            var provider = Field<ComboBox>("AsrProviderParseBox");
            var originalProvider = provider.SelectedValue;
            loading.SetValue(window, true);
            try
            {
                foreach (var item in new[]
                {
                    (false, "faster-whisper", "missing", false, true),
                    (true, "cloud", "missing", false, true),
                    (true, "faster-whisper", "missing", false, false),
                    (true, "faster-whisper", "downloading", false, false),
                    (true, "faster-whisper", "ready", false, false),
                    (true, "faster-whisper", "downloaded", true, true)
                })
                {
                    Field<CheckBox>("RecognizeCheck").IsChecked = item.Item1;
                    provider.SelectedValue = item.Item2;
                    var requiresModel = item.Item1 && item.Item2 != "cloud";
                    var listener = new TcpListener(IPAddress.Loopback, 0);
                    listener.Start();
                    using var timeout = new CancellationTokenSource(TimeSpan.FromSeconds(4));
                    port.SetValue(engine, ((IPEndPoint)listener.LocalEndpoint).Port);
                    var response = requiresModel ? Task.Run(async () =>
                    {
                        using var client = await listener.AcceptTcpClientAsync(timeout.Token);
                        using var stream = client.GetStream();
                        using var reader = new StreamReader(stream, Encoding.UTF8, false, 1024, true);
                        var request = await reader.ReadLineAsync(timeout.Token);
                        string? header;
                        while (!string.IsNullOrEmpty(header = await reader.ReadLineAsync(timeout.Token))) { }
                        if (request?.Contains("/api/v1/whisper/status") != true) throw new Exception("Unexpected model preflight request");
                        var body = Encoding.UTF8.GetBytes(System.Text.Json.JsonSerializer.Serialize(new { model = "small", status = item.Item3, downloaded = item.Item4 }));
                        await stream.WriteAsync(Encoding.ASCII.GetBytes($"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {body.Length}\r\nConnection: close\r\n\r\n"), timeout.Token);
                        await stream.WriteAsync(body, timeout.Token);
                    }) : Task.CompletedTask;
                    try
                    {
                        var ready = await (Task<bool>)typeof(MainWindow).GetMethod("EnsureWhisperReadyAsync", BindingFlags.Instance | BindingFlags.NonPublic)!.Invoke(window, null)!;
                        await response;
                        Check(ready == item.Item5 && (requiresModel || !listener.Pending()), $"Whisper提交检查 {item.Item1}/{item.Item2}/{item.Item3}");
                        if (!ready) Check(Field<FrameworkElement>("WhisperPrompt").Visibility == Visibility.Visible, "缺模型或正在下载时提供就地准备提醒");
                        if (!item.Item4 && item.Item3 == "ready") Check(Field<Button>("PrepareWhisperButton").IsEnabled, "旧就绪状态不能屏蔽缺失模型的下载入口");
                    }
                    finally { listener.Stop(); }
                }
                var duplicateListener = new TcpListener(IPAddress.Loopback, 0);
                duplicateListener.Start();
                try
                {
                    port.SetValue(engine, ((IPEndPoint)duplicateListener.LocalEndpoint).Port);
                    var probeType = typeof(MainWindow).Assembly.GetType("Jiying.Desktop.ProbeResult")!;
                    typeof(MainWindow).GetField("_probe", BindingFlags.Instance | BindingFlags.NonPublic)!.SetValue(window, Activator.CreateInstance(probeType));
                    window.ProbeFormats.Add(new FormatInfo());
                    Field<ComboBox>("FormatList").SelectedIndex = 0;
                    typeof(MainWindow).GetField("_submitting", BindingFlags.Instance | BindingFlags.NonPublic)!.SetValue(window, true);
                    Call("Submit_Click", Field<Button>("SubmitButton"), new RoutedEventArgs());
                    await Task.Delay(100);
                    Check(!duplicateListener.Pending(), "提交处理中再次触发开始不会发出第二个请求");
                }
                finally
                {
                    duplicateListener.Stop();
                    typeof(MainWindow).GetField("_submitting", BindingFlags.Instance | BindingFlags.NonPublic)!.SetValue(window, false);
                    typeof(MainWindow).GetField("_probe", BindingFlags.Instance | BindingFlags.NonPublic)!.SetValue(window, null);
                    window.ProbeFormats.Clear();
                }
            }
            finally
            {
                port.SetValue(engine, null);
                Field<CheckBox>("RecognizeCheck").IsChecked = originalRecognize;
                provider.SelectedValue = originalProvider;
                typeof(MainWindow).GetField("_whisperStatus", BindingFlags.Instance | BindingFlags.NonPublic)!.SetValue(window, null);
                Field<FrameworkElement>("WhisperPrompt").Visibility = Visibility.Collapsed;
                Field<StackPanel>("ToastHost").Children.Clear();
                loading.SetValue(window, false);
                Call("SyncWhisperPrompt");
            }
        }
        void Check(bool condition, string message) { Console.WriteLine((condition ? "PASS " : "FAIL ") + message); if (!condition) failures.Add(message); }
        IEnumerable<DependencyObject> Descendants(DependencyObject parent)
        {
            for (var i = 0; i < VisualTreeHelper.GetChildrenCount(parent); i++)
            {
                var child = VisualTreeHelper.GetChild(parent, i);
                yield return child;
                foreach (var descendant in Descendants(child)) yield return descendant;
            }
        }
        void InspectVisual(Visual visual)
        {
            var drawing = VisualTreeHelper.GetDrawing(visual);
            if (drawing != null) InspectDrawing(drawing);
            for (var i = 0; i < VisualTreeHelper.GetChildrenCount(visual); i++)
                if (VisualTreeHelper.GetChild(visual, i) is Visual child) InspectVisual(child);
        }
        void InspectDrawing(Drawing drawing)
        {
            if (drawing is GlyphRunDrawing glyph) glyphFonts.Add(glyph.GlyphRun.GlyphTypeface.FontUri.ToString());
            if (drawing is DrawingGroup group) foreach (var child in group.Children) InspectDrawing(child);
        }
    }
}
