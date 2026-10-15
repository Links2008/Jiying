using System.Collections.ObjectModel;
using System.Collections.Concurrent;
using System.IO;
using System.Net.Http;
using System.Runtime.InteropServices;
using System.Text.RegularExpressions;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Controls.Primitives;
using System.Windows.Input;
using System.Windows.Interop;
using System.Windows.Media;
using System.Windows.Media.Animation;
using System.Windows.Media.Effects;
using System.Windows.Media.Imaging;
using Microsoft.Win32;

namespace Jiying.Desktop;

public partial class MainWindow : Window
{
    [StructLayout(LayoutKind.Sequential)]
    private struct AccentPolicy
    {
        public int State;
        public int Flags;
        public int GradientColor;
        public int AnimationId;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct CompositionAttributeData
    {
        public int Attribute;
        public nint Data;
        public int SizeOfData;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct Margins
    {
        public int Left;
        public int Right;
        public int Top;
        public int Bottom;
    }

    [DllImport("user32.dll")]
    private static extern int SetWindowCompositionAttribute(nint hwnd, ref CompositionAttributeData data);

    [DllImport("dwmapi.dll")]
    private static extern int DwmExtendFrameIntoClientArea(nint hwnd, ref Margins margins);

    [DllImport("dwmapi.dll")]
    private static extern int DwmSetWindowAttribute(nint hwnd, int attribute, ref int value, int size);


    private static readonly Dictionary<string, (string BaseUrl, string Model)> LlmPresets = new()
    {
        ["deepseek"] = ("https://api.deepseek.com/v1", "deepseek-chat"),
        ["openai"] = ("https://api.openai.com/v1", "gpt-4o-mini"),
        ["siliconflow"] = ("https://api.siliconflow.cn/v1", "deepseek-ai/DeepSeek-V3"),
        ["zhipu"] = ("https://open.bigmodel.cn/api/paas/v4", "glm-4-flash")
    };

    private static readonly Dictionary<string, (string BaseUrl, string Model)> AsrPresets = new()
    {
        ["openai"] = ("https://api.openai.com/v1", "whisper-1"),
        ["siliconflow"] = ("https://api.siliconflow.cn/v1", "FunAudioLLM/SenseVoiceSmall"),
        ["zhipu"] = ("https://open.bigmodel.cn/api/paas/v4", "glm-asr")
    };

    private static readonly HttpClient ThumbnailClient = new() { Timeout = TimeSpan.FromSeconds(15) };

    private readonly CancellationTokenSource _stop = new();
    private readonly SemaphoreSlim _refreshLock = new(1, 1);
    private readonly SemaphoreSlim _configSaveLock = new(1, 1);
    private readonly EngineClient _engine;
    private readonly UiSettings _ui = UiSettings.Load();
    private readonly HashSet<string> _selectedLanguages = ["zh"];
    private readonly ConcurrentDictionary<string, string> _knownStatuses = [];
    private readonly ConcurrentDictionary<string, TaskItem> _terminalDetails = [];
    private long _terminalDetailsRevision;
    private ProbeResult? _probe;
    private EngineConfig _config = new();
    private ArtifactItem? _editingArtifact;
    private string _subtitleSavedText = "";
    private bool _subtitleBusy;
    private int _subtitleOperationSequence;
    private int _deleteOperationSequence;
    private long _modalSequence;
    private CancellationTokenSource? _subtitleLoadStop;
    private int _subtitleLoadSequence;
    private TaskItem[] _deleteTasks = [];
    private Action<string>? _directorySelected;
    private CancellationTokenSource? _previewStop;
    private CancellationTokenSource? _whisperPollStop;
    private CancellationTokenSource? _gpuPollStop;
    private CancellationTokenSource? _probeStop;
    private CancellationTokenSource? _bilibiliLoginStop;
    private string _currentDirectory = "";
    private string _parentDirectory = "";
    private string _homeDirectory = "";
    private string _currentView = "parse";
    private int _refreshQueued;
    private int _refreshPending;
    private int _probeSequence;
    private bool _loadingControls = true;
    private bool _deleteBatch;
    private bool _hasLoadedTasks;
    private bool _submitting;
    private WhisperStatus? _whisperStatus;
    private bool _windowClosing;
    private bool _compactLayout;
    private int _viewAnimationSequence;
    private int _subtitleAnimationSequence;
    private bool _subtitleDragging;
    private BitmapSource? _darkArtwork;
    private BitmapSource? _lightArtwork;

    private static readonly IEasingFunction EaseOut = new CubicBezierEase(.23, 1, .32, 1);
    private static readonly IEasingFunction EaseInOut = new CubicBezierEase(.77, 0, .175, 1);
    private static readonly IEasingFunction CssEase = new CubicBezierEase(.25, .1, .25, 1);
    private static readonly IEasingFunction ResultEase = new CubicBezierEase(.2, 0, .2, 1);
    private static readonly IEasingFunction ResultForegroundEase = new CubicBezierEase(.19, 1, .22, 1);

    public ObservableCollection<FormatInfo> ProbeFormats { get; } = [];
    public ObservableCollection<TaskItem> QueueTasks { get; } = [];
    public ObservableCollection<TaskItem> DoneTasks { get; } = [];

    public MainWindow()
    {
        InitializeComponent();
        Closing += Window_Closing;
        var fontBase = new Uri("pack://application:,,,/Jiying.Native;component/Assets/Fonts/");
        Resources["AppFont"] = new FontFamily(fontBase, "./#Sora, PingFang SC, HarmonyOS Sans SC, MiSans, Microsoft YaHei");
        Resources["MonoFont"] = new FontFamily(fontBase, "./#JetBrains Mono, Consolas");
        // WebView2 applies Windows text scaling to the viewport as well as the fonts.
        var textScale = Math.Clamp(Convert.ToDouble(Registry.GetValue(
            @"HKEY_CURRENT_USER\Software\Microsoft\Accessibility", "TextScaleFactor", 100)) / 100, 1, 2.25);
        RootGrid.LayoutTransform = new ScaleTransform(textScale, textScale);
        ApplyScreenBlendArtwork();
        DataContext = this;
        _engine = new EngineClient(Path.Combine(AppContext.BaseDirectory, "engine", "JiyingEngine.exe"));
        BuildLanguageChoices();
        EnsureSavedFontChoice();
        ApplyUiSettings();
        ShowView("parse");
        _loadingControls = false;
    }

    private void ApplyScreenBlendArtwork()
    {
        if (BackgroundImage.Source is not BitmapSource source) return;
        var light = _ui.Theme == "light" || (_ui.Theme == "auto" && SystemUsesLightTheme());
        if (light) _lightArtwork = CreateBlendArtwork(source, 1.18, 1.06, true);
        else _darkArtwork = CreateBlendArtwork(source, 1.12, 1.04, false);
        BackgroundImage.Source = light ? _lightArtwork : _darkArtwork;
    }

    private static BitmapSource LoadBlendArtwork(bool light) => CreateBlendArtwork(
        new BitmapImage(new Uri("pack://application:,,,/Jiying.Native;component/Assets/etch-bg.jpg")),
        light ? 1.18 : 1.12, light ? 1.06 : 1.04, light);

    private static BitmapSource CreateBlendArtwork(BitmapSource source, double contrast, double brightness, bool darkInk)
    {
        var bitmap = new FormatConvertedBitmap(source, PixelFormats.Bgra32, null, 0);
        var stride = bitmap.PixelWidth * 4;
        var pixels = new byte[stride * bitmap.PixelHeight];
        bitmap.CopyPixels(pixels, stride, 0);
        for (var i = 0; i < pixels.Length; i += 4)
        {
            var luminance = (77 * pixels[i + 2] + 150 * pixels[i + 1] + 29 * pixels[i]) >> 8;
            var alpha = (byte)Math.Clamp((int)(((luminance - 128) * contrast + 128) * brightness), 0, 255);
            pixels[i] = pixels[i + 1] = pixels[i + 2] = darkInk ? (byte)0 : alpha;
            pixels[i + 3] = alpha;
        }
        var artwork = BitmapSource.Create(bitmap.PixelWidth, bitmap.PixelHeight, bitmap.DpiX, bitmap.DpiY,
            PixelFormats.Pbgra32, null, pixels, stride);
        artwork.Freeze();
        return artwork;
    }

    private void Window_SourceInitialized(object? sender, EventArgs e)
    {
        var hwnd = new WindowInteropHelper(this).Handle;
        if (HwndSource.FromHwnd(hwnd)?.CompositionTarget is { } target)
            target.BackgroundColor = Colors.Transparent;

        try
        {
            var margins = new Margins { Left = -1, Right = -1, Top = -1, Bottom = -1 };
            _ = DwmExtendFrameIntoClientArea(hwnd, ref margins);
            var rounded = 2;
            _ = DwmSetWindowAttribute(hwnd, 33, ref rounded, sizeof(int));
            ApplyNativeBackdrop();
        }
        catch (DllNotFoundException) { }
        catch (EntryPointNotFoundException) { }
    }

    private async void Window_Loaded(object sender, RoutedEventArgs e)
    {
        ApplyAppearance();
        UpdateWindowClip();
        UpdateResponsiveLayout();
        LayoutBackground();
        SetConnection(false, "正在连接引擎");
        try
        {
            if (await _engine.EnsureConnectedAsync(true, _stop.Token))
            {
                SetConnection(true, $"引擎已连接 · {_engine.Port}");
                await LoadConfigAsync();
                await RefreshYtDlpStatusAsync();
                await RefreshTasksAsync();
            }
            else
            {
                SetConnection(false, "引擎未连接");
                ShowError(string.IsNullOrWhiteSpace(_engine.ConnectionHint)
                    ? "本地视频引擎在 30 秒内没有就绪，界面会继续尝试重连。"
                    : _engine.ConnectionHint);
            }
        }
        catch (OperationCanceledException) when (_stop.IsCancellationRequested) { return; }
        catch (Exception ex)
        {
            SetConnection(false, "引擎连接失败");
            ShowError(ex.Message);
        }

        _ = Task.Run(() => PollLoopAsync(_stop.Token));
        _ = Task.Run(() => EventLoopAsync(_stop.Token));
    }

    private void LayoutBackground()
    {
        var width = Math.Max(1, RootGrid.ActualWidth);
        var height = Math.Max(1, RootGrid.ActualHeight);
        BackgroundImage.Height = height * 1.70;
        BackgroundImage.Width = Math.Max(width, height * 1.70 * 1.60);
    }

    private void ApplyNativeBackdrop()
    {
        var hwnd = new WindowInteropHelper(this).Handle;
        if (hwnd == nint.Zero) return;
        var light = _ui.Theme == "light" || (_ui.Theme == "auto" && SystemUsesLightTheme());
        var dark = light ? 0 : 1;
        _ = DwmSetWindowAttribute(hwnd, 20, ref dark, sizeof(int));
        var t = (Math.Clamp(_ui.BackgroundOpacity, 40, 100) - 40d) / 60d;
        var alpha = (byte)Math.Round((light ? 85 : 75) + (light ? 95 : 85) * t);
        var tint = ((uint)alpha << 24) | (light ? 0x00F8F6F4u : 0x0013100Du);
        var accent = new AccentPolicy { State = 4, Flags = 2, GradientColor = unchecked((int)tint) };
        var pointer = Marshal.AllocHGlobal(Marshal.SizeOf<AccentPolicy>());
        try
        {
            Marshal.StructureToPtr(accent, pointer, false);
            var data = new CompositionAttributeData { Attribute = 19, Data = pointer, SizeOfData = Marshal.SizeOf<AccentPolicy>() };
            _ = SetWindowCompositionAttribute(hwnd, ref data);
        }
        finally { Marshal.FreeHGlobal(pointer); }
    }

    private async Task PollLoopAsync(CancellationToken token)
    {
        var loadedConfig = _engine.IsConnected;
        while (!token.IsCancellationRequested)
        {
            var connectionVersion = _engine.ConnectionVersion;
            try
            {
                var wasConnected = _engine.IsConnected;
                if (await _engine.EnsureConnectedAsync(true, token))
                {
                    connectionVersion = _engine.ConnectionVersion;
                    SetConnection(true, $"引擎已连接 · {_engine.Port}");
                    if (!wasConnected || !loadedConfig)
                    {
                        await LoadConfigAsync();
                        loadedConfig = true;
                    }
                    await RefreshTasksAsync();
                }
                else
                {
                    loadedConfig = false;
                    SetConnection(false, string.IsNullOrWhiteSpace(_engine.ConnectionHint) ? "等待本地引擎" : "引擎未连接");
                }
            }
            catch (OperationCanceledException) when (token.IsCancellationRequested) { return; }
            catch (Exception ex)
            {
                loadedConfig = false;
                if (_engine.Disconnect(connectionVersion))
                {
                    SetConnection(false, "引擎已断开");
                    ShowError(ex.Message);
                }
            }

            try { await Task.Delay(TimeSpan.FromSeconds(5), token); }
            catch (OperationCanceledException) { return; }
        }
    }

    private async Task EventLoopAsync(CancellationToken token)
    {
        while (!token.IsCancellationRequested)
        {
            if (!_engine.IsConnected)
            {
                try { await Task.Delay(1000, token); }
                catch (OperationCanceledException) { return; }
                continue;
            }

            var connectionVersion = _engine.ConnectionVersion;
            try { await _engine.ListenEventsAsync(() => { QueueRefresh(); return Task.CompletedTask; }, token); }
            catch (OperationCanceledException) when (token.IsCancellationRequested) { return; }
            catch
            {
                if (_engine.Disconnect(connectionVersion)) SetConnection(false, "事件流断开，正在重连");
                try { await Task.Delay(1000, token); }
                catch (OperationCanceledException) { return; }
            }
        }
    }

    private void QueueRefresh()
    {
        Interlocked.Exchange(ref _refreshPending, 1);
        if (Interlocked.CompareExchange(ref _refreshQueued, 1, 0) != 0) return;
        _ = Task.Run(async () =>
        {
            try
            {
                // 当前读取进行中收到的新通知仍需保留，结束后再合并读取一次。
                while (Interlocked.Exchange(ref _refreshPending, 0) == 1)
                {
                    await Task.Delay(500, _stop.Token);
                    await RefreshTasksAsync();
                }
            }
            catch (OperationCanceledException) { }
            finally
            {
                Interlocked.Exchange(ref _refreshQueued, 0);
                if (Volatile.Read(ref _refreshPending) != 0 && !_stop.IsCancellationRequested) QueueRefresh();
            }
        });
    }

    private async Task RefreshTasksAsync()
    {
        if (!_engine.IsConnected) return;
        if (!await _refreshLock.WaitAsync(0)) { QueueRefresh(); return; }
        try
        {
            var revision = Volatile.Read(ref _terminalDetailsRevision);
            var tasks = await _engine.GetTasksAsync(_stop.Token);
            var ids = tasks.Where(task => task.IsTerminal).Select(task => task.Id).ToHashSet();
            foreach (var stale in _terminalDetails.Keys.Where(id => !ids.Contains(id)).ToArray())
                _terminalDetails.TryRemove(stale, out _);

            foreach (var task in tasks.Where(task => task.IsTerminal))
            {
                if (!_terminalDetails.TryGetValue(task.Id, out var detail) ||
                    detail.Status != task.Status || detail.Error != task.Error || detail.FinishedAt != task.FinishedAt)
                {
                    try
                    {
                        detail = await _engine.GetTaskAsync(task.Id, _stop.Token);
                        lock (_terminalDetails)
                        {
                            // 保存、重生成或删除后，在途旧详情不能重新填入已失效的缓存。
                            if (revision != _terminalDetailsRevision) { QueueRefresh(); return; }
                            _terminalDetails[task.Id] = detail;
                        }
                    }
                    catch (OperationCanceledException) when (_stop.IsCancellationRequested) { throw; }
                    catch { detail = task; }
                }
                CopyTaskDetail(task, detail);
            }

            var queue = tasks.Where(task => !task.IsTerminal).ToArray();
            var done = tasks.Where(task => task.IsTerminal).OrderByDescending(task => task.CreatedAt).ToArray();
            var speed = queue.Sum(task => task.Progress.SpeedBps);
            await Dispatcher.InvokeAsync(() =>
            {
                if (_stop.IsCancellationRequested) return;
                if (revision != Volatile.Read(ref _terminalDetailsRevision)) { QueueRefresh(); return; }
                foreach (var task in tasks)
                {
                    foreach (var artifact in task.Artifacts) artifact.TaskId = task.Id;
                    if (_hasLoadedTasks && _ui.Notify && task.Status == "completed" &&
                        (!_knownStatuses.TryGetValue(task.Id, out var old) || old != "completed"))
                        NotifyCompleted(task);
                    _knownStatuses[task.Id] = task.Status;
                }
                _hasLoadedTasks = true;
                var visibleIds = tasks.Select(task => task.Id).ToHashSet();
                foreach (var stale in _knownStatuses.Keys.Where(id => !visibleIds.Contains(id)).ToArray())
                    _knownStatuses.TryRemove(stale, out _);
                SyncTasks(QueueTasks, queue);
                SyncTasks(DoneTasks, done);
                QueueEmpty.Visibility = queue.Length == 0 ? Visibility.Visible : Visibility.Collapsed;
                QueueList.Visibility = queue.Length == 0 ? Visibility.Collapsed : Visibility.Visible;
                DoneEmpty.Visibility = done.Length == 0 ? Visibility.Visible : Visibility.Collapsed;
                DoneList.Visibility = done.Length == 0 ? Visibility.Collapsed : Visibility.Visible;
                QueueBadge.Visibility = queue.Length == 0 ? Visibility.Collapsed : Visibility.Visible;
                DoneBadge.Visibility = done.Length == 0 ? Visibility.Collapsed : Visibility.Visible;
                QueueBadgeText.Text = queue.Length.ToString();
                DoneBadgeText.Text = done.Length.ToString();
                QueueCountText.Text = $"进行中 {queue.Count(task => task.Status == "running")} · 共 {queue.Length} 个任务";
                DoneCountText.Text = $"共 {done.Length} 条记录";
                NetSpeedText.Text = speed >= 1048576 ? $"{speed / 1048576d:0.0} MB/s" : $"{speed / 1024d:0} KB/s";
            });
        }
        catch (OperationCanceledException) when (_stop.IsCancellationRequested) { }
        catch (Exception ex) { ShowError(ex.Message); }
        finally { _refreshLock.Release(); }
    }

    private void InvalidateTaskDetails(string id)
    {
        lock (_terminalDetails)
        {
            _terminalDetailsRevision++;
            _terminalDetails.TryRemove(id, out _);
        }
    }

    private static void CopyTaskDetail(TaskItem target, TaskItem detail)
    {
        if (!string.IsNullOrWhiteSpace(detail.Title)) target.Title = detail.Title;
        if (!string.IsNullOrWhiteSpace(detail.OutputDirectory)) target.OutputDirectory = detail.OutputDirectory;
        if (!string.IsNullOrWhiteSpace(detail.SubtitleMode)) target.SubtitleMode = detail.SubtitleMode;
        if (detail.Artifacts.Count > 0) target.Artifacts = detail.Artifacts;
    }

    private static void Replace<T>(ObservableCollection<T> target, IEnumerable<T> items)
    {
        target.Clear();
        foreach (var item in items) target.Add(item);
    }

    private static void SyncTasks(ObservableCollection<TaskItem> target, IReadOnlyList<TaskItem> items)
    {
        var ids = items.Select(item => item.Id).ToHashSet();
        for (var i = target.Count - 1; i >= 0; i--)
            if (!ids.Contains(target[i].Id)) target.RemoveAt(i);
        for (var i = 0; i < items.Count; i++)
        {
            var existing = target.FirstOrDefault(item => item.Id == items[i].Id);
            if (existing is null) target.Insert(i, items[i]);
            else
            {
                var oldIndex = target.IndexOf(existing);
                if (oldIndex != i) target.Move(oldIndex, i);
                existing.RefreshFrom(items[i]);
            }
        }
    }

    private async void Probe_Click(object sender, RoutedEventArgs e)
    {
        if (_probeStop is not null) { _probeStop.Cancel(); return; }
        await ProbeAsync();
    }

    private async Task ProbeAsync(bool forceRefresh = false)
    {
        var url = NormalizeUrl(UrlBox.Text);
        if (!Uri.TryCreate(url, UriKind.Absolute, out var parsed) || parsed.Scheme is not ("http" or "https"))
        {
            ShowError("请输入完整的 http 或 https 视频链接。");
            UrlBox.Focus();
            return;
        }
        if (IsDouyinFeedUrl(parsed))
        {
            await ConnectPlatformAsync();
            return;
        }
        if (!_engine.IsConnected)
        {
            ShowError(string.IsNullOrWhiteSpace(_engine.ConnectionHint) ? "本地视频引擎尚未连接，正在自动重试。" : _engine.ConnectionHint);
            return;
        }

        UrlBox.Text = url;
        _probeStop?.Cancel();
        using var probeStop = CancellationTokenSource.CreateLinkedTokenSource(_stop.Token);
        _probeStop = probeStop;
        var sequence = Interlocked.Increment(ref _probeSequence);
        SetProbeBusy(true);
        ParseFailure.Visibility = Visibility.Collapsed;
        ProbePanel.Visibility = Visibility.Collapsed;
        ClearError();
        try
        {
            var probe = await _engine.ProbeAsync(url, probeStop.Token, forceRefresh);
            if (sequence != _probeSequence) return;
            _probe = probe;
            ProbeTitle.Text = string.IsNullOrWhiteSpace(probe.Title) ? "未命名视频" : probe.Title;
            var duration = probe.Duration > 0 ? " · 时长 " + TimeSpan.FromSeconds(probe.Duration).ToString(@"m\:ss") : "";
            ProbeMeta.Text = duration.TrimStart(' ', '·');
            ProbeQualityHint.Text = probe.QualityNotice;
            ProbeQualityHint.Visibility = string.IsNullOrWhiteSpace(probe.QualityNotice) ? Visibility.Collapsed : Visibility.Visible;
            if (IsBilibiliUrl(url))
                ParseAuthHint.Text = probe.AuthStatus == "loaded"
                    ? "已读取 B 站登录 Cookie；可下载画质以列表为准，4K 取决于视频与账号权限。"
                    : probe.AuthStatus == "unknown" ? "登录信息不可用，仍可下载列表中的公开画质；需要更高清时可重新连接。"
                    : "未登录也可下载当前列表中的画质；需要更高清时，连接 B 站后自动刷新。";
            Replace(ProbeFormats, probe.Formats.Count > 0 ? probe.Formats : [new FormatInfo { FormatId = "source", Label = "源画质" }]);
            FormatList.SelectedIndex = 0;
            LoadRemoteImage(ProbeThumbnail, probe.Thumbnail, sequence);
            ProbePanel.Visibility = Visibility.Visible;
            AnimateCard(ProbePanel);
            UpdateSubmitHint();
            QueueSubtitlePreview();
        }
        catch (OperationCanceledException) when (probeStop.IsCancellationRequested) { }
        catch (Exception ex)
        {
            if (sequence != _probeSequence) return;
            _probe = null;
            ParseFailureText.Text = ex.Message;
            ParseFailure.Visibility = Visibility.Visible;
            ShowError(ex.Message);
        }
        finally
        {
            if (ReferenceEquals(_probeStop, probeStop)) _probeStop = null;
            if (sequence == _probeSequence)
            {
                SetProbeBusy(false);
            }
        }
    }

    private void SetProbeBusy(bool busy)
    {
        UpdateParseLayout(busy || ProbePanel.Visibility == Visibility.Visible || ParseFailure.Visibility == Visibility.Visible);
        ProbeButton.IsEnabled = true;
        ProbeButtonLabel.Text = busy ? "取消" : "解析";
        ReadLoginButton.IsEnabled = RefreshQualityButton.IsEnabled = !busy;
        ParseAuthSourceBox.IsEnabled = BrowseCookiesButton.IsEnabled = !busy;
        ConnectPlatformButton.IsEnabled = !busy;
        ProbeButtonIcon.Visibility = busy ? Visibility.Collapsed : Visibility.Visible;
        ProbeSpinner.Visibility = busy ? Visibility.Visible : Visibility.Collapsed;
        ProbeSpinnerRotate.BeginAnimation(RotateTransform.AngleProperty, null);
        if (busy && SystemParameters.ClientAreaAnimation)
            ProbeSpinnerRotate.BeginAnimation(RotateTransform.AngleProperty,
                new DoubleAnimation(0, 360, TimeSpan.FromMilliseconds(700)) { RepeatBehavior = RepeatBehavior.Forever });
        ParseLoading.Visibility = busy ? Visibility.Visible : Visibility.Collapsed;
        UrlClipboardPath.SetResourceReference(System.Windows.Shapes.Shape.StrokeProperty, busy ? "AccentBrush" : "MutedBrush");
        UrlClipboardIcon.BeginAnimation(OpacityProperty, null);
        UrlClipboardIcon.Opacity = 1;
        if (busy && SystemParameters.ClientAreaAnimation)
            UrlClipboardIcon.BeginAnimation(OpacityProperty, new DoubleAnimation(1, .45, TimeSpan.FromMilliseconds(550))
            { AutoReverse = true, RepeatBehavior = RepeatBehavior.Forever });
    }

    private async void Submit_Click(object sender, RoutedEventArgs e)
    {
        if (_submitting) return;
        if (_probe is null || FormatList.SelectedItem is not FormatInfo format) return;
        var recognize = RecognizeCheck.IsChecked == true;
        var translate = TranslateCheck.IsChecked == true;
        var burn = recognize && BurnCheck.IsChecked == true;
        var color = SubtitleColorValue();
        if (burn && !Regex.IsMatch(color, "^#[0-9A-Fa-f]{6}$"))
        {
            ShowError("字幕颜色请输入 #RRGGBB 格式，例如 #FFFFFF。");
            SubtitleCustomColorBox.Focus();
            return;
        }
        if (translate && Selected(TranslationParseBox, "null") != "llm")
        {
            ShowError("请先选择翻译引擎。");
            return;
        }
        _submitting = true;
        SubmitButton.IsEnabled = false;
        SubmitButton.Content = "正在准备…";
        ClearError();
        try
        {
            if (!await SaveEngineConfigAsync(false)) return;
            if (!await EnsureWhisperReadyAsync()) return;
            SubmitButton.Content = "正在创建任务…";
            var task = await _engine.SubmitAsync(new SubmitTaskRequest
            {
                Url = UrlBox.Text.Trim(),
                Resolution = format.FormatId == "source" ? "best" : format.FormatId,
                Langs = translate ? _selectedLanguages.ToArray() : [],
                Recognize = recognize,
                Translate = translate,
                Burn = burn,
                Bilingual = burn && translate && BilingualCheck.IsChecked == true,
                OutputDirectory = OutputPathBox.Text.Trim(),
                FontName = Selected(FontNameBox),
                FontSize = IntValue(Selected(FontSizeBox), 18),
                Position = Selected(SubtitlePositionBox, "bottom"),
                SourceLanguage = Selected(SourceLanguageBox),
                Outline = (int)OutlineSlider.Value,
                Color = color,
                VerticalOffset = (int)VerticalOffsetSlider.Value,
                HorizontalOffset = _ui.SubtitleHorizontalOffset
            }, _stop.Token);
            ShowToast("任务已创建", task.DisplayTitle);
            ShowView("queue");
            await RefreshTasksAsync();
        }
        catch (OperationCanceledException) when (_stop.IsCancellationRequested) { }
        catch (Exception ex) { ShowError(ex.Message); }
        finally { _submitting = false; SubmitButton.IsEnabled = true; SubmitButton.Content = "开始下载"; }
    }

    private async Task<bool> EnsureWhisperReadyAsync()
    {
        if (RecognizeCheck.IsChecked != true || Selected(AsrProviderParseBox, "faster-whisper") == "cloud") return true;
        var status = await _engine.GetWhisperStatusAsync(Selected(WhisperParseBox, "small"), _stop.Token);
        ApplyWhisperStatus(status);
        if (status.Downloaded && status.Status is not ("downloading" or "loading")) return true;
        WhisperPrompt.Visibility = Visibility.Visible;
        ShowError(status.Status is "downloading" or "loading" ? "语音模型正在准备，完成后即可开始。" : "生成字幕需要先下载语音模型，点击“下载语音模型”即可准备。仅下载视频无需模型。");
        return false;
    }

    private async void Cancel_Click(object sender, RoutedEventArgs e) => await RunTaskAction(sender, _engine.CancelAsync, "已发送取消请求");
    private async void Retry_Click(object sender, RoutedEventArgs e) => await RunTaskAction(sender, _engine.RetryAsync, "任务已重新开始");
    private async void Reveal_Click(object sender, RoutedEventArgs e) => await RunTaskAction(sender, _engine.RevealAsync, "已打开输出目录");

    private async Task RunTaskAction(object sender, Func<string, CancellationToken, Task> action, string success)
    {
        if (sender is not Button { Tag: string id }) return;
        if (!((Button)sender).IsEnabled) return;
        var button = (Button)sender;
        button.IsEnabled = false;
        ClearError();
        try
        {
            await action(id, _stop.Token);
            InvalidateTaskDetails(id);
            ShowToast(success);
            await RefreshTasksAsync();
        }
        catch (OperationCanceledException) when (_stop.IsCancellationRequested) { }
        catch (Exception ex) { ShowError(ex.Message); }
        finally { button.IsEnabled = true; }
    }

    private async void Artifact_Click(object sender, RoutedEventArgs e)
    {
        if (sender is not Button { Tag: ArtifactItem artifact }) return;
        if (_subtitleBusy) return;
        if (!ConfirmDiscardSubtitleChanges()) return;
        CancelSubtitleLoad();
        if (artifact.IsSubtitle)
        {
            var sequence = _subtitleLoadSequence;
            using var loadStop = CancellationTokenSource.CreateLinkedTokenSource(_stop.Token);
            _subtitleLoadStop = loadStop;
            try
            {
                var content = await _engine.GetArtifactContentAsync(artifact.Id, loadStop.Token);
                // 连续选择字幕时，已取消或较早的响应不能替换当前编辑对象与文本。
                if (sequence != _subtitleLoadSequence || loadStop.IsCancellationRequested || _subtitleBusy) return;
                _editingArtifact = artifact;
                SubtitleDialogTitle.Text = $"修改字幕 · {artifact.DisplayText}";
                LoadSubtitleEditor(content);
                _subtitleSavedText = content.Text;
                SetSubtitleBusy(false, "修改后选择“保存并重新生成视频”，进度会显示在传输队列中。");
                OpenModal(SubtitleDialog);
                if (_subtitleDocument is null) SubtitleTextBox.Focus();
                else SubtitleRows.Focus();
            }
            catch (OperationCanceledException) when (loadStop.IsCancellationRequested) { }
            catch (Exception ex) { if (sequence == _subtitleLoadSequence) ShowError(ex.Message); }
            finally { if (ReferenceEquals(_subtitleLoadStop, loadStop)) _subtitleLoadStop = null; }
            return;
        }

        var dialog = new SaveFileDialog
        {
            FileName = string.IsNullOrWhiteSpace(artifact.FileName) ? $"artifact-{artifact.Id}" : artifact.FileName,
            Filter = "所有文件|*.*"
        };
        if (dialog.ShowDialog(this) != true) return;
        try
        {
            await _engine.SaveArtifactAsync(artifact.Id, dialog.FileName, _stop.Token);
            ShowToast("文件已保存", dialog.FileName);
        }
        catch (OperationCanceledException) when (_stop.IsCancellationRequested) { }
        catch (Exception ex) { ShowError(ex.Message); }
    }

    private async void SaveSubtitle_Click(object sender, RoutedEventArgs e)
    {
        if (_subtitleBusy || _editingArtifact is null) return;
        var operation = ++_subtitleOperationSequence;
        SetSubtitleBusy(true, "正在保存字幕…");
        try
        {
            if (await SaveSubtitleAsync()) ShowToast("字幕已保存", "可继续修改，或重新生成带字幕视频");
        }
        finally { if (operation == _subtitleOperationSequence) SetSubtitleBusy(false); }
    }

    private void SetSubtitleBusy(bool busy, string? message = null)
    {
        _subtitleBusy = busy;
        SubtitleTextBox.IsReadOnly = busy;
        SubtitleRows.IsEnabled = !busy;
        SubtitleSaveButton.IsEnabled = SubtitleRenderButton.IsEnabled = SubtitleCloseButton.IsEnabled = !busy;
        if (message is not null) SubtitleHint.Text = message;
    }

    private async Task<bool> SaveSubtitleAsync()
    {
        var artifact = _editingArtifact;
        if (artifact is null) return false;
        var text = SubtitleTextBox.Text;
        try
        {
            if (_subtitleDocument is not null) _subtitleDocument.Serialize(true);
            await _engine.SaveArtifactContentAsync(artifact.Id, text, _stop.Token);
            _subtitleSavedText = text;
            InvalidateTaskDetails(artifact.TaskId);
            SubtitleHint.Text = "字幕已保存；可继续修改，或重新生成视频。";
            return true;
        }
        catch (OperationCanceledException) when (_stop.IsCancellationRequested) { return false; }
        catch (Exception ex)
        {
            SubtitleHint.Text = $"保存失败：{ex.Message}";
            ShowError(ex.Message);
            return false;
        }
    }

    private async void RerenderSubtitle_Click(object sender, RoutedEventArgs e)
    {
        if (_editingArtifact is null || _subtitleBusy) return;
        var operation = ++_subtitleOperationSequence;
        var artifact = _editingArtifact;
        var submitted = false;
        SetSubtitleBusy(true, "正在保存字幕…");
        try
        {
            if (!await SaveSubtitleAsync()) return;
            SubtitleHint.Text = "字幕已保存，正在提交视频重生成…";
            await _engine.RerenderAsync(artifact.TaskId, artifact.Id, artifact.Language, _stop.Token);
            submitted = true;
            InvalidateTaskDetails(artifact.TaskId);
            SetSubtitleBusy(false);
            CloseModal();
            ShowToast("已开始重新生成视频", "使用已保存的字幕，进度会在传输队列中更新");
            ShowView("queue");
            await RefreshTasksAsync();
        }
        catch (OperationCanceledException) when (_stop.IsCancellationRequested) { }
        catch (Exception ex)
        {
            if (operation == _subtitleOperationSequence && _editingArtifact?.Id == artifact.Id &&
                _editingArtifact.TaskId == artifact.TaskId)
                SubtitleHint.Text = submitted ? "视频已提交，进度刷新失败。" : $"字幕已保存，视频重生成未启动：{ex.Message}";
            ShowError(ex.Message);
        }
        finally { if (operation == _subtitleOperationSequence) SetSubtitleBusy(false); }
    }

    private async void DeleteOne_Click(object sender, RoutedEventArgs e)
    {
        if (sender is not Button { Tag: TaskItem task }) return;
        if (task.Artifacts.Count == 0)
        {
            try { task = await _engine.GetTaskAsync(task.Id, _stop.Token); }
            catch { }
        }
        ShowDeleteDialog([task], false);
    }

    private void ClearDone_Click(object sender, RoutedEventArgs e)
    {
        if (DoneTasks.Count == 0) { ShowToast("没有可清理的记录"); return; }
        ShowDeleteDialog(DoneTasks.ToArray(), true);
    }

    private void ShowDeleteDialog(TaskItem[] tasks, bool batch)
    {
        _deleteTasks = tasks;
        _deleteBatch = batch;
        DeleteDialogTitle.Text = batch ? "清理已完成内容" : "删除下载内容";
        DeleteDescription.Text = batch
            ? $"选择清理范围 · 共 {tasks.Length} 条已完成记录"
            : $"“{tasks[0].DisplayTitle}”";
        DeleteAllChoice.IsChecked = true;
        DeleteChoice_Changed(DeleteAllChoice, new RoutedEventArgs());
        OpenModal(DeleteDialog);
        if (DeleteDialog.Visibility == Visibility.Visible) ConfirmDeleteButton.IsEnabled = true;
    }

    private void DeleteChoice_Changed(object sender, RoutedEventArgs e)
    {
        if (DeleteHint is null || ConfirmDeleteText is null) return;
        var scope = DeleteSubtitleChoice?.IsChecked == true ? "subtitle"
            : DeleteVideoChoice?.IsChecked == true ? "video" : "all";
        DeleteHint.Text = scope switch
        {
            "subtitle" => "保留视频和记录",
            "video" => "保留字幕和记录",
            _ => "同时删除文件和记录"
        };
        ConfirmDeleteText.Text = scope == "all" ? "删除全部" : "删除所选";
    }

    private async void ConfirmDelete_Click(object sender, RoutedEventArgs e)
    {
        if (!ConfirmDeleteButton.IsEnabled) return;
        var operation = ++_deleteOperationSequence;
        var tasks = _deleteTasks.ToArray();
        var batch = _deleteBatch;
        var modalSequence = _modalSequence;
        ConfirmDeleteButton.IsEnabled = false;
        try
        {
            var scope = DeleteSubtitleChoice.IsChecked == true ? "subtitle"
                : DeleteVideoChoice.IsChecked == true ? "video" : "all";
            DeleteResult result;
            if (batch) result = await _engine.ClearDoneAsync(scope, _stop.Token, tasks.Select(task => task.Id).ToArray());
            else result = scope == "all"
                ? await _engine.DeleteTaskAsync(tasks[0].Id, _stop.Token)
                : await _engine.DeleteTaskArtifactsAsync(tasks[0].Id, scope, _stop.Token);
            foreach (var task in tasks) InvalidateTaskDetails(task.Id);
            // 删除进行中允许关闭弹窗，晚到结果不能关闭随后打开的另一份确认。
            if (modalSequence == _modalSequence && DeleteDialog.Visibility == Visibility.Visible) CloseModal();
            ShowToast("删除完成", $"已删除 {result.DeletedFiles.Count} 个文件" + (result.Skipped.Count > 0 ? $"，跳过 {result.Skipped.Count} 个" : ""));
            await RefreshTasksAsync();
        }
        catch (OperationCanceledException) when (_stop.IsCancellationRequested) { }
        catch (Exception ex) { ShowError(ex.Message); }
        finally
        {
            if (operation == _deleteOperationSequence && modalSequence == _modalSequence &&
                DeleteDialog.Visibility == Visibility.Visible) ConfirmDeleteButton.IsEnabled = true;
        }
    }

    private async Task<bool> LoadConfigAsync(CancellationToken token = default)
    {
        if (!token.CanBeCanceled) token = _stop.Token;
        try
        {
            var config = await _engine.GetConfigAsync(token);
            await Dispatcher.InvokeAsync(() =>
            {
                token.ThrowIfCancellationRequested();
                _config = config;
                WithControlLoading(() =>
                {
                    Select(WhisperParseBox, config.Model, "small");
                    Select(WhisperSettingsBox, config.Model, "small");
                    Select(TranslationParseBox, config.TranslatorProvider, "null");
                    Select(TranslationSettingsBox, config.TranslatorProvider, "null");
                    Select(LlmVendorParseBox, config.LlmVendor, "deepseek");
                    Select(LlmVendorSettingsBox, config.LlmVendor, "deepseek");
                    LlmBaseParseBox.Text = LlmBaseSettingsBox.Text = config.LlmBaseUrl;
                    LlmModelParseBox.Text = LlmModelSettingsBox.Text = config.LlmModel;
                    LlmKeySettingsBox.Password = "";
                    LlmKeySettingsBox.ToolTip = string.IsNullOrWhiteSpace(config.LlmApiKeyMasked) ? "API Key" : $"已保存 {config.LlmApiKeyMasked}，留空保持不变";
                    Select(AsrProviderParseBox, config.TranscriberProvider, "faster-whisper");
                    Select(AsrProviderSettingsBox, config.TranscriberProvider, "faster-whisper");
                    Select(AsrVendorParseBox, config.AsrVendor, "siliconflow");
                    Select(AsrVendorSettingsBox, config.AsrVendor, "siliconflow");
                    AsrBaseParseBox.Text = AsrBaseSettingsBox.Text = config.AsrBaseUrl;
                    AsrModelParseBox.Text = AsrModelSettingsBox.Text = config.AsrModel;
                    AsrKeySettingsBox.Password = "";
                    AsrKeySettingsBox.ToolTip = string.IsNullOrWhiteSpace(config.AsrApiKeyMasked) ? "API Key" : $"已保存 {config.AsrApiKeyMasked}，留空保持不变";
                    Select(ProxyModeBox, config.Download.ProxyMode, "system");
                    CustomProxyBox.Text = config.Download.CustomProxy;
                    AriaCheck.IsChecked = config.Download.Aria2cEnabled;
                    FragmentsBox.Text = config.Download.FragmentsConcurrency.ToString();
                    ConcurrencySlider.Value = Math.Clamp(config.MaxConcurrent, 1, 4);
                });
                SyncDependentControls();
                SyncLoginControls();
            });
            token.ThrowIfCancellationRequested();
            StartWhisperStatus(config.Model);
            return true;
        }
        catch (OperationCanceledException) when (token.IsCancellationRequested) { }
        catch (Exception ex) { ShowError($"读取引擎配置失败：{ex.Message}"); }
        return false;
    }

    private async Task<bool> SaveEngineConfigAsync(bool showToast = true)
    {
        if (_loadingControls) return false;
        if (!_engine.IsConnected)
        {
            SettingsSaveStatus.Text = "引擎未连接，配置尚未保存";
            return false;
        }
        var acquired = false;
        try
        {
            await _configSaveLock.WaitAsync(_stop.Token);
            acquired = true;
            await Dispatcher.InvokeAsync(() => SettingsSaveStatus.Text = "正在保存…");
            var update = await Dispatcher.InvokeAsync(BuildConfigUpdate);
            await _engine.UpdateConfigAsync(update, _stop.Token);
            _config.Model = update.Model;
            _config.TranslatorProvider = update.TranslatorProvider;
            _config.LlmVendor = update.LlmVendor;
            _config.LlmBaseUrl = update.LlmBaseUrl;
            _config.LlmModel = update.LlmModel;
            _config.TranscriberProvider = update.TranscriberProvider;
            _config.AsrVendor = update.AsrVendor;
            _config.AsrBaseUrl = update.AsrBaseUrl;
            _config.AsrModel = update.AsrModel;
            await Dispatcher.InvokeAsync(() =>
            {
                // 保存请求期间仍可编辑，成功回调只能清除本次已提交的凭据。
                // 新输入或其他新修改必须保留，并明确显示尚未保存。
                var pendingChanges = System.Text.Json.JsonSerializer.Serialize(BuildConfigUpdate(), EngineClient.JsonOptions) !=
                    System.Text.Json.JsonSerializer.Serialize(update, EngineClient.JsonOptions);
                SettingsSaveStatus.Text = pendingChanges ? "已保存上一份设置；当前修改尚未保存" : "已保存并生效";
                if (showToast) ShowToast(pendingChanges ? "上一份设置已保存" : "引擎配置已保存并生效",
                    pendingChanges ? "当前修改尚未保存，请点击保存设置。" : "");
                if (!string.IsNullOrWhiteSpace(update.LlmApiKey))
                {
                    if (LlmKeySettingsBox.Password == update.LlmApiKey) LlmKeySettingsBox.Password = "";
                    LlmKeySettingsBox.ToolTip = "已保存凭证，留空保持不变";
                }
                if (!string.IsNullOrWhiteSpace(update.AsrApiKey))
                {
                    if (AsrKeySettingsBox.Password == update.AsrApiKey) AsrKeySettingsBox.Password = "";
                    AsrKeySettingsBox.ToolTip = "已保存凭证，留空保持不变";
                }
            });
            return true;
        }
        catch (OperationCanceledException) when (_stop.IsCancellationRequested) { }
        catch (Exception ex)
        {
            await Dispatcher.InvokeAsync(() => SettingsSaveStatus.Text = "保存失败，修改已保留；点击保存设置重试");
            ShowError($"配置保存失败：{ex.Message}");
        }
        finally { if (acquired) _configSaveLock.Release(); }
        return false;
    }

    private async void SaveSettings_Click(object sender, RoutedEventArgs e) => await SaveEngineConfigAsync(false);

    private EngineConfigUpdate BuildConfigUpdate() => new()
    {
        ProxyMode = Selected(ProxyModeBox, "system"),
        CustomProxy = CustomProxyBox.Text.Trim(),
        Aria2cEnabled = AriaCheck.IsChecked == true,
        FragmentsConcurrency = Math.Clamp(IntValue(FragmentsBox.Text, 16), 1, 64),
        MaxConcurrent = (int)ConcurrencySlider.Value,
        Model = Selected(WhisperSettingsBox, "small"),
        TranslatorProvider = Selected(TranslationSettingsBox, "null"),
        LlmApiKey = LlmKeySettingsBox.Password,
        LlmModel = LlmModelSettingsBox.Text.Trim(),
        LlmVendor = Selected(LlmVendorSettingsBox, "deepseek"),
        LlmBaseUrl = LlmBaseSettingsBox.Text.Trim(),
        TranscriberProvider = Selected(AsrProviderSettingsBox, "faster-whisper"),
        AsrVendor = Selected(AsrVendorSettingsBox, "siliconflow"),
        AsrBaseUrl = AsrBaseSettingsBox.Text.Trim(),
        AsrApiKey = AsrKeySettingsBox.Password,
        AsrModel = AsrModelSettingsBox.Text.Trim()
    };

    private async void ParseConfig_Changed(object sender, SelectionChangedEventArgs e)
    {
        if (_loadingControls) return;
        WithControlLoading(() =>
        {
            Select(WhisperSettingsBox, Selected(WhisperParseBox, "small"));
            Select(TranslationSettingsBox, Selected(TranslationParseBox, "null"));
            Select(AsrProviderSettingsBox, Selected(AsrProviderParseBox, "faster-whisper"));
        });
        SyncDependentControls();
        UpdateSubmitHint();
        await SaveEngineConfigAsync();
        if (ReferenceEquals(sender, WhisperParseBox)) StartWhisperStatus(Selected(WhisperParseBox, "small"));
    }

    private async void SettingsConfig_Changed(object sender, RoutedEventArgs e)
    {
        if (_loadingControls) return;
        WithControlLoading(() =>
        {
            Select(TranslationParseBox, Selected(TranslationSettingsBox, "null"));
            Select(AsrProviderParseBox, Selected(AsrProviderSettingsBox, "faster-whisper"));
        });
        SyncDependentControls();
        UpdateSubmitHint();
        await SaveEngineConfigAsync();
    }

    private async void WhisperModel_Changed(object sender, SelectionChangedEventArgs e)
    {
        if (_loadingControls) return;
        var model = Selected(WhisperSettingsBox, "small");
        WithControlLoading(() => Select(WhisperParseBox, model));
        SyncDependentControls();
        await SaveEngineConfigAsync();
        StartWhisperStatus(model);
    }

    private async void ParseLlmVendor_Changed(object sender, SelectionChangedEventArgs e)
    {
        if (_loadingControls) return;
        var vendor = Selected(LlmVendorParseBox, "deepseek");
        WithControlLoading(() => Select(LlmVendorSettingsBox, vendor));
        ApplyLlmPreset(vendor);
        await SaveEngineConfigAsync();
    }

    private async void SettingsLlmVendor_Changed(object sender, SelectionChangedEventArgs e)
    {
        if (_loadingControls) return;
        var vendor = Selected(LlmVendorSettingsBox, "deepseek");
        WithControlLoading(() => Select(LlmVendorParseBox, vendor));
        ApplyLlmPreset(vendor);
        await SaveEngineConfigAsync();
    }

    private async void ParseAsrVendor_Changed(object sender, SelectionChangedEventArgs e)
    {
        if (_loadingControls) return;
        var vendor = Selected(AsrVendorParseBox, "siliconflow");
        WithControlLoading(() => Select(AsrVendorSettingsBox, vendor));
        ApplyAsrPreset(vendor);
        await SaveEngineConfigAsync();
    }

    private async void SettingsAsrVendor_Changed(object sender, SelectionChangedEventArgs e)
    {
        if (_loadingControls) return;
        var vendor = Selected(AsrVendorSettingsBox, "siliconflow");
        WithControlLoading(() => Select(AsrVendorParseBox, vendor));
        ApplyAsrPreset(vendor);
        await SaveEngineConfigAsync();
    }

    private async void ParseTextConfig_Changed(object sender, RoutedEventArgs e)
    {
        if (_loadingControls) return;
        WithControlLoading(() =>
        {
            LlmBaseSettingsBox.Text = LlmBaseParseBox.Text;
            LlmModelSettingsBox.Text = LlmModelParseBox.Text;
            AsrBaseSettingsBox.Text = AsrBaseParseBox.Text;
            AsrModelSettingsBox.Text = AsrModelParseBox.Text;
        });
        await SaveEngineConfigAsync();
    }

    private async void SettingsTextConfig_Changed(object sender, RoutedEventArgs e)
    {
        if (_loadingControls) return;
        WithControlLoading(() =>
        {
            LlmBaseParseBox.Text = LlmBaseSettingsBox.Text;
            LlmModelParseBox.Text = LlmModelSettingsBox.Text;
            AsrBaseParseBox.Text = AsrBaseSettingsBox.Text;
            AsrModelParseBox.Text = AsrModelSettingsBox.Text;
        });
        await SaveEngineConfigAsync();
    }

    private void ApplyLlmPreset(string vendor)
    {
        if (!LlmPresets.TryGetValue(vendor, out var preset)) return;
        WithControlLoading(() =>
        {
            LlmBaseParseBox.Text = LlmBaseSettingsBox.Text = preset.BaseUrl;
            LlmModelParseBox.Text = LlmModelSettingsBox.Text = preset.Model;
        });
    }

    private void ApplyAsrPreset(string vendor)
    {
        if (!AsrPresets.TryGetValue(vendor, out var preset)) return;
        WithControlLoading(() =>
        {
            AsrBaseParseBox.Text = AsrBaseSettingsBox.Text = preset.BaseUrl;
            AsrModelParseBox.Text = AsrModelSettingsBox.Text = preset.Model;
        });
    }

    private void SyncDependentControls(bool animateSubtitles = false)
    {
        var translate = Selected(TranslationSettingsBox, "null") == "llm";
        LlmCloudSettings.IsEnabled = translate;
        LlmCloudSettings.Visibility = translate ? Visibility.Visible : Visibility.Collapsed;
        LlmCloudSettings.Opacity = translate ? 1 : .45;
        LlmVendorParseBox.IsEnabled = LlmBaseParseBox.IsEnabled = LlmModelParseBox.IsEnabled = translate;

        var cloudAsr = Selected(AsrProviderSettingsBox, "faster-whisper") == "cloud";
        AsrCloudSettings.IsEnabled = cloudAsr;
        AsrCloudSettings.Visibility = cloudAsr ? Visibility.Visible : Visibility.Collapsed;
        AsrCloudSettings.Opacity = cloudAsr ? 1 : .45;
        AsrVendorParseBox.IsEnabled = AsrBaseParseBox.IsEnabled = AsrModelParseBox.IsEnabled = cloudAsr;
        WhisperSettingsRow.IsEnabled = !cloudAsr;
        WhisperSettingsRow.Visibility = cloudAsr ? Visibility.Collapsed : Visibility.Visible;
        GpuSettingsRow.Visibility = cloudAsr ? Visibility.Collapsed : Visibility.Visible;
        GpuAdvancedSettings.Visibility = cloudAsr ? Visibility.Collapsed : Visibility.Visible;
        SpeechSettingsSummary.Text = (cloudAsr ? $"云端识别 · {Selected(AsrVendorSettingsBox, "siliconflow")}" :
            $"本地离线 · {Selected(WhisperSettingsBox, "small")}") + (translate ? " · 翻译开启" : " · 翻译关闭");
        WhisperSettingsRow.Opacity = cloudAsr ? .45 : 1;
        WhisperParseBox.IsEnabled = !cloudAsr;
        var customProxy = Selected(ProxyModeBox, "system") == "custom";
        CustomProxyBox.IsEnabled = customProxy;
        CustomProxyBox.Visibility = customProxy ? Visibility.Visible : Visibility.Collapsed;
        var recognize = RecognizeCheck.IsChecked == true;
        var translateSubtitles = TranslateCheck.IsChecked == true;
        WithControlLoading(() => Select(SubtitleModeBox, !recognize ? "none" : translateSubtitles ? "translate" : "source"));
        SubtitleModeSummary.Text = !recognize ? "无需识别，直接下载。" :
            translateSubtitles ? "AI 自动理解语境、人名与术语，再翻译成选中的目标语言。" : "从视频语音生成原语言字幕。";
        var sequence = ++_subtitleAnimationSequence;
        SetSubtitleSection(TranslationLanguageRow, translateSubtitles, animateSubtitles, sequence);
        SetSubtitleSection(TranslationProviderRow, translateSubtitles, animateSubtitles, sequence);
        SetSubtitleSection(RecognitionOptions, recognize, animateSubtitles, sequence);
        BurnCheck.IsEnabled = recognize;
        SetSubtitleSection(BurnCard, recognize, animateSubtitles, sequence);
        SetSubtitleSection(BurnSettingsPanel, recognize && BurnCheck.IsChecked == true, animateSubtitles, sequence);
        BilingualCheck.IsEnabled = recognize && translateSubtitles && BurnCheck.IsChecked == true;
        SubtitleCustomColorBox.Visibility = Selected(SubtitleColorBox) == "custom" ? Visibility.Visible : Visibility.Collapsed;
        ConcurrencyLabel.Text = $"最多 {(int)ConcurrencySlider.Value} 个任务并行";
        SyncWhisperPrompt();
    }

    private async void DownloadWhisper_Click(object sender, RoutedEventArgs e)
    {
        if (!_engine.IsConnected) { ShowError("本地视频引擎尚未连接。"); return; }
        var model = Selected(WhisperSettingsBox, "small");
        DownloadWhisperButton.IsEnabled = false;
        PrepareWhisperButton.IsEnabled = false;
        DownloadWhisperButton.Content = "连接中…";
        WhisperStatusText.Text = "正在连接模型仓库…";
        try
        {
            await _engine.DownloadWhisperAsync(model, _stop.Token);
            StartWhisperStatus(model);
        }
        catch (OperationCanceledException) when (_stop.IsCancellationRequested) { }
        catch (Exception ex)
        {
            DownloadWhisperButton.IsEnabled = true;
            DownloadWhisperButton.Content = "下载模型";
            PrepareWhisperButton.IsEnabled = true;
            ParseWhisperStatusText.Text = $"模型下载失败：{FriendlyError(ex.Message)}";
            ShowError($"模型下载失败：{ex.Message}");
        }
    }

    private async void DownloadGpu_Click(object sender, RoutedEventArgs e)
    {
        if (!_engine.IsConnected) { ShowError("本地视频引擎尚未连接。"); return; }
        DownloadGpuButton.IsEnabled = false;
        GpuStatusText.Text = "正在连接 NVIDIA 组件仓库…";
        try
        {
            await _engine.DownloadGpuAsync(_stop.Token);
            StartGpuStatus();
        }
        catch (OperationCanceledException) when (_stop.IsCancellationRequested) { }
        catch (Exception ex)
        {
            DownloadGpuButton.IsEnabled = true;
            GpuStatusText.Text = $"组件下载失败：{FriendlyError(ex.Message)}；CPU 仍可使用";
        }
    }

    private void StartGpuStatus()
    {
        if (_stop.IsCancellationRequested || !_engine.IsConnected) return;
        _gpuPollStop?.Cancel();
        _gpuPollStop?.Dispose();
        _gpuPollStop = CancellationTokenSource.CreateLinkedTokenSource(_stop.Token);
        _ = PollGpuStatusAsync(_gpuPollStop.Token);
    }

    private async Task PollGpuStatusAsync(CancellationToken token)
    {
        try
        {
            while (!token.IsCancellationRequested && _engine.IsConnected)
            {
                var status = await _engine.GetGpuStatusAsync(token);
                await Dispatcher.InvokeAsync(() => ApplyGpuStatus(status));
                if (status.Status != "downloading") return;
                await Task.Delay(1200, token);
            }
        }
        catch (OperationCanceledException) when (token.IsCancellationRequested) { }
        catch (Exception ex) { GpuStatusText.Text = $"组件状态读取失败：{FriendlyError(ex.Message)}"; }
    }

    private void ApplyGpuStatus(GpuStatus status)
    {
        var busy = status.Status == "downloading";
        GpuProgress.Visibility = busy ? Visibility.Visible : Visibility.Collapsed;
        GpuProgress.IsIndeterminate = busy && status.Fraction <= 0;
        GpuProgress.Value = Math.Clamp(status.Fraction, 0, 1) * 100;
        DownloadGpuButton.IsEnabled = status.Supported && !busy && (!status.Downloaded || status.Status == "failed");
        DownloadGpuButton.Content = busy ? "下载中…" : status.Downloaded ? "已安装" :
            status.Status == "failed" ? "重试下载" : $"下载 GPU 组件 · {status.SizeBytes / 1048576.0:0} MiB";
        GpuStatusText.Text = !status.Supported ? "当前使用 CPU · 未检测到可用 NVIDIA 显卡" :
            status.Status == "failed" ? $"下载失败：{FriendlyError(status.Error)}；CPU 仍可使用" :
            !string.IsNullOrWhiteSpace(status.Message) ? status.Message :
            status.Downloaded ? "GPU 组件可用；后续识别自动使用" : "当前使用 CPU · 下载组件后可启用 NVIDIA 加速";
    }

    private void StartWhisperStatus(string model)
    {
        if (_stop.IsCancellationRequested) return;
        _whisperPollStop?.Cancel();
        _whisperPollStop?.Dispose();
        _whisperPollStop = CancellationTokenSource.CreateLinkedTokenSource(_stop.Token);
        _ = PollWhisperStatusAsync(model, _whisperPollStop.Token);
    }

    private async Task PollWhisperStatusAsync(string model, CancellationToken token)
    {
        while (!token.IsCancellationRequested && _engine.IsConnected)
        {
            try
            {
                var status = await _engine.GetWhisperStatusAsync(model, token);
                var busy = status.Status is "downloading" or "loading";
                await Dispatcher.InvokeAsync(() => ApplyWhisperStatus(status));
                if (!busy) return;
                await Task.Delay(1200, token);
            }
            catch (OperationCanceledException) when (token.IsCancellationRequested) { return; }
            catch (Exception ex)
            {
                await Dispatcher.InvokeAsync(() => WhisperStatusText.Text = $"模型状态检查失败：{FriendlyError(ex.Message)}");
                return;
            }
        }
    }

    private void ApplyWhisperStatus(WhisperStatus status)
    {
        if (status.Model != Selected(WhisperParseBox, "small")) return;
        _whisperStatus = status;
        var busy = status.Status is "downloading" or "loading";
        var percent = Math.Clamp(status.Fraction * 100, 0, 100);
        WhisperProgress.Value = percent;
        WhisperProgress.IsIndeterminate = status.Status == "loading";
        WhisperProgress.Visibility = busy ? Visibility.Visible : Visibility.Collapsed;
        DownloadWhisperButton.IsEnabled = !busy && (!status.Downloaded || status.Status is "failed" or "load_failed");
        DownloadWhisperButton.Content = status.Status switch
        {
            "loading" => "载入中…", "downloading" => "下载中…", "ready" => "已就绪",
            "downloaded" => "已下载", "failed" => "重试下载", "load_failed" => "重试",
            _ => "下载模型"
        };
        WhisperStatusText.Text = !string.IsNullOrWhiteSpace(status.Message) ? status.Message : status.Status switch
        {
            "ready" => $"已载入 · {status.Model}",
            "downloaded" => $"已下载，首次识别时载入 · {status.Model}",
            "loading" => "正在载入模型…",
            "downloading" => $"下载中 {percent:0}%",
            "load_failed" => $"模型已下载，但载入失败：{status.Error}",
            "failed" => $"下载失败：{status.Error}",
            _ when status.Downloaded => $"已下载，首次识别时载入 · {status.Model}",
            _ => $"尚未下载 {status.Model}，生成字幕前需下载；仅下载视频不需要。"
        };
        if (!status.Downloaded && !busy)
        {
            DownloadWhisperButton.Content = "下载模型";
            WhisperStatusText.Text = status.Status == "failed" ? $"模型下载失败：{status.Error}" :
                $"语音模型 {status.Model} 未下载或文件不完整，生成字幕前需先准备。";
        }
        SyncWhisperPrompt();
    }

    private void SyncWhisperPrompt()
    {
        if (WhisperPrompt is null) return;
        var status = _whisperStatus;
        var needed = RecognizeCheck.IsChecked == true && Selected(AsrProviderParseBox, "faster-whisper") != "cloud";
        var current = status?.Model == Selected(WhisperParseBox, "small");
        WhisperPrompt.Visibility = needed && current && status is not null &&
            (!status.Downloaded || status.Status is "downloading" or "loading" or "failed" or "load_failed")
            ? Visibility.Visible : Visibility.Collapsed;
        PrepareWhisperButton.Visibility = WhisperPrompt.Visibility;
        ParseWhisperStatusText.Text = WhisperStatusText.Text;
        ParseWhisperProgress.Value = WhisperProgress.Value;
        ParseWhisperProgress.IsIndeterminate = WhisperProgress.IsIndeterminate;
        ParseWhisperProgress.Visibility = WhisperProgress.Visibility;
        PrepareWhisperButton.Content = status?.Status == "downloading" ? $"模型下载 {status.Fraction * 100:0}%" :
            status?.Status == "loading" ? "模型载入中…" : status?.Status == "load_failed" ? "重试载入模型" : "下载语音模型";
        PrepareWhisperButton.IsEnabled = DownloadWhisperButton.IsEnabled;
    }


    private void BuildLanguageChoices()
    {
        foreach (var item in new[]
        {
            ("zh", "中文"), ("en", "英语"), ("ja", "日语"), ("ko", "韩语"),
            ("es", "西语"), ("fr", "法语"), ("de", "德语"), ("ru", "俄语"),
            ("pt", "葡语"), ("th", "泰语"), ("vi", "越南语")
        })
        {
            var check = new CheckBox { Content = item.Item2, Tag = item.Item1, Style = (Style)Resources["LanguageChip"], IsChecked = item.Item1 == "zh" };
            check.Click += Language_Click;
            LanguagePanel.Children.Add(check);
        }
    }

    private void Language_Click(object sender, RoutedEventArgs e)
    {
        if (sender is not CheckBox { Tag: string language } check) return;
        if (check.IsChecked == true) _selectedLanguages.Add(language);
        else _selectedLanguages.Remove(language);
        if (_selectedLanguages.Count == 0)
        {
            check.IsChecked = true;
            _selectedLanguages.Add(language);
        }
        UpdateSubmitHint();
    }

    private void SetSubtitleSection(FrameworkElement section, bool show, bool animate, int sequence)
    {
        if (!animate || !IsLoaded || !SystemParameters.ClientAreaAnimation)
        {
            section.BeginAnimation(MaxHeightProperty, null);
            section.BeginAnimation(OpacityProperty, null);
            section.MaxHeight = double.PositiveInfinity;
            section.Opacity = 1;
            section.Visibility = show ? Visibility.Visible : Visibility.Collapsed;
            return;
        }
        if (show && section.Visibility == Visibility.Visible && double.IsPositiveInfinity(section.MaxHeight)) return;
        if (!show && section.Visibility == Visibility.Collapsed) return;
        var from = section.Visibility == Visibility.Visible ? section.ActualHeight : 0;
        section.BeginAnimation(MaxHeightProperty, null);
        section.BeginAnimation(OpacityProperty, null);
        section.MaxHeight = double.PositiveInfinity;
        section.Visibility = Visibility.Visible;
        section.Measure(new Size(Math.Max(1, (section.Parent as FrameworkElement)?.ActualWidth ?? ParseLayout.ActualWidth), double.PositiveInfinity));
        var to = show ? section.DesiredSize.Height : 0;
        section.MaxHeight = from;
        section.Opacity = from > 0 ? 1 : 0;
        var duration = TimeSpan.FromMilliseconds(show ? 220 : 170);
        var motion = new DoubleAnimation(from, to, duration) { EasingFunction = EaseOut, FillBehavior = FillBehavior.Stop };
        motion.Completed += (_, _) =>
        {
            if (sequence != _subtitleAnimationSequence) return;
            section.MaxHeight = double.PositiveInfinity;
            section.Opacity = 1;
            if (!show) section.Visibility = Visibility.Collapsed;
        };
        section.BeginAnimation(MaxHeightProperty, motion);
        section.BeginAnimation(OpacityProperty, new DoubleAnimation(section.Opacity, show ? 1 : 0, duration) { FillBehavior = FillBehavior.Stop });
    }

    private void SubtitleChoice_Changed(object sender, SelectionChangedEventArgs e)
    {
        if (_loadingControls) return;
        var mode = Selected(SubtitleModeBox, "none");
        RecognizeCheck.IsChecked = mode != "none";
        TranslateCheck.IsChecked = mode == "translate";
        SubtitleMode_Changed(mode == "translate" ? TranslateCheck : RecognizeCheck, new RoutedEventArgs());
    }

    private async void SubtitleMode_Changed(object sender, RoutedEventArgs e)
    {
        if (_loadingControls) return;
        var enableTranslation = ReferenceEquals(sender, TranslateCheck) && TranslateCheck.IsChecked == true;
        if (enableTranslation)
        {
            RecognizeCheck.IsChecked = true;
            WithControlLoading(() =>
            {
                Select(TranslationParseBox, "llm");
                Select(TranslationSettingsBox, "llm");
            });
        }
        if (ReferenceEquals(sender, RecognizeCheck) && RecognizeCheck.IsChecked != true)
        {
            TranslateCheck.IsChecked = false;
            BurnCheck.IsChecked = false;
        }
        _ui.Recognize = RecognizeCheck.IsChecked == true;
        _ui.Translate = TranslateCheck.IsChecked == true;
        _ui.Burn = BurnCheck.IsChecked == true;
        _ui.Save();
        SyncDependentControls(true);
        UpdateSubmitHint();
        if (enableTranslation) await SaveEngineConfigAsync(false);
    }

    private void BurnOption_Changed(object sender, RoutedEventArgs e)
    {
        if (_loadingControls) return;
        _ui.Burn = BurnCheck.IsChecked == true;
        _ui.Bilingual = BilingualCheck.IsChecked == true;
        _ui.Save();
        SyncDependentControls(true);
        UpdateSubmitHint();
        QueueSubtitlePreview();
    }

    private void BurnStyle_Changed(object sender, SelectionChangedEventArgs e)
    {
        if (_loadingControls) return;
        if (ReferenceEquals(sender, SubtitlePositionBox))
        {
            _ui.SubtitleHorizontalOffset = 0;
            WithControlLoading(() => VerticalOffsetSlider.Value = 0);
        }
        SyncDependentControls();
        SaveSubtitleStyle();
        QueueSubtitlePreview();
    }

    private void BurnColor_LostFocus(object sender, RoutedEventArgs e)
    {
        if (_loadingControls) return;
        SaveSubtitleStyle();
        QueueSubtitlePreview();
    }

    private void ResetSubtitlePosition_Click(object sender, RoutedEventArgs e)
    {
        WithControlLoading(() =>
        {
            Select(SubtitlePositionBox, "bottom");
            VerticalOffsetSlider.Value = 0;
        });
        _ui.SubtitleHorizontalOffset = 0;
        SubtitleDragMarker.Visibility = Visibility.Collapsed;
        SaveSubtitleStyle();
        QueueSubtitlePreview();
    }

    private void SubtitlePreview_MouseDown(object sender, MouseButtonEventArgs e)
    {
        if (BurnCheck.IsChecked != true || !SubtitlePreviewSurface.CaptureMouse()) return;
        _subtitleDragging = true;
        MoveSubtitleMarker(e.GetPosition(SubtitlePreviewSurface));
        e.Handled = true;
    }

    private void SubtitlePreview_MouseMove(object sender, MouseEventArgs e)
    {
        if (_subtitleDragging) MoveSubtitleMarker(e.GetPosition(SubtitlePreviewSurface));
    }

    private void SubtitlePreview_MouseUp(object sender, MouseButtonEventArgs e)
    {
        if (!_subtitleDragging) return;
        var point = e.GetPosition(SubtitlePreviewSurface);
        _subtitleDragging = false;
        SubtitlePreviewSurface.ReleaseMouseCapture();
        var x = Math.Clamp((int)Math.Round(point.X / SubtitlePreviewSurface.ActualWidth * 640), 50, 590);
        var y = Math.Clamp((int)Math.Round(point.Y / SubtitlePreviewSurface.ActualHeight * 360), 18, 342);
        var position = y < 108 ? "top" : y < 252 ? "middle" : "bottom";
        var baseY = position == "top" ? 36 : position == "middle" ? 180 : 324;
        WithControlLoading(() =>
        {
            Select(SubtitlePositionBox, position);
            VerticalOffsetSlider.Value = y - baseY;
        });
        _ui.SubtitleHorizontalOffset = x - 320;
        SaveSubtitleStyle();
        QueueSubtitlePreview();
        e.Handled = true;
    }

    private void SubtitlePreview_LostCapture(object sender, MouseEventArgs e)
    {
        _subtitleDragging = false;
        SubtitleDragMarker.Visibility = Visibility.Collapsed;
    }

    private void MoveSubtitleMarker(Point point)
    {
        Canvas.SetLeft(SubtitleDragMarker, Math.Clamp(point.X - 7, 0, SubtitlePreviewSurface.ActualWidth - 14));
        Canvas.SetTop(SubtitleDragMarker, Math.Clamp(point.Y - 7, 0, SubtitlePreviewSurface.ActualHeight - 14));
        SubtitleDragMarker.Visibility = Visibility.Visible;
    }

    private void VerticalOffsetSlider_ValueChanged(object sender, RoutedPropertyChangedEventArgs<double> e)
    {
        if (VerticalOffsetValue is null) return;
        VerticalOffsetValue.Text = ((int)e.NewValue).ToString();
        if (_loadingControls) return;
        SaveSubtitleStyle();
        QueueSubtitlePreview();
    }

    private void OutlineSlider_ValueChanged(object sender, RoutedPropertyChangedEventArgs<double> e)
    {
        if (OutlineValue is null) return;
        OutlineValue.Text = ((int)e.NewValue).ToString();
        if (_loadingControls) return;
        SaveSubtitleStyle();
        QueueSubtitlePreview();
    }

    private void SaveSubtitleStyle()
    {
        _ui.FontName = Selected(FontNameBox);
        _ui.FontSize = IntValue(Selected(FontSizeBox), 18);
        _ui.SubtitlePosition = Selected(SubtitlePositionBox, "bottom");
        _ui.Outline = (int)OutlineSlider.Value;
        var color = SubtitleColorValue();
        if (Regex.IsMatch(color, "^#[0-9A-Fa-f]{6}$")) _ui.SubtitleColor = color.ToUpperInvariant();
        _ui.SubtitleVerticalOffset = (int)VerticalOffsetSlider.Value;
        _ui.Save();
    }

    private string SubtitleColorValue() => Selected(SubtitleColorBox, "#FFFFFF") == "custom"
        ? SubtitleCustomColorBox.Text.Trim() : Selected(SubtitleColorBox, "#FFFFFF");

    private void QueueSubtitlePreview()
    {
        if (!_engine.IsConnected || SubtitlePreviewImage is null || BurnCheck.IsChecked != true) return;
        _previewStop?.Cancel();
        _previewStop?.Dispose();
        _previewStop = CancellationTokenSource.CreateLinkedTokenSource(_stop.Token);
        var token = _previewStop.Token;
        var font = Selected(FontNameBox);
        var size = IntValue(Selected(FontSizeBox), 18);
        var position = Selected(SubtitlePositionBox, "bottom");
        var outline = (int)OutlineSlider.Value;
        var color = SubtitleColorValue();
        if (!Regex.IsMatch(color, "^#[0-9A-Fa-f]{6}$")) return;
        var verticalOffset = (int)VerticalOffsetSlider.Value;
        var horizontalOffset = _ui.SubtitleHorizontalOffset;
        var text = BilingualCheck.IsChecked == true ? "中文字幕示例\nEnglish subtitle sample" : "字幕预览 Subtitle Preview 123";
        _ = Task.Run(async () =>
        {
            try
            {
                await Task.Delay(180, token);
                var bitmap = BitmapFromBytes(await _engine.GetSubtitlePreviewAsync(font, size, position, outline, color, verticalOffset, horizontalOffset, text, token));
                await Dispatcher.InvokeAsync(() => SubtitlePreviewImage.Source = bitmap);
            }
            catch (OperationCanceledException) { }
            catch { }
        }, token);
    }

    private void UpdateSubmitHint()
    {
        if (SubmitHint is null) return;
        var format = FormatList?.SelectedItem as FormatInfo;
        var parts = new List<string> { format?.SizeText ?? "大小未知" };
        if (format is not null) parts.Insert(0, format.Label);
        parts.Add(RecognizeCheck.IsChecked != true ? "仅下载视频" :
            TranslateCheck.IsChecked == true ? $"识别并翻译 {_selectedLanguages.Count} 种目标语言" : "识别源语言字幕");
        if (RecognizeCheck.IsChecked == true && BurnCheck.IsChecked == true)
            parts.Add(BilingualCheck.IsChecked == true && TranslateCheck.IsChecked == true ? "双语字幕烧录" : "硬字幕烧录");
        SubmitHint.Text = string.Join("，", parts);
    }

    private void FormatList_SelectionChanged(object sender, SelectionChangedEventArgs e) => UpdateSubmitHint();

    private async void BrowseOutput_Click(object sender, RoutedEventArgs e) => await OpenDirectoryDialogAsync(OutputPathBox.Text, SetOutputDirectory);

    private static bool IsBilibiliUrl(string url) => Uri.TryCreate(NormalizeUrl(url), UriKind.Absolute, out var uri)
        && (uri.Host.Equals("b23.tv", StringComparison.OrdinalIgnoreCase)
            || uri.Host.Equals("bilibili.com", StringComparison.OrdinalIgnoreCase)
            || uri.Host.EndsWith(".bilibili.com", StringComparison.OrdinalIgnoreCase));

    private void SyncLoginControls()
    {
        WithControlLoading(() =>
        {
            var file = _config.Download.Cookies.Trim();
            Select(ParseAuthSourceBox, file.Length > 0 ? "file" : _config.Download.CookiesFromBrowser);
            ParseCookiesFileBox.Text = file;
        });
        var fromFile = Selected(ParseAuthSourceBox) == "file";
        BrowseCookiesButton.Visibility = ParseCookiesFileBox.Visibility = fromFile ? Visibility.Visible : Visibility.Collapsed;
    }

    private void SyncPlatformLoginControls()
    {
        if (BilibiliLoginBar is null || ConnectPlatformButton is null || AuthPlatformTitle is null) return;
        var url = NormalizeUrl(UrlBox.Text);
        var bilibili = IsBilibiliUrl(url);
        var platform = PlatformConnectionWindow.PlatformForUrl(url);
        BilibiliLoginBar.Visibility = bilibili || platform is not null ? Visibility.Visible : Visibility.Collapsed;
        ConnectBilibiliButton.Visibility = bilibili ? Visibility.Visible : Visibility.Collapsed;
        ConnectPlatformButton.Visibility = platform is not null ? Visibility.Visible : Visibility.Collapsed;
        var name = platform == "douyin" ? "抖音" : "小红书";
        ConnectPlatformButton.Content = "扫码登录" + name;
        BilibiliLoginAdvanced.Visibility = platform == "xiaohongshu" ? Visibility.Collapsed : Visibility.Visible;
        AuthPlatformTitle.Text = bilibili ? "B 站账号（可选）" : name + "连接（可选）";
        ParseAuthHint.Text = bilibili ? "未登录也可下载公开画质；需要更高清时可连接账号。"
            : platform == "xiaohongshu" ? "扫码并在手机上确认，登录后自动继续解析。"
            : "扫码后自动继续；首页链接会打开官网供你选择具体视频。";
    }

    private async void ConnectPlatform_Click(object sender, RoutedEventArgs e) => await ConnectPlatformAsync();

    private async Task ConnectPlatformAsync()
    {
        var originalUrl = NormalizeUrl(UrlBox.Text);
        var platform = PlatformConnectionWindow.PlatformForUrl(originalUrl);
        if (platform is null) return;
        PlatformConnectionWindow? connection = null;
        connection = new PlatformConnectionWindow(this, platform, originalUrl, async cookies =>
        {
            if (!_engine.IsConnected && !await _engine.EnsureConnectedAsync(true, _stop.Token))
                throw new InvalidOperationException("本地视频引擎未就绪，请重试连接。");
            if (connection?.IsVisible != true) throw new OperationCanceledException();
            await _engine.SavePlatformSessionAsync(platform, cookies, _stop.Token);
        });
        if (connection.ShowDialog() != true || _stop.IsCancellationRequested) return;
        UrlBox.Text = connection.VideoUrl;
        await ProbeAsync(true);
    }

    private void ParseAuthSource_Changed(object sender, SelectionChangedEventArgs e)
    {
        if (_loadingControls) return;
        var source = Selected(ParseAuthSourceBox);
        WithControlLoading(() =>
        {
            if (source != "file") ParseCookiesFileBox.Text = "";
        });
        BrowseCookiesButton.Visibility = ParseCookiesFileBox.Visibility = source == "file" ? Visibility.Visible : Visibility.Collapsed;
        ParseAuthHint.Text = "登录来源已变更，请点击“读取并刷新”。浏览器需先登录当前网站；读取失败时可连接官网或选择 Cookie 文件。";
        _probe = null;
        ProbePanel.Visibility = Visibility.Collapsed;
    }

    private async void BrowseCookies_Click(object sender, RoutedEventArgs e)
    {
        var dialog = new OpenFileDialog { Title = "选择网站 Cookie 文件", DefaultExt = ".txt",
            Filter = "Cookie 文件 (*.txt)|*.txt|所有文件 (*.*)|*.*" };
        if (dialog.ShowDialog(this) != true) return;
        WithControlLoading(() =>
        {
            ParseCookiesFileBox.Text = dialog.FileName;
            Select(ParseAuthSourceBox, "file");
        });
        if (await SaveLoginAsync()) await ProbeAsync(true);
    }

    private async void ReadLogin_Click(object sender, RoutedEventArgs e)
    {
        if (Selected(ParseAuthSourceBox) == "file" && !File.Exists(ParseCookiesFileBox.Text.Trim()))
        { ShowError("请先选择现有的 Cookie 文件。"); return; }
        if (!_engine.IsConnected) { ShowError("本地视频引擎尚未连接。"); return; }
        if (await SaveLoginAsync()) await ProbeAsync(true);
    }

    private async Task<bool> SaveLoginAsync()
    {
        var source = Selected(ParseAuthSourceBox);
        var file = source == "file" ? ParseCookiesFileBox.Text.Trim() : "";
        var browser = source == "file" ? "" : source;
        try
        {
            await _engine.UpdateLoginAsync(file, browser, _stop.Token);
            _config.Download.Cookies = file;
            _config.Download.CookiesFromBrowser = browser;
            return true;
        }
        catch (OperationCanceledException) when (_stop.IsCancellationRequested) { return false; }
        catch (Exception ex) { ShowError(ex.Message); return false; }
    }

    private async void ConnectBilibili_Click(object sender, RoutedEventArgs e)
    {
        if (_bilibiliLoginStop is not null)
        { _bilibiliLoginStop.Cancel(); BilibiliQrPanel.Visibility = Visibility.Collapsed; return; }
        if (!_engine.IsConnected) { ShowError("本地视频引擎尚未连接。"); return; }
        using var loginStop = CancellationTokenSource.CreateLinkedTokenSource(_stop.Token);
        loginStop.CancelAfter(TimeSpan.FromSeconds(200));
        _bilibiliLoginStop = loginStop;
        ConnectBilibiliButton.Content = "取消连接";
        BilibiliQrPanel.Visibility = Visibility.Visible;
        BilibiliQrImage.Source = null;
        BilibiliQrStatus.Text = "正在连接 B 站…";
        ClearError();
        try
        {
            var login = await _engine.StartBilibiliLoginAsync(loginStop.Token);
            BilibiliQrImage.Source = BitmapFromBytes(Convert.FromBase64String(login.ImageBase64));
            BilibiliQrStatus.Text = login.Message;
            while (!loginStop.IsCancellationRequested)
            {
                await Task.Delay(2000, loginStop.Token);
                var status = await _engine.PollBilibiliLoginAsync(login.SessionId, loginStop.Token);
                BilibiliQrStatus.Text = status.Message;
                if (status.Status == "expired") return;
                if (status.Status != "connected") continue;
                BilibiliQrPanel.Visibility = Visibility.Collapsed;
                BilibiliQrImage.Source = null;
                if (!await LoadConfigAsync(loginStop.Token)) return;
                ParseAuthHint.Text = "B 站已连接，正在刷新画质…";
                ShowToast("B 站已连接");
                await ProbeAsync(true);
                return;
            }
        }
        catch (OperationCanceledException) when (loginStop.IsCancellationRequested)
        { BilibiliQrStatus.Text = "扫码已结束，点击连接可重新生成二维码。"; }
        catch (Exception ex)
        { BilibiliQrStatus.Text = "连接失败，请重试。"; ShowError(ex.Message); }
        finally
        {
            BilibiliQrImage.Source = null;
            if (ReferenceEquals(_bilibiliLoginStop, loginStop)) _bilibiliLoginStop = null;
            ConnectBilibiliButton.IsEnabled = true;
            ConnectBilibiliButton.Content = "连接 B 站";
        }
    }

    private async void RefreshQuality_Click(object sender, RoutedEventArgs e)
    {
        if (await SaveEngineConfigAsync(false)) await ProbeAsync(true);
    }
    private async void BrowseSettingsOutput_Click(object sender, RoutedEventArgs e) => await OpenDirectoryDialogAsync(_ui.OutputDirectory, SetOutputDirectory);

    private async Task OpenDirectoryDialogAsync(string start, Action<string> selected)
    {
        if (!_engine.IsConnected) { ShowError("本地视频引擎尚未连接。"); return; }
        _directorySelected = selected;
        OpenModal(DirectoryDialog);
        try
        {
            var drives = await _engine.GetDrivesAsync(_stop.Token);
            _homeDirectory = drives.Home;
            DrivePanel.Children.Clear();
            foreach (var drive in drives.Drives)
            {
                var button = new Button { Content = drive, Tag = drive, Style = (Style)FindResource("ChipButton") };
                button.Click += async (_, _) => await NavigateDirectoryAsync(drive);
                DrivePanel.Children.Add(button);
            }
            await NavigateDirectoryAsync(string.IsNullOrWhiteSpace(start) ? drives.Home : start);
        }
        catch (OperationCanceledException) when (_stop.IsCancellationRequested) { }
        catch (Exception ex)
        {
            CloseModal();
            ShowError(ex.Message);
        }
    }

    private async Task NavigateDirectoryAsync(string path)
    {
        try
        {
            var listing = await _engine.GetDirectoriesAsync(path, _stop.Token);
            _currentDirectory = listing.Current;
            _parentDirectory = listing.Parent;
            if (!string.IsNullOrWhiteSpace(listing.Home)) _homeDirectory = listing.Home;
            DirectoryCurrentText.Text = listing.Current;
            DirectoryListBox.ItemsSource = listing.Directories;
            DirectoryUpButton.IsEnabled = !string.IsNullOrWhiteSpace(_parentDirectory);
        }
        catch (OperationCanceledException) when (_stop.IsCancellationRequested) { }
        catch (Exception ex) { ShowError(ex.Message); }
    }

    private async void DirectoryList_DoubleClick(object sender, MouseButtonEventArgs e)
    {
        if (DirectoryListBox.SelectedItem is DirectoryEntry item) await NavigateDirectoryAsync(item.Path);
    }
    private async void DirectoryUp_Click(object sender, RoutedEventArgs e) { if (!string.IsNullOrWhiteSpace(_parentDirectory)) await NavigateDirectoryAsync(_parentDirectory); }
    private async void DirectoryHome_Click(object sender, RoutedEventArgs e) { if (!string.IsNullOrWhiteSpace(_homeDirectory)) await NavigateDirectoryAsync(_homeDirectory); }
    private void DirectoryAccept_Click(object sender, RoutedEventArgs e)
    {
        if (string.IsNullOrWhiteSpace(_currentDirectory)) return;
        _directorySelected?.Invoke(_currentDirectory);
        CloseModal();
    }

    private void SetOutputDirectory(string path)
    {
        _ui.OutputDirectory = path;
        _ui.Save();
        UpdateOutputPathUi();
    }

    private void OutputPathBox_LostFocus(object sender, RoutedEventArgs e)
    {
        var path = OutputPathBox.Text.Trim();
        if (string.IsNullOrWhiteSpace(path)) path = UiSettings.DefaultOutputDirectory;
        SetOutputDirectory(path);
    }

    private void UpdateOutputPathUi()
    {
        OutputPathBox.Text = _ui.OutputDirectory;
        SettingsPathText.Text = _ui.OutputDirectory;
        OutputPathSideText.Text = _ui.OutputDirectory;
        try
        {
            var root = Path.GetPathRoot(_ui.OutputDirectory);
            var drive = string.IsNullOrWhiteSpace(root) ? null : new DriveInfo(root);
            DiskFreeText.Text = drive is { IsReady: true } ? $"剩余 {drive.AvailableFreeSpace / 1073741824d:0.#} GB" : "保存到本地磁盘";
        }
        catch { DiskFreeText.Text = "保存到本地磁盘"; }
    }

    private void UiOption_Changed(object sender, RoutedEventArgs e)
    {
        if (_loadingControls) return;
        _ui.Notify = NotifyCheck.IsChecked == true;
        _ui.AutoParse = AutoParseCheck.IsChecked == true;
        _ui.SourceLanguage = Selected(SourceLanguageBox);
        _ui.Save();
    }

    private async void ConcurrencySlider_ValueChanged(object sender, RoutedPropertyChangedEventArgs<double> e)
    {
        if (ConcurrencyLabel is null) return;
        ConcurrencyLabel.Text = $"最多 {(int)e.NewValue} 个任务并行";
        if (!_loadingControls) await SaveEngineConfigAsync();
    }

    private void Accent_Click(object sender, RoutedEventArgs e)
    {
        if (sender is not Button { Tag: string accent }) return;
        _ui.Accent = accent;
        _ui.Save();
        ApplyAppearance();
    }

    private void Theme_Changed(object sender, SelectionChangedEventArgs e)
    {
        if (_loadingControls) return;
        _ui.Theme = Selected(ThemeBox, "auto");
        _ui.Save();
        ApplyThemeWithTransition();
    }

    private void ThemeButton_Click(object sender, RoutedEventArgs e)
    {
        if (sender is not Button { Tag: string theme }) return;
        _ui.Theme = theme;
        _ui.Save();
        WithControlLoading(() => Select(ThemeBox, theme, "auto"));
        ApplyThemeWithTransition();
    }

    private void ApplyThemeWithTransition()
    {
        if (!IsLoaded || !SystemParameters.ClientAreaAnimation || RootGrid.ActualWidth <= 0 || RootGrid.ActualHeight <= 0)
        {
            ApplyAppearance();
            return;
        }

        ThemeTransitionLayer.Visibility = Visibility.Collapsed;
        ThemeTransitionLayer.BeginAnimation(OpacityProperty, null);
        var dpi = VisualTreeHelper.GetDpi(RootGrid);
        var snapshot = new RenderTargetBitmap(
            Math.Max(1, (int)Math.Ceiling(RootGrid.ActualWidth * dpi.DpiScaleX)),
            Math.Max(1, (int)Math.Ceiling(RootGrid.ActualHeight * dpi.DpiScaleY)),
            dpi.PixelsPerInchX, dpi.PixelsPerInchY, PixelFormats.Pbgra32);
        snapshot.Render(RootGrid);
        snapshot.Freeze();
        ThemeTransitionLayer.Source = snapshot;
        ThemeTransitionLayer.Opacity = 1;
        ThemeTransitionLayer.Visibility = Visibility.Visible;
        ApplyAppearance();
        var fade = new DoubleAnimation(0, TimeSpan.FromMilliseconds(240)) { EasingFunction = EaseOut };
        fade.Completed += (_, _) =>
        {
            ThemeTransitionLayer.Visibility = Visibility.Collapsed;
            ThemeTransitionLayer.Source = null;
        };
        ThemeTransitionLayer.BeginAnimation(OpacityProperty, fade);
    }

    private void BackgroundOpacity_ValueChanged(object sender, RoutedPropertyChangedEventArgs<double> e)
    {
        if (BackgroundOpacityText is null) return;
        BackgroundOpacityText.Text = $"{(int)e.NewValue}%";
        if (_loadingControls) return;
        _ui.BackgroundOpacity = (int)e.NewValue;
        _ui.Save();
        ApplyAppearance();
    }

    private async void YtDlpCheck_Click(object sender, RoutedEventArgs e)
    {
        YtDlpCheckButton.IsEnabled = false;
        YtDlpCheckLabel.Text = "检查中…";
        try
        {
            if (!await _engine.EnsureConnectedAsync(true, _stop.Token))
                throw new InvalidOperationException("本地视频引擎尚未连接");
            var result = await _engine.CheckYtDlpAsync(_stop.Token);
            if (!result.Ok) ShowToast("检查未完成", result.Error);
            else if (!string.IsNullOrWhiteSpace(result.Pending)) ShowToast($"新版 {result.Pending} 已准备好", "当前解析和下载全部结束后自动生效");
            else if (!string.IsNullOrWhiteSpace(result.Installed)) ShowToast($"解析引擎 {result.Installed} 已就绪", "跟随 yt-dlp 官方 GitHub 发布，已校验完整性");
            else ShowToast($"解析引擎已是最新 {result.LatestSeen}");
            await RefreshYtDlpStatusAsync();
        }
        catch (OperationCanceledException) when (_stop.IsCancellationRequested) { }
        catch (Exception ex) { ShowToast("检查失败", FriendlyError(ex.Message)); }
        finally
        {
            YtDlpCheckButton.IsEnabled = true;
            YtDlpCheckLabel.Text = "检查更新";
        }
    }

    private async Task RefreshYtDlpStatusAsync()
    {
        try
        {
            var status = await _engine.GetYtDlpStatusAsync(_stop.Token);
            var active = string.IsNullOrWhiteSpace(status.Active) ? "版本未知" : $"当前 {status.Active}";
            var latest = string.IsNullOrWhiteSpace(status.LatestSeen) || status.LatestSeen == status.Active
                ? "" : $" · 上游 {status.LatestSeen}";
            var pending = string.IsNullOrWhiteSpace(status.Pending) ? "" : $" · {status.Pending} 等待下载结束后生效";
            YtDlpStatusText.Text = $"GitHub 官方发布 · {active}{latest}{pending}";
        }
        catch (OperationCanceledException) when (_stop.IsCancellationRequested) { }
        catch (Exception)
        {
            YtDlpStatusText.Text = "自动更新已开启 · 暂未取得版本";
        }
    }

    private void ApplyUiSettings()
    {
        WithControlLoading(() =>
        {
            NotifyCheck.IsChecked = _ui.Notify;
            AutoParseCheck.IsChecked = _ui.AutoParse;
            Select(SourceLanguageBox, _ui.SourceLanguage);
            RecognizeCheck.IsChecked = _ui.Recognize;
            TranslateCheck.IsChecked = _ui.Recognize && _ui.Translate;
            BurnCheck.IsChecked = _ui.Burn;
            BilingualCheck.IsChecked = _ui.Bilingual;
            Select(FontNameBox, _ui.FontName);
            Select(FontSizeBox, _ui.FontSize.ToString(), "18");
            Select(SubtitlePositionBox, _ui.SubtitlePosition, "bottom");
            Select(SubtitleColorBox, _ui.SubtitleColor, "custom");
            SubtitleCustomColorBox.Text = _ui.SubtitleColor;
            VerticalOffsetSlider.Value = Math.Clamp(_ui.SubtitleVerticalOffset, -120, 120);
            OutlineSlider.Value = Math.Clamp(_ui.Outline, 0, 8);
            Select(ThemeBox, _ui.Theme, "auto");
            BackgroundOpacitySlider.Value = Math.Clamp(_ui.BackgroundOpacity, 40, 100);
            Select(WhisperParseBox, "small");
            Select(WhisperSettingsBox, "small");
            Select(TranslationParseBox, "null");
            Select(TranslationSettingsBox, "null");
            Select(LlmVendorParseBox, "deepseek");
            Select(LlmVendorSettingsBox, "deepseek");
            Select(AsrProviderParseBox, "faster-whisper");
            Select(AsrProviderSettingsBox, "faster-whisper");
            Select(AsrVendorParseBox, "siliconflow");
            Select(AsrVendorSettingsBox, "siliconflow");
            Select(ProxyModeBox, "system");
            ConcurrencySlider.Value = 2;
        });
        ApplyLlmPreset("deepseek");
        ApplyAsrPreset("siliconflow");
        UpdateOutputPathUi();
        SyncDependentControls();
        ApplyAppearance();
        UpdateSubmitHint();
    }

    private void ApplyAppearance()
    {
        var accent = _ui.Accent switch
        {
            "jade" => ("#68D5B5", "#2468D5B5"),
            "amber" => ("#E4A451", "#24E4A451"),
            "iris" => ("#8CA8DC", "#248CA8DC"),
            _ => ("#3050F0", "#243050F0")
        };
        SetBrush("AccentBrush", accent.Item1);
        SetBrush("AccentSoftBrush", accent.Item2);
        SetBrush("PrimaryInkBrush", _ui.Accent is "jade" or "amber" or "iris" ? "#142152" : "#FFFFFF");
        var accentColor = ((SolidColorBrush)Resources["AccentBrush"]).Color;
        Resources["SwitchOnBrush"] = AlphaBrush(.32, accentColor.R, accentColor.G, accentColor.B);
        Resources["PrimaryGradient"] = new LinearGradientBrush(
            Color.FromRgb((byte)(accentColor.R * .88 + 255 * .12), (byte)(accentColor.G * .88 + 255 * .12), (byte)(accentColor.B * .88 + 255 * .12)),
            accentColor, 90);

        var light = _ui.Theme == "light" || (_ui.Theme == "auto" && SystemUsesLightTheme());
        ApplyNativeBackdrop();
        var t = (Math.Clamp(_ui.BackgroundOpacity, 40, 100) - 40d) / 60d;
        var strength = Math.Pow(t, 1.7);
        var mist = .15 + .85 * strength;
        Opacity = 1;
        MistLayer.Opacity = mist;
        Background = Brushes.Transparent;
        if (light)
        {
            Resources["WindowBrush"] = AlphaBrush(.12 * (.3 + .7 * strength), 252, 253, 254);
            SetBrush("RailBrush", "#091A202A");
            SetBrush("PanelBrush", "#0C1A202A");
            SetBrush("CardBrush", "#0C1A202A");
            SetBrush("InputBrush", "#18FFFFFF");
            SetBrush("DialogBrush", "#FFF7F8FA");
            SetBrush("TextBrush", "#303844");
            SetBrush("MutedBrush", "#687383");
            SetBrush("SecondaryTextBrush", "#586473");
            SetBrush("BorderBrush", "#171A202A");
            SetBrush("BorderStrongBrush", "#2B1A202A");
            SetBrush("ToastBrush", "#E0FAFBFD");
            BackgroundVeil.Fill = VerticalGradient("#F6F7F9", "#EEF0F3", "#E8EAEE");
            BackgroundVeil.Opacity = .18 + .42 * strength;
            BackgroundImage.Source = _lightArtwork ??= LoadBlendArtwork(true);
            BackgroundImage.Opacity = .15 * (.25 + .75 * strength) * .5;
            VignetteLayer.Opacity = 0;
        }
        else
        {
            Resources["WindowBrush"] = AlphaBrush(.10 * (.3 + .7 * strength), 13, 17, 24);
            SetBrush("RailBrush", "#09FFFFFF");
            SetBrush("PanelBrush", "#0CFFFFFF");
            SetBrush("CardBrush", "#0CFFFFFF");
            SetBrush("InputBrush", "#73090C12");
            SetBrush("DialogBrush", "#FF20252E");
            SetBrush("TextBrush", "#EAEFF4");
            SetBrush("MutedBrush", "#818A94");
            SetBrush("SecondaryTextBrush", "#ABB2BA");
            SetBrush("BorderBrush", "#17FFFFFF");
            SetBrush("BorderStrongBrush", "#2BFFFFFF");
            SetBrush("ToastBrush", "#C711161E");
            BackgroundVeil.Fill = VerticalGradient("#2C3033", "#292D30", "#25292C");
            BackgroundVeil.Opacity = .18 + .46 * strength;
            BackgroundImage.Source = _darkArtwork ??= LoadBlendArtwork(false);
            BackgroundImage.Opacity = .30 * (.25 + .75 * strength);
            VignetteLayer.Opacity = 1;
        }
        ModalOverlay.Resources["CardBrush"] = Resources["DialogBrush"];
        GrainLayer.Opacity = .045;

        foreach (var button in new[] { ThemeDarkButton, ThemeLightButton, ThemeAutoButton })
        {
            var selected = Equals(button.Tag, _ui.Theme);
            button.Background = selected ? (Brush)Resources["AccentSoftBrush"] : Brushes.Transparent;
            button.Foreground = selected ? (Brush)Resources["TextBrush"] : (Brush)Resources["MutedBrush"];
        }
        foreach (var button in new[] { AccentIconButton, AccentJadeButton, AccentAmberButton, AccentIrisButton })
        {
            button.BorderThickness = new Thickness(2);
            button.BorderBrush = Equals(button.Tag, _ui.Accent)
                ? (Brush)Resources["TextBrush"]
                : Brushes.Transparent;
        }
        ShowView(_currentView);
    }

    private static SolidColorBrush AlphaBrush(double opacity, byte red, byte green, byte blue) =>
        new(Color.FromArgb((byte)Math.Round(255 * opacity), red, green, blue));

    private static LinearGradientBrush VerticalGradient(string top, string middle, string bottom) => new(
        new GradientStopCollection { new(Brush(top).Color, 0), new(Brush(middle).Color, .52), new(Brush(bottom).Color, 1) },
        new Point(0, 0), new Point(0, 1));

    private void SetBrush(string key, string color) => Resources[key] = Brush(color);
    private static SolidColorBrush Brush(string value) => new((Color)ColorConverter.ConvertFromString(value));
    private static bool SystemUsesLightTheme() => Convert.ToInt32(Registry.GetValue(
        @"HKEY_CURRENT_USER\Software\Microsoft\Windows\CurrentVersion\Themes\Personalize", "AppsUseLightTheme", 0)) != 0;

    private async void Refresh_Click(object sender, RoutedEventArgs e)
    {
        await RefreshTasksAsync();
        ShowToast("已刷新任务列表");
    }

    private void UrlBox_KeyDown(object sender, KeyEventArgs e)
    {
        if (e.Key != Key.Enter) return;
        e.Handled = true;
        _ = ProbeAsync();
    }

    private void UrlBox_TextChanged(object sender, TextChangedEventArgs e)
    {
        Interlocked.Increment(ref _probeSequence);
        _probeStop?.Cancel();
        _bilibiliLoginStop?.Cancel();
        _probe = null;
        if (UrlPlaceholder is not null)
            UrlPlaceholder.Visibility = string.IsNullOrWhiteSpace(UrlBox.Text) ? Visibility.Visible : Visibility.Collapsed;
        if (ProbePanel is not null) ProbePanel.Visibility = Visibility.Collapsed;
        if (ParseFailure is not null) ParseFailure.Visibility = Visibility.Collapsed;
        if (ProbeFormats is not null) ProbeFormats.Clear();
        if (ProbeThumbnail is not null) ProbeThumbnail.Source = null;
        _previewStop?.Cancel();
        SyncPlatformLoginControls();
        if (ProbeButton is not null && ParseLoading is not null) SetProbeBusy(false);
    }

    private async void Example_Click(object sender, RoutedEventArgs e)
    {
        if (sender is not Button { Tag: string url }) return;
        UrlBox.Text = url;
        await ProbeAsync();
    }

    private void WindowControls_MouseEnter(object sender, MouseEventArgs e) => SetWindowGlyphs(.84);
    private void WindowControls_MouseLeave(object sender, MouseEventArgs e) => SetWindowGlyphs(0);

    private void SetWindowGlyphs(double opacity)
    {
        var duration = TimeSpan.FromMilliseconds(120);
        foreach (var glyph in new[] { WMinGlyph, WMaxGlyph, WCloseGlyph })
            glyph.BeginAnimation(OpacityProperty, new DoubleAnimation(opacity, duration));
    }

    private void WindowMin_Click(object sender, RoutedEventArgs e) => SystemCommands.MinimizeWindow(this);

    private void WindowMax_Click(object sender, RoutedEventArgs e)
    {
        if (WindowState == WindowState.Maximized) SystemCommands.RestoreWindow(this);
        else SystemCommands.MaximizeWindow(this);
    }

    private async void WindowClose_Click(object sender, RoutedEventArgs e)
    {
        if (_windowClosing) return;
        if (!ConfirmDiscardSubtitleChanges()) return;
        _windowClosing = true;
        if (!SystemParameters.ClientAreaAnimation) { Close(); return; }
        RootGrid.RenderTransformOrigin = new Point(.5, .5);
        RootGrid.RenderTransform = new ScaleTransform(1, 1);
        var duration = TimeSpan.FromMilliseconds(280);
        RootGrid.BeginAnimation(OpacityProperty, new DoubleAnimation(0, duration) { EasingFunction = CssEase });
        ((ScaleTransform)RootGrid.RenderTransform).BeginAnimation(ScaleTransform.ScaleXProperty, new DoubleAnimation(.985, duration) { EasingFunction = CssEase });
        ((ScaleTransform)RootGrid.RenderTransform).BeginAnimation(ScaleTransform.ScaleYProperty, new DoubleAnimation(.985, duration) { EasingFunction = CssEase });
        await Task.Delay(duration);
        Close();
    }

    private void Window_StateChanged(object? sender, EventArgs e)
    {
        if (WMaxGlyph is not null)
            WMaxGlyph.Text = WindowState == WindowState.Maximized ? "↙" : "↗";
        UpdateWindowClip();
    }

    private void Window_SizeChanged(object sender, SizeChangedEventArgs e)
    {
        if (!IsLoaded) return;
        UpdateWindowClip();
        UpdateResponsiveLayout();
        LayoutBackground();
    }

    private void UpdateWindowClip()
    {
        if (RootGrid is null || RootGrid.ActualWidth <= 0 || RootGrid.ActualHeight <= 0) return;
        RootGrid.Clip = WindowState == WindowState.Maximized
            ? null
            : new RectangleGeometry(new Rect(0, 0, RootGrid.ActualWidth, RootGrid.ActualHeight), 12, 12);
    }

    private void UpdateResponsiveLayout()
    {
        var width = RootGrid.ActualWidth;
        var compact = width <= 900;
        ParseLayout.MinHeight = Math.Max(0, RootGrid.ActualHeight - 110);
        ParseHeading.FontSize = ProbePanel.Visibility == Visibility.Visible || ParseLoading.Visibility == Visibility.Visible
            ? 24 : Math.Clamp(width * .034, 28, 40);
        ParseHeading.LineHeight = ParseHeading.FontSize * 1.15;
        _compactLayout = compact;
        RailColumn.Width = new GridLength(compact ? 0 : 224);
        CompactNavRow.Height = new GridLength(compact ? 70 : 0);
        Grid.SetRow(Rail, compact ? 1 : 0);
        Grid.SetColumn(Rail, 0);
        Grid.SetColumnSpan(Rail, compact ? 2 : 1);
        Rail.BorderThickness = compact ? new Thickness(0, 1, 0, 0) : new Thickness(0);
        Rail.BorderBrush = Resources["BorderBrush"] as Brush;
        RailLayout.Margin = compact ? new Thickness(12, 8, 12, 8) : new Thickness(14, 20, 14, 16);
        RailLayout.RowDefinitions[0].Height = compact ? new GridLength(0) : GridLength.Auto;
        RailLayout.RowDefinitions[1].Height = compact ? new GridLength(1, GridUnitType.Star) : GridLength.Auto;
        RailLayout.RowDefinitions[2].Height = compact ? new GridLength(0) : new GridLength(1, GridUnitType.Star);
        RailLayout.RowDefinitions[3].Height = compact ? new GridLength(0) : GridLength.Auto;
        LogoPanel.Visibility = DiskPanel.Visibility = compact ? Visibility.Collapsed : Visibility.Visible;
        NavPanel.Orientation = compact ? Orientation.Horizontal : Orientation.Vertical;
        var buttons = new[] { ParseNav, QueueNav, DoneNav, SettingsNav };
        var compactWidth = Math.Max(1, (width - 24) / buttons.Length);
        foreach (var button in buttons)
        {
            button.Width = compact ? compactWidth : double.NaN;
            button.Height = compact ? 52 : 42;
            button.Margin = compact ? new Thickness(0) : new Thickness(0, 0, 0, 6);
            button.HorizontalContentAlignment = compact ? HorizontalAlignment.Center : HorizontalAlignment.Left;
            button.FontSize = compact ? 11 : 14;
            if (button.Content is DockPanel content && content.Children[0] is Viewbox icon)
            {
                // The original narrow layout puts the icon above its label.
                content.LastChildFill = false;
                DockPanel.SetDock(icon, compact ? Dock.Top : Dock.Left);
                icon.Margin = compact ? new Thickness(0, 0, 0, 3) : new Thickness(0, 0, 11, 0);
                icon.HorizontalAlignment = HorizontalAlignment.Center;
                foreach (var label in content.Children.OfType<TextBlock>())
                    label.HorizontalAlignment = compact ? HorizontalAlignment.Center : HorizontalAlignment.Left;
            }
        }
        NavThumb.Width = compact ? compactWidth : double.NaN;
        NavThumb.HorizontalAlignment = compact ? HorizontalAlignment.Left : HorizontalAlignment.Stretch;
        NavThumb.Height = compact ? 52 : 42;
        PageHost.Margin = compact ? new Thickness(18, 54, 18, 30) : new Thickness(40, 60, 40, 56);
        var padding = PageHost.Margin;
        ParseScroller.Margin = SettingsScroller.Margin = new Thickness(-padding.Left, -padding.Top, -padding.Right, -padding.Bottom);
        ParseLayout.Margin = SettingsContent.Margin = padding;
        var contentWidth = ParseLayout.ActualWidth > 1 ? ParseLayout.ActualWidth : Math.Max(1, width - (compact ? 36 : 80));
        var previewWidth = Math.Max(1, contentWidth - 36);
        SkeletonThumbColumn.Width = compact ? new GridLength(1, GridUnitType.Star) : new GridLength(264);
        SkeletonGapColumn.Width = compact ? new GridLength(0) : new GridLength(20);
        SkeletonLayout.ColumnDefinitions[2].Width = compact ? new GridLength(0) : new GridLength(1, GridUnitType.Star);
        Grid.SetColumn(SkeletonLines, compact ? 0 : 2);
        Grid.SetRow(SkeletonLines, compact ? 1 : 0);
        SkeletonLines.Margin = compact ? new Thickness(0, 20, 0, 0) : new Thickness(0, 8, 0, 0);
        SkeletonThumb.Height = compact ? previewWidth * 9 / 16 : 148;

        ProbeThumbColumn.Width = new GridLength(compact ? 140 : 286);
        ProbePreviewLayout.ColumnDefinitions[1].Width = new GridLength(1, GridUnitType.Star);
        Grid.SetColumn(ProbePreviewBody, 1);
        Grid.SetRow(ProbePreviewBody, 0);
        ProbePreviewBody.Margin = new Thickness(0);
        ProbePreviewThumb.Width = compact ? 120 : 264;
        ProbePreviewThumb.Height = compact ? 67.5 : 148;
        ProbePreviewThumb.HorizontalAlignment = HorizontalAlignment.Left;

        DirectoryDialog.Width = Math.Min(560, width * .92);
        SubtitleDialog.Width = Math.Min(940, width * .94);
        DeleteDialog.Width = Math.Min(520, width * .92);
        ToastHost.Margin = compact ? new Thickness(0, 0, 14, 76) : new Thickness(0, 0, 22, 22);
        MoveNavThumb(false);
    }

    private void Window_PreviewKeyDown(object sender, KeyEventArgs e)
    {
        if (e.Key == Key.Escape && ModalOverlay.Visibility == Visibility.Visible)
        {
            CloseModal();
            e.Handled = true;
            return;
        }
        if (e.Key != Key.V || Keyboard.Modifiers != ModifierKeys.Control ||
            Keyboard.FocusedElement is TextBoxBase or PasswordBox) return;
        try
        {
            var value = Clipboard.GetText().Trim();
            var url = NormalizeUrl(value);
            if (!Uri.TryCreate(url, UriKind.Absolute, out var uri) || uri.Scheme is not ("http" or "https")) return;
            UrlBox.Text = url;
            ShowView("parse");
            if (_ui.AutoParse) _ = ProbeAsync();
            else UrlBox.Focus();
            e.Handled = true;
        }
        catch { }
    }

    private void Nav_Click(object sender, RoutedEventArgs e)
    {
        if (sender is Button { Tag: string view }) ShowView(view);
    }

    private void ShowView(string view)
    {
        var sequence = ++_viewAnimationSequence;
        var views = new[] { ParseView, QueueView, DoneView, SettingsView };
        var current = views.FirstOrDefault(candidate => candidate.Visibility == Visibility.Visible) ?? ParseView;
        var next = ViewFor(view);
        var changed = current != next;
        _currentView = view;
        var labels = view switch
        {
            "queue" => ("传输队列", "实时查看每一步进度，可随时取消"),
            "done" => ("已完成", "查看和管理已完成的下载"),
            "settings" => ("设置", "下载、识别、翻译、网络与外观"),
            _ => ("解析", "粘贴链接，选画质，开始下载")
        };
        PageTitle.Text = labels.Item1;
        PageSubtitle.Text = labels.Item2;
        var navButtons = new[] { ParseNav, QueueNav, DoneNav, SettingsNav };
        var navIcons = new[] { ParseNavIcon, QueueNavIcon, DoneNavIcon, SettingsNavIcon };
        for (var i = 0; i < navButtons.Length; i++)
        {
            var active = (string?)navButtons[i].Tag == view;
            navButtons[i].Background = Brushes.Transparent;
            navButtons[i].Foreground = active ? Resources["TextBrush"] as Brush : Resources["MutedBrush"] as Brush;
            navIcons[i].Stroke = active ? Resources["AccentBrush"] as Brush : Resources["MutedBrush"] as Brush;
        }
        if (changed || !NavThumbMove.HasAnimatedProperties)
            MoveNavThumb(changed && SystemParameters.ClientAreaAnimation);
        foreach (var candidate in views)
        {
            ResetView(candidate);
            candidate.Visibility = candidate == next ? Visibility.Visible : Visibility.Collapsed;
        }
        if (changed && SystemParameters.ClientAreaAnimation)
        {
            // 页面先可见、可操作，过渡不能让点击等待旧页退场。
            next.RenderTransform = new TranslateTransform(0, 3);
            var enter = new DoubleAnimation(0, TimeSpan.FromMilliseconds(100)) { EasingFunction = EaseOut };
            enter.Completed += (_, _) => { if (sequence == _viewAnimationSequence) ResetView(next); };
            ((TranslateTransform)next.RenderTransform).BeginAnimation(TranslateTransform.YProperty, enter);
        }
        if (changed && view is "queue" or "done") QueueRefresh();
        if (changed && view == "settings")
        {
            if (File.Exists(UiSettings.NotificationStatusPath))
            {
                try
                {
                    using var status = System.Text.Json.JsonDocument.Parse(File.ReadAllText(UiSettings.NotificationStatusPath));
                    NotifyStatusText.Text = status.RootElement.GetProperty("message").GetString() ?? "软件内提示与 Windows 系统通知";
                }
                catch (Exception) { }
            }
            StartWhisperStatus(Selected(WhisperSettingsBox, "small"));
            StartGpuStatus();
        }
    }

    private FrameworkElement ViewFor(string view) => view switch
    {
        "queue" => QueueView,
        "done" => DoneView,
        "settings" => SettingsView,
        _ => ParseView
    };

    private static void ResetView(FrameworkElement view)
    {
        view.BeginAnimation(OpacityProperty, null);
        view.Opacity = 1;
        view.RenderTransform = Transform.Identity;
        view.Effect = null;
        view.IsHitTestVisible = true;
    }

    private static void AnimateCard(FrameworkElement card)
    {
        ResetView(card);
        if (!SystemParameters.ClientAreaAnimation) return;
        card.Opacity = 0;
        card.RenderTransform = new TranslateTransform(0, 30);
        card.BeginAnimation(OpacityProperty, new DoubleAnimation(1, TimeSpan.FromMilliseconds(600)) { EasingFunction = ResultEase });
        ((TranslateTransform)card.RenderTransform).BeginAnimation(TranslateTransform.YProperty,
            new DoubleAnimation(0, TimeSpan.FromMilliseconds(500)) { BeginTime = TimeSpan.FromMilliseconds(100), EasingFunction = ResultForegroundEase });
    }

    private void MoveNavThumb(bool animate)
    {
        var index = _currentView switch { "queue" => 1, "done" => 2, "settings" => 3, _ => 0 };
        var x = _compactLayout ? index * NavThumb.Width : 0;
        var y = _compactLayout ? 0 : index * 48;
        var fromX = NavThumbMove.X;
        var fromY = NavThumbMove.Y;
        NavThumbMove.BeginAnimation(TranslateTransform.XProperty, null);
        NavThumbMove.BeginAnimation(TranslateTransform.YProperty, null);
        if (!animate || (Math.Abs(fromX - x) < .01 && Math.Abs(fromY - y) < .01))
        {
            NavThumbMove.X = x;
            NavThumbMove.Y = y;
            return;
        }
        NavThumbMove.X = fromX;
        NavThumbMove.Y = fromY;
        NavThumbMove.BeginAnimation(TranslateTransform.XProperty, new DoubleAnimation(fromX, x, TimeSpan.FromMilliseconds(300)) { EasingFunction = EaseInOut });
        NavThumbMove.BeginAnimation(TranslateTransform.YProperty, new DoubleAnimation(fromY, y, TimeSpan.FromMilliseconds(300)) { EasingFunction = EaseInOut });
    }

    private void OpenModal(FrameworkElement dialog)
    {
        if (!ReferenceEquals(dialog, SubtitleDialog) && SubtitleDialog.Visibility == Visibility.Visible &&
            (_subtitleBusy || !ConfirmDiscardSubtitleChanges())) return;
        if (!ReferenceEquals(dialog, SubtitleDialog)) CancelSubtitleLoad();
        _modalSequence++;
        DirectoryDialog.Visibility = SubtitleDialog.Visibility = DeleteDialog.Visibility = Visibility.Collapsed;
        dialog.Visibility = Visibility.Visible;
        ModalOverlay.Visibility = Visibility.Visible;
        ModalOverlay.BeginAnimation(OpacityProperty, null);
        dialog.BeginAnimation(OpacityProperty, null);
        ModalOverlay.Opacity = SystemParameters.ClientAreaAnimation ? 0 : 1;
        dialog.Opacity = SystemParameters.ClientAreaAnimation ? 0 : 1;
        dialog.RenderTransformOrigin = new Point(.5, .5);
        var transforms = new TransformGroup();
        transforms.Children.Add(new ScaleTransform(.97, .97));
        transforms.Children.Add(new TranslateTransform(0, 8));
        dialog.RenderTransform = transforms;
        if (!SystemParameters.ClientAreaAnimation) return;
        ModalOverlay.BeginAnimation(OpacityProperty, new DoubleAnimation(1, TimeSpan.FromMilliseconds(180)));
        dialog.BeginAnimation(OpacityProperty, new DoubleAnimation(1, TimeSpan.FromMilliseconds(300)) { EasingFunction = EaseOut });
        ((ScaleTransform)transforms.Children[0]).BeginAnimation(ScaleTransform.ScaleXProperty, new DoubleAnimation(1, TimeSpan.FromMilliseconds(300)) { EasingFunction = EaseOut });
        ((ScaleTransform)transforms.Children[0]).BeginAnimation(ScaleTransform.ScaleYProperty, new DoubleAnimation(1, TimeSpan.FromMilliseconds(300)) { EasingFunction = EaseOut });
        ((TranslateTransform)transforms.Children[1]).BeginAnimation(TranslateTransform.YProperty, new DoubleAnimation(0, TimeSpan.FromMilliseconds(300)) { EasingFunction = EaseOut });
    }

    private void CloseModal_Click(object sender, RoutedEventArgs e) => CloseModal();
    private void CloseModal()
    {
        if (_subtitleBusy && SubtitleDialog.Visibility == Visibility.Visible) return;
        if (!ConfirmDiscardSubtitleChanges()) return;
        CancelSubtitleLoad();
        _directorySelected = null;
        if (ModalOverlay.Visibility != Visibility.Visible) return;
        var modalSequence = ++_modalSequence;
        if (!SystemParameters.ClientAreaAnimation) { FinishCloseModal(); return; }
        var fade = new DoubleAnimation(0, TimeSpan.FromMilliseconds(150));
        fade.Completed += (_, _) => { if (modalSequence == _modalSequence) FinishCloseModal(); };
        ModalOverlay.BeginAnimation(OpacityProperty, fade);
    }

    private bool ConfirmDiscardSubtitleChanges()
    {
        if (SubtitleDialog.Visibility != Visibility.Visible || _editingArtifact is null ||
            SubtitleTextBox.Text == _subtitleSavedText) return true;
        var choice = MessageBox.Show(this, "字幕还有未保存的修改。要丢弃修改并关闭吗？", "未保存的字幕",
            MessageBoxButton.YesNo, MessageBoxImage.Warning, MessageBoxResult.No);
        if (choice != MessageBoxResult.Yes) return false;
        SubtitleTextBox.Text = _subtitleSavedText;
        _subtitleDocument = null;
        return true;
    }

    private void CancelSubtitleLoad()
    {
        _subtitleLoadSequence++;
        _subtitleLoadStop?.Cancel();
    }

    private void FinishCloseModal()
    {
        ModalOverlay.Visibility = Visibility.Collapsed;
        ModalOverlay.BeginAnimation(OpacityProperty, null);
        ModalOverlay.Opacity = 1;
        DirectoryDialog.Visibility = SubtitleDialog.Visibility = DeleteDialog.Visibility = Visibility.Collapsed;
    }

    private void SetConnection(bool connected, string text)
    {
        if (!Dispatcher.CheckAccess()) { Dispatcher.Invoke(() => SetConnection(connected, text)); return; }
        ConnectionDot.Fill = connected ? Brush("#48CF8B") : Brush("#E9A23B");
        ConnectionText.Text = text;
        ConnectionText.ToolTip = connected || string.IsNullOrWhiteSpace(_engine.ConnectionHint) ? null : _engine.ConnectionHint;
    }

    private void ShowError(string message)
    {
        if (!Dispatcher.CheckAccess()) { Dispatcher.Invoke(() => ShowError(message)); return; }
        ShowToast("操作失败", FriendlyError(message));
    }

    private static string FriendlyError(string message) =>
        message.Contains("sending the request", StringComparison.OrdinalIgnoreCase)
            ? "本地视频引擎连接刚刚中断，正在自动恢复，请稍后重试。"
            : message;

    private static void ClearError() { }

    private void NotifyCompleted(TaskItem task)
    {
        ShowToast("下载已完成", task.DisplayTitle);
    }

    internal void ShowCompleted() => ShowView("done");

    private void ShowToast(string title, string detail = "")
    {
        if (!Dispatcher.CheckAccess()) { Dispatcher.Invoke(() => ShowToast(title, detail)); return; }
        var iconPath = new System.Windows.Shapes.Path
        {
            Data = title.Contains("失败") || title.Contains("错误") || title.Contains("未完成")
                ? Geometry.Parse("M3,12 A9,9 0 1 0 21,12 A9,9 0 1 0 3,12 M12,9 L12.01,9 M11,12 L12,12 L12,16 L13,16")
                : (Geometry)FindResource("IconCheck"),
            Fill = Brushes.Transparent, StrokeThickness = 1.8,
            StrokeStartLineCap = PenLineCap.Round, StrokeEndLineCap = PenLineCap.Round, StrokeLineJoin = PenLineJoin.Round
        };
        iconPath.SetResourceReference(System.Windows.Shapes.Shape.StrokeProperty, "AccentBrush");
        var icon = new Border { Width = 30, Height = 30, CornerRadius = new CornerRadius(9), Child = new Viewbox { Width = 16, Height = 16, Child = iconPath } };
        icon.SetResourceReference(Border.BackgroundProperty, "AccentSoftBrush");
        var titleText = new TextBlock { Text = title, FontSize = 13.5, FontWeight = FontWeights.SemiBold };
        titleText.SetResourceReference(TextBlock.ForegroundProperty, "TextBrush");
        var detailText = new TextBlock { Text = detail, FontSize = 12, Margin = new Thickness(0, 2, 0, 0), TextWrapping = TextWrapping.Wrap };
        detailText.SetResourceReference(TextBlock.ForegroundProperty, "MutedBrush");
        detailText.Visibility = string.IsNullOrWhiteSpace(detail) ? Visibility.Collapsed : Visibility.Visible;
        var text = new StackPanel();
        text.Children.Add(titleText);
        text.Children.Add(detailText);
        var content = new Grid();
        content.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
        content.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
        content.Children.Add(icon);
        Grid.SetColumn(text, 1);
        text.Margin = new Thickness(11, 0, 0, 0);
        content.Children.Add(text);
        var scale = new ScaleTransform(SystemParameters.ClientAreaAnimation ? .98 : 1, SystemParameters.ClientAreaAnimation ? .98 : 1);
        var move = new TranslateTransform(0, SystemParameters.ClientAreaAnimation ? 14 : 0);
        var card = new Border { Width = 360, Padding = new Thickness(14, 13, 14, 13), CornerRadius = new CornerRadius(11), BorderThickness = new Thickness(1), Margin = new Thickness(0, ToastHost.Children.Count == 0 ? 0 : 10, 0, 0), RenderTransformOrigin = new Point(.5, 1), RenderTransform = new TransformGroup { Children = { scale, move } }, Child = content };
        card.SetResourceReference(Border.BackgroundProperty, "ToastBrush");
        card.SetResourceReference(Border.BorderBrushProperty, "BorderStrongBrush");
        ToastHost.Children.Add(card);
        card.Opacity = SystemParameters.ClientAreaAnimation ? 0 : 1;
        scale.ScaleX = scale.ScaleY = SystemParameters.ClientAreaAnimation ? .98 : 1;
        if (SystemParameters.ClientAreaAnimation)
        {
            var duration = TimeSpan.FromMilliseconds(240);
            card.BeginAnimation(OpacityProperty, new DoubleAnimation(1, duration) { EasingFunction = EaseOut });
            scale.BeginAnimation(ScaleTransform.ScaleXProperty, new DoubleAnimation(1, duration) { EasingFunction = EaseOut });
            scale.BeginAnimation(ScaleTransform.ScaleYProperty, new DoubleAnimation(1, duration) { EasingFunction = EaseOut });
            move.BeginAnimation(TranslateTransform.YProperty, new DoubleAnimation(0, duration) { EasingFunction = EaseOut });
        }
        _ = HideToastLaterAsync(card);
    }

    private async Task HideToastLaterAsync(Border card)
    {
        try
        {
            await Task.Delay(4200, _stop.Token);
            await Dispatcher.InvokeAsync(() => HideToast(card));
        }
        catch (OperationCanceledException) { }
    }

    private void HideToast(Border card)
    {
        if (!ToastHost.Children.Contains(card)) return;
        if (!SystemParameters.ClientAreaAnimation) { ToastHost.Children.Remove(card); return; }
        var transforms = (TransformGroup)card.RenderTransform;
        var scale = (ScaleTransform)transforms.Children[0];
        var move = (TranslateTransform)transforms.Children[1];
        var fade = new DoubleAnimation(0, TimeSpan.FromMilliseconds(150));
        fade.Completed += (_, _) => ToastHost.Children.Remove(card);
        card.BeginAnimation(OpacityProperty, fade);
        scale.BeginAnimation(ScaleTransform.ScaleXProperty, new DoubleAnimation(.98, TimeSpan.FromMilliseconds(150)));
        scale.BeginAnimation(ScaleTransform.ScaleYProperty, new DoubleAnimation(.98, TimeSpan.FromMilliseconds(150)));
        move.BeginAnimation(TranslateTransform.YProperty, new DoubleAnimation(6, TimeSpan.FromMilliseconds(150)));
    }

    private void WithControlLoading(Action action)
    {
        var previous = _loadingControls;
        _loadingControls = true;
        try { action(); }
        finally { _loadingControls = previous; }
    }

    private static string Selected(ComboBox box, string fallback = "") => box.SelectedValue?.ToString() ?? fallback;
    private static void Select(ComboBox box, string value, string fallback = "")
    {
        box.SelectedValue = value;
        if (box.SelectedIndex < 0 && !string.IsNullOrWhiteSpace(fallback)) box.SelectedValue = fallback;
        if (box.SelectedIndex < 0 && box.Items.Count > 0) box.SelectedIndex = 0;
    }
    private static int IntValue(string value, int fallback) => int.TryParse(value, out var parsed) ? parsed : fallback;
    private static string NormalizeUrl(string value)
    {
        var trimmed = value.Trim();
        var link = Regex.Match(trimmed, @"https?://[^\s<>""“”]+", RegexOptions.IgnoreCase);
        if (link.Success) trimmed = link.Value.TrimEnd('。', '，', '；', '！', '、', '）', '】', ')', ',', ';', '!');
        if (trimmed.StartsWith("www.", StringComparison.OrdinalIgnoreCase)) trimmed = $"https://{trimmed}";
        if (Uri.TryCreate(trimmed, UriKind.Absolute, out var uri) &&
            uri.Host is "douyin.com" or "www.douyin.com" or "m.douyin.com" or "iesdouyin.com" or "www.iesdouyin.com")
        {
            var video = Regex.Match(uri.AbsolutePath, @"/(?:share/)?video/(\d+)(?:/|$)");
            if (video.Success) return "https://www.douyin.com/video/" + video.Groups[1].Value;
            foreach (var part in uri.Query.TrimStart('?').Split('&'))
            {
                var pair = part.Split('=', 2);
                if (pair.Length == 2 && pair[0] is "modal_id" or "aweme_id" or "vid" or "item_id")
                {
                    var id = Uri.UnescapeDataString(pair[1]);
                    if (Regex.IsMatch(id, @"^\d+$")) return "https://www.douyin.com/video/" + id;
                }
            }
        }
        return trimmed;
    }

    private static bool IsDouyinFeedUrl(Uri uri) =>
        uri.Host is "douyin.com" or "www.douyin.com" or "m.douyin.com" &&
        uri.AbsolutePath.TrimEnd('/') is "" or "/jingxuan" or "/recommend";

    private static BitmapImage BitmapFromBytes(byte[] bytes)
    {
        using var stream = new MemoryStream(bytes);
        var bitmap = new BitmapImage();
        bitmap.BeginInit();
        bitmap.CacheOption = BitmapCacheOption.OnLoad;
        bitmap.StreamSource = stream;
        bitmap.EndInit();
        bitmap.Freeze();
        return bitmap;
    }

    private void ProbePreviewThumb_SizeChanged(object sender, SizeChangedEventArgs e)
    {
        ((Border)sender).Clip = new RectangleGeometry(new Rect(e.NewSize), 12, 12);
    }

    private async void LoadRemoteImage(Image image, string url, int sequence)
    {
        image.Source = null;
        if (!Uri.TryCreate(url, UriKind.Absolute, out var uri)) return;
        try
        {
            var bytes = await ThumbnailClient.GetByteArrayAsync(uri, _stop.Token);
            if (sequence != _probeSequence || _stop.IsCancellationRequested) return;
            var bitmap = await Task.Run(() => BitmapFromBytes(bytes), _stop.Token);
            if (sequence == _probeSequence && !_stop.IsCancellationRequested) RevealThumbnail(image, bitmap);
        }
        catch (Exception ex) when (ex is HttpRequestException or OperationCanceledException or FileFormatException or NotSupportedException or ArgumentException) { }
    }

    private static void RevealThumbnail(Image image, BitmapSource bitmap)
    {
        image.BeginAnimation(OpacityProperty, null);
        image.Source = bitmap;
        image.Opacity = 1;
        if (SystemParameters.ClientAreaAnimation)
            image.BeginAnimation(OpacityProperty,
                new DoubleAnimation(0, 1, TimeSpan.FromMilliseconds(600)) { EasingFunction = ResultEase });
    }

    private void Window_Closing(object? sender, System.ComponentModel.CancelEventArgs e)
    {
        // Alt+F4、系统关闭与自绘关闭按钮均要保留用户尚未保存的字幕修改。
        if (ConfirmDiscardSubtitleChanges()) return;
        e.Cancel = true;
        _windowClosing = false;
        RootGrid.BeginAnimation(OpacityProperty, null);
        RootGrid.Opacity = 1;
        RootGrid.RenderTransform = null;
    }

    private void Window_Closed(object? sender, EventArgs e)
    {
        _stop.Cancel();
        _previewStop?.Cancel();
        _whisperPollStop?.Cancel();
        _probeStop?.Cancel();
        _engine.Dispose();
    }
}

internal sealed class CubicBezierEase(double x1, double y1, double x2, double y2) : IEasingFunction
{
    public double Ease(double normalizedTime)
    {
        var u = normalizedTime;
        for (var i = 0; i < 6; i++)
        {
            var error = Bezier(u, x1, x2) - normalizedTime;
            var derivative = BezierDerivative(u, x1, x2);
            if (Math.Abs(error) < .0001 || Math.Abs(derivative) < .0001) break;
            u = Math.Clamp(u - error / derivative, 0, 1);
        }
        return Bezier(u, y1, y2);
    }

    private static double Bezier(double t, double p1, double p2)
    {
        var inverse = 1 - t;
        return 3 * inverse * inverse * t * p1 + 3 * inverse * t * t * p2 + t * t * t;
    }

    private static double BezierDerivative(double t, double p1, double p2)
    {
        var inverse = 1 - t;
        return 3 * inverse * inverse * p1 + 6 * inverse * t * (p2 - p1) + 3 * t * t * (1 - p2);
    }
}
