using System.Windows;
using System.Windows.Controls;

namespace Jiying.Desktop;

public partial class MainWindow
{
    private CancellationTokenSource? _whisperPollStop;
    private CancellationTokenSource? _gpuPollStop;
    private WhisperStatus? _whisperStatus;
    internal WhisperStatus? WhisperComponentStatus { set => _whisperStatus = value; }

    internal void DownloadWhisper_Click(object sender, RoutedEventArgs e) =>
        _ = RunWindowOperationAsync("下载语音模型", DownloadWhisperAsync);

    private async Task DownloadWhisperAsync()
    {
        if (!_engine.IsConnected) { ShowError("本地视频引擎尚未连接。"); return; }
        var model = Selected(WhisperSettingsBox, "small");
        DownloadWhisperButton.IsEnabled = false;
        PrepareWhisperButton.IsEnabled = false;
        DownloadWhisperButton.Content = "连接中…";
        WhisperStatusText.Text = "正在连接模型仓库…";
        try
        {
            await _engine.DownloadWhisperAsync(model, _windowToken);
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

    private void DownloadGpu_Click(object sender, RoutedEventArgs e) =>
        _ = RunWindowOperationAsync("下载 NVIDIA 组件", DownloadGpuAsync);

    private async Task DownloadGpuAsync()
    {
        if (!_engine.IsConnected) { ShowError("本地视频引擎尚未连接。"); return; }
        DownloadGpuButton.IsEnabled = false;
        GpuStatusText.Text = "正在连接 NVIDIA 组件仓库…";
        try
        {
            await _engine.DownloadGpuAsync(_windowToken);
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
        _gpuPollStop = CancellationTokenSource.CreateLinkedTokenSource(_windowToken);
        var token = _gpuPollStop.Token;
        _ = RunWindowOperationAsync("GPU 组件状态", () => PollGpuStatusAsync(token), token);
    }

    private async Task PollGpuStatusAsync(CancellationToken token)
    {
        try
        {
            while (!token.IsCancellationRequested && _engine.IsConnected)
            {
                var status = await _engine.GetGpuStatusAsync(token);
                token.ThrowIfCancellationRequested();
                ApplyGpuStatus(status);
                if (status.Status != "downloading") return;
                await Task.Delay(1200, token);
            }
        }
        catch (OperationCanceledException) when (token.IsCancellationRequested) { }
        catch (Exception ex) when (!token.IsCancellationRequested)
        {
            DesktopDiagnostics.Write("读取 GPU 组件状态", ex);
            GpuStatusText.Text = $"组件状态读取失败：{FriendlyError(ex.Message)}";
        }
    }

    internal void ApplyGpuStatus(GpuStatus status)
    {
        var busy = status.Status == "downloading";
        GpuProgress.Visibility = busy ? Visibility.Visible : Visibility.Collapsed;
        GpuProgress.IsIndeterminate = busy && status.Fraction <= 0;
        GpuProgress.Value = Math.Clamp(status.Fraction, 0, 1) * 100;
        DownloadGpuButton.IsEnabled = status.Supported && !busy && (!status.Downloaded || status.Status == "failed");
        DownloadGpuButton.Content = busy ? "下载中…" : status.Downloaded ? "已安装" :
            status.Status == "failed" ? "重试下载" :
            status.SizeBytes > 0 ? $"下载 GPU 组件 · {status.SizeBytes / 1048576.0:0} MiB" : "下载 GPU 组件";
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
        _whisperPollStop = CancellationTokenSource.CreateLinkedTokenSource(_windowToken);
        var token = _whisperPollStop.Token;
        _ = RunWindowOperationAsync("语音模型状态", () => PollWhisperStatusAsync(model, token), token);
    }

    private async Task PollWhisperStatusAsync(string model, CancellationToken token)
    {
        while (!token.IsCancellationRequested && _engine.IsConnected)
        {
            try
            {
                var status = await _engine.GetWhisperStatusAsync(model, token);
                var busy = status.Status is "downloading" or "loading";
                token.ThrowIfCancellationRequested();
                ApplyWhisperStatus(status);
                if (!busy) return;
                await Task.Delay(1200, token);
            }
            catch (OperationCanceledException) when (token.IsCancellationRequested) { return; }
            catch (Exception ex)
            {
                if (!token.IsCancellationRequested)
                {
                    DesktopDiagnostics.Write("读取语音模型状态", ex);
                    WhisperStatusText.Text = $"模型状态检查失败：{FriendlyError(ex.Message)}";
                }
                return;
            }
        }
    }

    internal void ApplyWhisperStatus(WhisperStatus status)
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

    internal void SyncWhisperPrompt()
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
}
