using System.Collections.ObjectModel;
using System.IO;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Media;
using System.Windows.Media.Imaging;
using Microsoft.Win32;

namespace Jiying.Desktop;

public partial class MainWindow : Window
{
    private readonly CancellationTokenSource _stop = new();
    private readonly CancellationToken _windowToken;
    private EngineClient _engine;
    private readonly UiSettings _ui = UiSettings.Load();
    private bool _windowInitialized;
    internal EngineClient Engine => _engine;
    internal UiSettings Settings => _ui;
    internal CancellationTokenSource WindowLifetime => _stop;

    internal void AttachEngine(EngineClient engine)
    {
        _engine.Dispose();
        _engine = engine;
    }

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
        DoneTaskItems.Collection = DoneTasks;
        _windowToken = _stop.Token;
        _engine = new EngineClient(Path.Combine(AppContext.BaseDirectory, "engine", "JiyingEngine.exe"));
        BuildLanguageChoices();
        EnsureSavedFontChoice();
        ApplyUiSettings();
        ShowView("parse");
        _loadingControls = false;
    }

    internal void Window_Loaded(object sender, RoutedEventArgs e) =>
        _ = RunWindowOperationAsync("窗口初始化", InitializeWindowAsync);

    private async Task InitializeWindowAsync()
    {
        if (_windowInitialized) return;
        _windowInitialized = true;
        ApplyAppearance();
        UpdateWindowClip();
        UpdateResponsiveLayout();
        LayoutBackground();
        SetConnection(false, "正在连接引擎");
        try
        {
            if (await _engine.EnsureConnectedAsync(true, _windowToken))
            {
                SetConnection(true, $"引擎已连接 · {_engine.Port}");
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
            DesktopDiagnostics.Write("引擎初始化", ex);
            SetConnection(false, "引擎连接失败");
            ShowError(ex.Message);
        }

        if (_stop.IsCancellationRequested) return;
        _ = RunWindowOperationAsync("引擎连接检查", () => PollLoopAsync(_windowToken), _windowToken);
        _ = RunWindowOperationAsync("下载事件监听", () => EventLoopAsync(_windowToken), _windowToken);
        if (_engine.IsConnected) await RefreshYtDlpStatusAsync();
    }

    private async Task PollLoopAsync(CancellationToken token)
    {
        var configConnection = -1L;
        var checkConnection = !_engine.IsConnected;
        while (!token.IsCancellationRequested)
        {
            try
            {
                if (checkConnection ? await _engine.EnsureConnectedAsync(true, token) : _engine.IsConnected)
                {
                    var version = _engine.ConnectionVersion;
                    SetConnection(true, $"引擎已连接 · {_engine.Port}");
                    var streamNeedsRecovery = checkConnection && (_streamConnection != version || !_streamSequenced);
                    if (_snapshotConnection != version || streamNeedsRecovery ||
                        _fullTaskRefresh || _changedTasks.Count > 0 || _progressUpdates.Count > 0)
                    {
                        if (_snapshotConnection != version || streamNeedsRecovery)
                            _fullTaskRefresh = true;
                        if (Environment.TickCount64 >= _taskRetryAfter)
                            await FlushTaskChangesAsync();
                    }
                    if (configConnection != version && await LoadConfigAsync(token))
                        configConnection = version;
                }
                else
                {
                    configConnection = -1;
                    SetConnection(false, string.IsNullOrWhiteSpace(_engine.ConnectionHint) ? "等待本地引擎" : "引擎未连接");
                }
            }
            catch (OperationCanceledException) when (token.IsCancellationRequested) { return; }
            catch (Exception error)
            {
                DesktopDiagnostics.Write("检查引擎连接", error);
                configConnection = -1;
                if (!_engine.IsConnected) SetConnection(false, "引擎已断开，正在重连");
            }

            checkConnection = true;
            try { await Task.Delay(TimeSpan.FromSeconds(5), token); }
            catch (OperationCanceledException) { return; }
        }
    }

    private static void Replace<T>(ObservableCollection<T> target, IEnumerable<T> items)
    {
        target.Clear();
        foreach (var item in items) target.Add(item);
    }

    internal void SetConnection(bool connected, string text)
    {
        if (_stop.IsCancellationRequested) return;
        if (!Dispatcher.CheckAccess()) { Dispatcher.Invoke(() => SetConnection(connected, text)); return; }
        ConnectionDot.Fill = connected ? Brush("#48CF8B") : Brush("#E9A23B");
        ConnectionText.Text = text;
        ConnectionText.ToolTip = connected || string.IsNullOrWhiteSpace(_engine.ConnectionHint) ? null : _engine.ConnectionHint;
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
        StopWheelScroll();
        _stop.Cancel();
        if (_taskUpdateTimer is not null)
        {
            _taskUpdateTimer.Stop();
            _taskUpdateTimer.Tick -= TaskUpdateTick;
        }
        _previewStop?.Dispose();
        _whisperPollStop?.Dispose();
        _gpuPollStop?.Dispose();
        _engine.Dispose();
        _stop.Dispose();
    }

    internal async Task RunWindowOperationAsync(string operation, Func<Task> action, CancellationToken expectedCancellation = default)
    {
        if (_stop.IsCancellationRequested) return;
        try { await action(); }
        catch (OperationCanceledException error) when (_windowToken.IsCancellationRequested ||
            expectedCancellation.IsCancellationRequested && error.InnerException is not TimeoutException) { }
        catch (Exception error)
        {
            DesktopDiagnostics.Write(operation, error);
            if (!_stop.IsCancellationRequested) ShowError(error.Message);
        }
    }
}
