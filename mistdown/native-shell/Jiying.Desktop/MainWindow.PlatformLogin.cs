using System.IO;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Media.Imaging;
using Microsoft.Win32;

namespace Jiying.Desktop;

public partial class MainWindow
{
    private bool _authRequired;
    private CancellationTokenSource? _bilibiliLoginStop;
    internal CancellationTokenSource? BilibiliCancellation => _bilibiliLoginStop;

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
        BilibiliLoginBar.Visibility = _authRequired && (bilibili || platform is not null)
            ? Visibility.Visible : Visibility.Collapsed;
        ConnectBilibiliButton.Visibility = bilibili ? Visibility.Visible : Visibility.Collapsed;
        ConnectPlatformButton.Visibility = platform is not null ? Visibility.Visible : Visibility.Collapsed;
        var name = bilibili ? "B 站" : platform == "douyin" ? "抖音" : "小红书";
        ConnectPlatformButton.Content = "扫码登录" + name;
        BilibiliLoginAdvanced.Visibility = platform == "xiaohongshu" ? Visibility.Collapsed : Visibility.Visible;
        AuthPlatformTitle.Text = "连接" + name;
        ParseAuthHint.Text = "扫码登录后自动继续解析。";
    }

    private void AccountQuality_Click(object sender, RoutedEventArgs e) =>
        _ = RunWindowOperationAsync("连接画质账号", ConnectQualityAccountAsync);

    private Task ConnectQualityAccountAsync()
    {
        _authRequired = true;
        SyncPlatformLoginControls();
        return IsBilibiliUrl(NormalizeUrl(UrlBox.Text)) ? ConnectBilibiliAsync() : ConnectPlatformAsync();
    }

    private void ConnectPlatform_Click(object sender, RoutedEventArgs e) =>
        _ = RunWindowOperationAsync("网页登录", ConnectPlatformAsync);

    private async Task ConnectPlatformAsync()
    {
        var originalUrl = NormalizeUrl(UrlBox.Text);
        var platform = PlatformConnectionWindow.PlatformForUrl(originalUrl);
        if (platform is null) return;
        using var login = CancellationTokenSource.CreateLinkedTokenSource(_windowToken);
        var loginToken = login.Token;
        PlatformConnectionWindow? connection = null;
        connection = new PlatformConnectionWindow(this, platform, originalUrl, async cookies =>
        {
            if (!_engine.IsConnected && !await _engine.EnsureConnectedAsync(true, loginToken))
                throw new InvalidOperationException("本地视频引擎未就绪，请重试连接。");
            if (connection?.IsVisible != true) throw new OperationCanceledException();
            await _engine.SavePlatformSessionAsync(platform, cookies, loginToken);
        });
        connection.Closed += (_, _) => login.Cancel();
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

    private void BrowseCookies_Click(object sender, RoutedEventArgs e) =>
        _ = RunWindowOperationAsync("导入登录凭据", BrowseCookiesAsync);

    private async Task BrowseCookiesAsync()
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

    private void ReadLogin_Click(object sender, RoutedEventArgs e) =>
        _ = RunWindowOperationAsync("读取登录状态", ReadLoginAsync);

    private async Task ReadLoginAsync()
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
            await _engine.UpdateLoginAsync(file, browser, _windowToken);
            _config.Download.Cookies = file;
            _config.Download.CookiesFromBrowser = browser;
            return true;
        }
        catch (OperationCanceledException) when (_stop.IsCancellationRequested) { return false; }
        catch (Exception ex) { ShowError(ex.Message); return false; }
    }

    internal void ConnectBilibili_Click(object sender, RoutedEventArgs e) =>
        _ = RunWindowOperationAsync("B站扫码", ConnectBilibiliAsync);

    private async Task ConnectBilibiliAsync()
    {
        if (_bilibiliLoginStop is not null)
        { _bilibiliLoginStop.Cancel(); BilibiliQrPanel.Visibility = Visibility.Collapsed; return; }
        if (!_engine.IsConnected) { ShowError("本地视频引擎尚未连接。"); return; }
        using var loginStop = CancellationTokenSource.CreateLinkedTokenSource(_windowToken);
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

    private void RefreshQuality_Click(object sender, RoutedEventArgs e) =>
        _ = RunWindowOperationAsync("刷新画质", RefreshQualityAsync);

    private async Task RefreshQualityAsync()
    {
        if (await SaveEngineConfigAsync(false)) await ProbeAsync(true);
    }
}
