using System.Collections.ObjectModel;
using System.IO;
using System.Net.Http;
using System.Text.RegularExpressions;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Input;
using System.Windows.Media;
using System.Windows.Media.Animation;
using System.Windows.Media.Imaging;

namespace Jiying.Desktop;

public partial class MainWindow
{
    private static readonly HttpClient ThumbnailClient = new() { Timeout = TimeSpan.FromSeconds(15) };
    private ProbeResult? _probe;
    internal ProbeResult? ProbeResult { get => _probe; set => _probe = value; }
    private CancellationTokenSource? _probeStop;
    private CancellationTokenSource? _probeThumbnailStop;
    private int _probeSequence;
    private bool _submitting;
    internal CancellationTokenSource? ProbeCancellation { set => _probeStop = value; }
    internal bool Submitting { get => _submitting; set => _submitting = value; }
    public ObservableCollection<FormatInfo> ProbeFormats { get; } = [];

    internal void Probe_Click(object sender, RoutedEventArgs e) =>
        _ = RunWindowOperationAsync("解析视频", ProbeFromButtonAsync);

    private async Task ProbeFromButtonAsync()
    {
        if (_probeStop is not null) { _probeStop.Cancel(); return; }
        await ProbeAsync();
    }

    internal async Task ProbeAsync(bool forceRefresh = false)
    {
        // 连续回车不重启同一次解析；换链接已取消的请求可以被新请求替换。
        if (_probeStop is { IsCancellationRequested: false }) return;
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
        _probeThumbnailStop?.Cancel();
        using var probeStop = CancellationTokenSource.CreateLinkedTokenSource(_windowToken);
        _probeStop = probeStop;
        var sequence = Interlocked.Increment(ref _probeSequence);
        _authRequired = false;
        SyncPlatformLoginControls();
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
            AccountQualityButton.Visibility = (IsBilibiliUrl(url) && probe.AuthStatus != "loaded"
                || PlatformConnectionWindow.PlatformForUrl(url) == "xiaohongshu" && !string.IsNullOrWhiteSpace(probe.QualityNotice))
                ? Visibility.Visible : Visibility.Collapsed;
            Replace(ProbeFormats, probe.Formats.Count > 0 ? probe.Formats : [new FormatInfo { FormatId = "source", Label = "源画质" }]);
            FormatList.SelectedIndex = 0;
            QueueProbeThumbnail(probe.Thumbnail, sequence);
            ProbePanel.Visibility = Visibility.Visible;
            AnimateCard(ProbePanel);
            UpdateSubmitHint();
            QueueSubtitlePreview();
        }
        catch (OperationCanceledException error) when (probeStop.IsCancellationRequested && error.InnerException is not TimeoutException) { }
        catch (Exception ex)
        {
            if (sequence != _probeSequence) return;
            _probe = null;
            _authRequired = ex is EngineApiException { Code: "E-AUTH-301" };
            SyncPlatformLoginControls();
            if (_authRequired && BilibiliLoginBar.Visibility == Visibility.Visible)
            {
                ParseFailure.Visibility = Visibility.Collapsed;
                return;
            }
            ParseFailureText.Text = ex.Message;
            ParseFailure.Visibility = Visibility.Visible;
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

    internal void SetProbeBusy(bool busy)
    {
        UpdateParseLayout(busy || ProbePanel.Visibility == Visibility.Visible || ParseFailure.Visibility == Visibility.Visible);
        ProbeButton.IsEnabled = true;
        ProbeButtonLabel.Text = busy ? "取消" : "解析";
        ReadLoginButton.IsEnabled = RefreshQualityButton.IsEnabled = !busy;
        ParseAuthSourceBox.IsEnabled = BrowseCookiesButton.IsEnabled = !busy;
        ConnectPlatformButton.IsEnabled = !busy;
        ProbeButtonIcon.Visibility = busy ? Visibility.Collapsed : Visibility.Visible;
        ProbeSpinner.Visibility = busy ? Visibility.Visible : Visibility.Collapsed;
        if (!busy || !SystemParameters.ClientAreaAnimation)
            ProbeSpinnerRotate.BeginAnimation(RotateTransform.AngleProperty, null);
        else if (!ProbeSpinnerRotate.HasAnimatedProperties)
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

    internal void Submit_Click(object sender, RoutedEventArgs e) =>
        _ = RunWindowOperationAsync("创建下载任务", SubmitDownloadAsync);

    private async Task SubmitDownloadAsync()
    {
        if (_submitting) return;
        if (_probe is null || FormatList.SelectedItem is not FormatInfo format) return;
        var recognize = RecognizeCheck.IsChecked == true;
        var translate = TranslateCheck.IsChecked == true;
        var burn = (recognize || _ui.VisualNamesOnly) && Selected(SubtitleOutputBox, "softmux") == "burn";
        var color = SubtitleColorValue();
        if (burn && !Regex.IsMatch(color, "^#[0-9A-Fa-f]{6}$"))
        {
            ShowError("字幕颜色请输入 #RRGGBB 格式，例如 #FFFFFF。");
            SubtitleCustomColorBox.Focus();
            return;
        }
        if (translate && !_ui.VisualNamesOnly && Selected(TranslationParseBox, "null") != "llm")
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
            if (recognize && !await EnsureWhisperReadyAsync()) return;
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
                PersonNotes = _ui.VisualNamesOnly || recognize && PersonNotesCheck.IsChecked == true,
                AudioSubtitles = _ui.VisualNamesOnly ? false : null,
                OutputDirectory = OutputPathBox.Text.Trim(),
                FontName = Selected(FontNameBox),
                FontSize = IntValue(Selected(FontSizeBox), 18),
                Position = Selected(SubtitlePositionBox, "bottom"),
                SourceLanguage = Selected(SourceLanguageBox),
                Outline = (int)OutlineSlider.Value,
                Color = color,
                VerticalOffset = (int)VerticalOffsetSlider.Value,
                HorizontalOffset = _ui.SubtitleHorizontalOffset
            }, _windowToken);
            ShowToast("任务已创建", task.DisplayTitle);
            ShowView("queue");
            MarkTaskChanged(task.Id);
            await FlushTaskChangesAsync();
        }
        catch (OperationCanceledException) when (_stop.IsCancellationRequested) { }
        catch (Exception ex) { ShowError(ex.Message); }
        finally { _submitting = false; SubmitButton.IsEnabled = true; SubmitButton.Content = "开始下载"; }
    }

    internal async Task<bool> EnsureWhisperReadyAsync()
    {
        if (RecognizeCheck.IsChecked != true || Selected(AsrProviderParseBox, "faster-whisper") == "cloud") return true;
        var status = await _engine.GetWhisperStatusAsync(Selected(WhisperParseBox, "small"), _windowToken);
        ApplyWhisperStatus(status);
        if (status.Downloaded && status.Status is not ("downloading" or "loading")) return true;
        WhisperPrompt.Visibility = Visibility.Visible;
        ShowError(status.Status is "downloading" or "loading" ? "语音模型正在准备，完成后即可开始。" : "生成字幕需要先下载语音模型，点击“下载语音模型”即可准备。仅下载视频无需模型。");
        return false;
    }

    private void UpdateSubmitHint()
    {
        if (SubmitHint is null) return;
        var format = FormatList?.SelectedItem as FormatInfo;
        var parts = new List<string> { format?.SizeText ?? "大小未知" };
        if (format is not null) parts.Insert(0, format.Label);
        parts.Add(_ui.VisualNamesOnly ? "识别画面英文人名" : RecognizeCheck.IsChecked != true ? "仅下载视频" :
            TranslateCheck.IsChecked == true ? $"识别并翻译 {_selectedLanguages.Count} 种目标语言" : "识别源语言字幕");
        if (RecognizeCheck.IsChecked == true || _ui.VisualNamesOnly)
            parts.Add(Selected(SubtitleOutputBox, "softmux") != "burn" ?
                (_ui.VisualNamesOnly || PersonNotesCheck.IsChecked == true ? "内嵌样式轨 · 检测到人名时使用 MKV" : "内嵌软字幕 + SRT") :
                BilingualCheck.IsChecked == true && TranslateCheck.IsChecked == true ? "双语字幕烧录" : "硬字幕烧录");
        SubmitHint.Text = string.Join("，", parts);
    }

    private void FormatList_SelectionChanged(object sender, SelectionChangedEventArgs e) => UpdateSubmitHint();

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
        _probeThumbnailStop?.Cancel();
        _bilibiliLoginStop?.Cancel();
        _probe = null;
        _authRequired = false;
        if (AccountQualityButton is not null) AccountQualityButton.Visibility = Visibility.Collapsed;
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

    private void Example_Click(object sender, RoutedEventArgs e)
    {
        if (sender is not Button { Tag: string url }) return;
        UrlBox.Text = url;
        _ = RunWindowOperationAsync("解析示例链接", () => ProbeAsync());
    }

    internal static string NormalizeUrl(string value)
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

    private void ProbePreviewThumb_SizeChanged(object sender, SizeChangedEventArgs e)
    {
        ((Border)sender).Clip = new RectangleGeometry(new Rect(e.NewSize), 12, 12);
    }

    private void QueueProbeThumbnail(string url, int sequence)
    {
        var source = CancellationTokenSource.CreateLinkedTokenSource(_windowToken);
        _probeThumbnailStop = source;
        var token = source.Token;
        _ = RunWindowOperationAsync("读取解析封面", async () =>
        {
            try { await LoadRemoteImageAsync(ProbeThumbnail, url, sequence, token); }
            finally
            {
                if (ReferenceEquals(_probeThumbnailStop, source)) _probeThumbnailStop = null;
                source.Dispose();
            }
        }, token);
    }

    private async Task LoadRemoteImageAsync(Image image, string url, int sequence, CancellationToken token)
    {
        image.Source = null;
        if (!Uri.TryCreate(url, UriKind.Absolute, out var uri)) return;
        try
        {
            var bytes = await ThumbnailClient.GetByteArrayAsync(uri, token);
            if (sequence != _probeSequence || token.IsCancellationRequested) return;
            var bitmap = await Task.Run(() => BitmapFromBytes(bytes), token);
            if (sequence == _probeSequence && !token.IsCancellationRequested) RevealThumbnail(image, bitmap);
        }
        catch (Exception ex) when (ex is HttpRequestException or FileFormatException or NotSupportedException or ArgumentException) { }
    }

    internal static void RevealThumbnail(Image image, BitmapSource bitmap)
    {
        image.BeginAnimation(OpacityProperty, null);
        image.Source = bitmap;
        image.Opacity = 1;
        if (SystemParameters.ClientAreaAnimation)
            image.BeginAnimation(OpacityProperty,
                new DoubleAnimation(0, 1, TimeSpan.FromMilliseconds(185)) { EasingFunction = EaseOut, FillBehavior = FillBehavior.Stop });
    }
}
