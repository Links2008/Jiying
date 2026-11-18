using System.Windows;
using System.Windows.Controls;
using System.Windows.Controls.Primitives;
using System.Windows.Media;

namespace Jiying.Desktop;

public partial class MainWindow
{
    private static readonly Dictionary<string, (string BaseUrl, string Model)> LlmPresets = new()
    {
        ["deepseek"] = ("https://api.deepseek.com/v1", "deepseek-flash"),
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

    private readonly SemaphoreSlim _configSaveLock = new(1, 1);
    private EngineConfig _config = new();
    private Task<string>? _ocrLanguageHint;
    private bool _loadingControls = true;
    internal bool ControlsAreLoading { get => _loadingControls; set => _loadingControls = value; }

    private async Task<bool> LoadConfigAsync(CancellationToken token = default)
    {
        if (!token.CanBeCanceled) token = _windowToken;
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

    internal async Task<bool> SaveEngineConfigAsync(bool showToast = true)
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
            await _configSaveLock.WaitAsync(_windowToken);
            acquired = true;
            await Dispatcher.InvokeAsync(() => SettingsSaveStatus.Text = "正在保存…");
            var update = await Dispatcher.InvokeAsync(BuildConfigUpdate);
            await _engine.UpdateConfigAsync(update, _windowToken);
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

    private void SaveSettings_Click(object sender, RoutedEventArgs e) =>
        _ = RunWindowOperationAsync("保存设置", () => SaveEngineConfigAsync(false));

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

    private void ParseConfig_Changed(object sender, SelectionChangedEventArgs e) =>
        _ = RunWindowOperationAsync("更新解析配置", () => SyncParseConfigAsync(sender));

    private async Task SyncParseConfigAsync(object sender)
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

    private void SettingsConfig_Changed(object sender, RoutedEventArgs e) =>
        _ = RunWindowOperationAsync("更新引擎配置", SyncSettingsConfigAsync);

    private async Task SyncSettingsConfigAsync()
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

    private void WhisperModel_Changed(object sender, SelectionChangedEventArgs e) =>
        _ = RunWindowOperationAsync("更换语音模型", ChangeWhisperModelAsync);

    private async Task ChangeWhisperModelAsync()
    {
        if (_loadingControls) return;
        var model = Selected(WhisperSettingsBox, "small");
        WithControlLoading(() => Select(WhisperParseBox, model));
        SyncDependentControls();
        await SaveEngineConfigAsync();
        StartWhisperStatus(model);
    }

    private void ParseLlmVendor_Changed(object sender, SelectionChangedEventArgs e) =>
        _ = RunWindowOperationAsync("更换翻译服务", () => ChangeLlmVendorAsync(true));

    private void SettingsLlmVendor_Changed(object sender, SelectionChangedEventArgs e) =>
        _ = RunWindowOperationAsync("更换翻译服务", () => ChangeLlmVendorAsync(false));

    private async Task ChangeLlmVendorAsync(bool fromParse)
    {
        if (_loadingControls) return;
        var vendor = Selected(fromParse ? LlmVendorParseBox : LlmVendorSettingsBox, "deepseek");
        WithControlLoading(() => Select(fromParse ? LlmVendorSettingsBox : LlmVendorParseBox, vendor));
        ApplyLlmPreset(vendor);
        await SaveEngineConfigAsync();
    }

    private void ParseAsrVendor_Changed(object sender, SelectionChangedEventArgs e) =>
        _ = RunWindowOperationAsync("更换语音服务", () => ChangeAsrVendorAsync(true));

    private void SettingsAsrVendor_Changed(object sender, SelectionChangedEventArgs e) =>
        _ = RunWindowOperationAsync("更换语音服务", () => ChangeAsrVendorAsync(false));

    private async Task ChangeAsrVendorAsync(bool fromParse)
    {
        if (_loadingControls) return;
        var vendor = Selected(fromParse ? AsrVendorParseBox : AsrVendorSettingsBox, "siliconflow");
        WithControlLoading(() => Select(fromParse ? AsrVendorSettingsBox : AsrVendorParseBox, vendor));
        ApplyAsrPreset(vendor);
        await SaveEngineConfigAsync();
    }

    private void ParseTextConfig_Changed(object sender, RoutedEventArgs e) =>
        _ = RunWindowOperationAsync("更新服务参数", SyncParseTextConfigAsync);

    private async Task SyncParseTextConfigAsync()
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

    private void SettingsTextConfig_Changed(object sender, RoutedEventArgs e) =>
        _ = RunWindowOperationAsync("更新服务参数", SyncSettingsTextConfigAsync);

    private async Task SyncSettingsTextConfigAsync()
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

    internal void SyncDependentControls(bool animateSubtitles = false)
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
        var visualOnly = _ui.VisualNamesOnly;
        var translateSubtitles = TranslateCheck.IsChecked == true;
        var personNotes = visualOnly || recognize && PersonNotesCheck.IsChecked == true;
        WithControlLoading(() => Select(SubtitleModeBox, visualOnly ? "visual" : !recognize ? "none" : translateSubtitles ? "translate" : "source"));
        SubtitleModeSummary.Text = visualOnly ? "读取视频名牌与署名，无需语音模型。" : !recognize ? "无需识别，直接下载。" :
            translateSubtitles ? "AI 自动理解语境、人名与术语，再翻译成选中的目标语言。" : "从视频语音生成原语言字幕。";
        TranslateCheck.Visibility = visualOnly ? Visibility.Visible : Visibility.Collapsed;
        SpeechModelRow.Visibility = SpeechLanguageRow.Visibility = SpeechProviderRow.Visibility =
            recognize ? Visibility.Visible : Visibility.Collapsed;
        SetSubtitleSection(TranslationLanguageRow, translateSubtitles || personNotes, animateSubtitles);
        SetSubtitleSection(TranslationProviderRow, translateSubtitles || personNotes, animateSubtitles);
        SetSubtitleSection(RecognitionOptions, recognize || visualOnly, animateSubtitles);
        var burn = Selected(SubtitleOutputBox, "softmux") == "burn";
        SubtitleOutputBox.IsEnabled = recognize || visualOnly;
        PersonNotesCheck.Visibility = recognize ? Visibility.Visible : Visibility.Collapsed;
        SubtitleOutputHint.Text = burn
            ? "字幕固定在画面中，需重新编码视频，导出较慢；同时保留 SRT 文件。"
            : personNotes ? "检测到人名后输出 MKV，内嵌可编辑的 ASS 样式轨。普通 SRT 字幕样式由播放器控制。"
                          : "字幕内嵌到视频，可关闭或切换；不重新编码画面，同时保留 SRT 文件。";
        if (personNotes) UpdateOcrLanguageHint();
        SetSubtitleSection(BurnCard, recognize || visualOnly, animateSubtitles);
        SetSubtitleSection(BurnSettingsPanel, (recognize || visualOnly) && (burn || personNotes), animateSubtitles);
        BilingualCheck.IsEnabled = recognize && translateSubtitles && burn;
        SubtitleCustomColorBox.Visibility = Selected(SubtitleColorBox) == "custom" ? Visibility.Visible : Visibility.Collapsed;
        ConcurrencyLabel.Text = $"最多 {(int)ConcurrencySlider.Value} 个任务并行";
        SyncWhisperPrompt();
    }

    private void UpdateOcrLanguageHint()
    {
        void AppendHint(string hint)
        {
            if (hint.Length > 0 && !SubtitleOutputHint.Text.EndsWith(hint, StringComparison.Ordinal))
                SubtitleOutputHint.Text += " " + hint;
        }

        if (_ocrLanguageHint is null)
        {
            var probe = _ocrLanguageHint = Task.Run(FrameOcr.LanguageHint);
            _ = RunWindowOperationAsync("检查画面文字识别语言", async () =>
            {
                var hint = await probe;
                if (!_windowToken.IsCancellationRequested
                    && (_ui.VisualNamesOnly || RecognizeCheck.IsChecked == true && PersonNotesCheck.IsChecked == true))
                    AppendHint(hint);
            }, _windowToken);
        }
        else if (_ocrLanguageHint.IsCompletedSuccessfully)
        {
            AppendHint(_ocrLanguageHint.Result);
        }
    }

    private void UiOption_Changed(object sender, RoutedEventArgs e)
    {
        if (_loadingControls) return;
        _ui.Notify = NotifyCheck.IsChecked == true;
        _ui.AutoParse = AutoParseCheck.IsChecked == true;
        _ui.SourceLanguage = Selected(SourceLanguageBox);
        _ui.Save();
    }

    private void ConcurrencySlider_ValueChanged(object sender, RoutedPropertyChangedEventArgs<double> e)
    {
        if (ConcurrencyLabel is null) return;
        ConcurrencyLabel.Text = $"最多 {(int)e.NewValue} 个任务并行";
        if (!_loadingControls) _ = RunWindowOperationAsync("更新并发数", () => SaveEngineConfigAsync());
    }

    private void YtDlpCheck_Click(object sender, RoutedEventArgs e) =>
        _ = RunWindowOperationAsync("检查解析引擎更新", CheckYtDlpAsync);

    private async Task CheckYtDlpAsync()
    {
        YtDlpCheckButton.IsEnabled = false;
        YtDlpCheckLabel.Text = "检查中…";
        try
        {
            if (!await _engine.EnsureConnectedAsync(true, _windowToken))
                throw new InvalidOperationException("本地视频引擎尚未连接");
            var result = await _engine.CheckYtDlpAsync(_windowToken);
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
            var status = await _engine.GetYtDlpStatusAsync(_windowToken);
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
}
