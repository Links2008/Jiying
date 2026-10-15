using System.IO;
using System.Text.RegularExpressions;
using System.Windows;
using Microsoft.Web.WebView2.Core;

namespace Jiying.Desktop;

public partial class PlatformConnectionWindow : Window
{
    private readonly string _platform;
    private readonly string _originalUrl;
    private readonly Func<IReadOnlyList<WebsiteCookie>, Task> _save;
    private bool _closed;
    private bool _selectingVideo;
    private bool _qrLoadRetried;
    private DateTime _qrNavigationStarted = DateTime.UtcNow;
    public string VideoUrl { get; private set; } = "";

    public PlatformConnectionWindow(Window owner, string platform, string url,
                                     Func<IReadOnlyList<WebsiteCookie>, Task> save)
    {
        InitializeComponent();
        Owner = owner;
        Resources.MergedDictionaries.Add(owner.Resources);
        _platform = platform;
        _originalUrl = url;
        _save = save;
        ConnectionTitle.Text = "连接" + (platform == "douyin" ? "抖音" : "小红书");
        if (platform is "xiaohongshu" or "douyin")
        {
            Width = 520; Height = 560; MinWidth = 440; MinHeight = 460;
            ResizeMode = ResizeMode.NoResize;
            var name = platform == "douyin" ? "抖音" : "小红书";
            ConnectionTitle.Text = "扫码登录" + name;
            ConnectionInstructions.Text = "使用" + name + " App 扫码，在手机上确认后自动继续。";
            HomeButton.Visibility = UseVideoButton.Visibility = Visibility.Collapsed;
            ReloadButton.Content = "刷新二维码";
            CancelButton.Visibility = Visibility.Visible;
        }
        Title = ConnectionTitle.Text + " · 即影";
        Loaded += async (_, _) => await InitializeWebsiteAsync();
        Closed += (_, _) => { _closed = true; Website.Dispose(); };
    }

    public static string? PlatformForUrl(string url)
    {
        if (!Uri.TryCreate(url, UriKind.Absolute, out var uri) || uri.Scheme is not ("https" or "http")) return null;
        var host = uri.Host.ToLowerInvariant();
        if (host is "douyin.com" or "www.douyin.com" or "m.douyin.com" or "v.douyin.com"
            or "iesdouyin.com" or "www.iesdouyin.com" or "jingxuan.douyin.com" or "open.douyin.com") return "douyin";
        if (host is "xiaohongshu.com" or "www.xiaohongshu.com" or "m.xiaohongshu.com"
            or "xhslink.com" or "www.xhslink.com") return "xiaohongshu";
        return null;
    }

    private bool AllowsNavigation(string url) => Uri.TryCreate(url, UriKind.Absolute, out var uri)
        && uri.Scheme is "https" or "http" && (_platform == "douyin"
            ? uri.Host is "douyin.com" or "iesdouyin.com" || uri.Host.EndsWith(".douyin.com", StringComparison.OrdinalIgnoreCase)
              || uri.Host.EndsWith(".iesdouyin.com", StringComparison.OrdinalIgnoreCase)
            : uri.Host is "xiaohongshu.com" or "xhslink.com" || uri.Host.EndsWith(".xiaohongshu.com", StringComparison.OrdinalIgnoreCase)
              || uri.Host.EndsWith(".xhslink.com", StringComparison.OrdinalIgnoreCase));

    private async Task InitializeWebsiteAsync()
    {
        try
        {
            var profileRoot = Environment.GetEnvironmentVariable("JIYING_PLATFORM_PROFILE")
                ?? Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "Jiying", "website-profile");
            var environment = await CoreWebView2Environment.CreateAsync(userDataFolder: Path.Combine(profileRoot, _platform));
            if (_closed) return;
            await Website.EnsureCoreWebView2Async(environment);
            if (_closed) return;
            if (IsActive) Website.Focus();
            var core = Website.CoreWebView2;
            core.Settings.IsWebMessageEnabled = false;
            core.Settings.IsPasswordAutosaveEnabled = false;
            core.Settings.IsGeneralAutofillEnabled = false;
            core.NavigationStarting += (_, e) =>
            {
                if (!AllowsNavigation(e.Uri)) e.Cancel = true;
                else _qrNavigationStarted = DateTime.UtcNow;
            };
            core.NewWindowRequested += (_, e) => { e.Handled = true; if (AllowsNavigation(e.Uri)) core.Navigate(e.Uri); };
            core.DownloadStarting += (_, e) => e.Cancel = true;
            ReloadButton.IsEnabled = true;
            foreach (var context in new[] { CoreWebView2WebResourceContext.Media, CoreWebView2WebResourceContext.Image })
                core.AddWebResourceRequestedFilter("*", context, CoreWebView2WebResourceRequestSourceKinds.Document);
            core.WebResourceRequested += (_, e) =>
            {
                if (DeferScanResource(e.Request.Uri, e.ResourceContext, _selectingVideo))
                    e.Response = environment.CreateWebResourceResponse(null, 204, "No Content", "");
            };
            if (_platform == "xiaohongshu")
            { await InitializeXiaohongshuAsync(core); return; }
            await InitializeDouyinAsync(core);
        }
        catch (Exception)
        {
            if (!_closed) ConnectionStatus.Text = _platform == "xiaohongshu"
                ? "扫码窗口无法启动，请安装或修复微软 WebView2 Runtime 后重试。"
                : "官网窗口无法启动；请安装微软 WebView2 Runtime，或使用解析页的浏览器 / Cookie 文件连接。";
        }
    }
    public static string SelectedVideoUrl(string platform, string currentUrl, string originalUrl)
    {
        bool HasVideo(string url)
        {
            if (PlatformForUrl(url) != platform || !Uri.TryCreate(url, UriKind.Absolute, out var uri)) return false;
            if (platform == "xiaohongshu") return Regex.IsMatch(uri.AbsolutePath, @"^/(explore|discovery/item|user/profile/[a-f0-9]{24})/[a-f0-9]{24}(/|$)", RegexOptions.IgnoreCase);
            if (Regex.IsMatch(uri.AbsolutePath, @"^/(share/|m/)?video/\d+(/|$)")) return true;
            return uri.Query.TrimStart('?').Split('&').Select(part => part.Split('=', 2)).Any(pair =>
                pair.Length == 2 && pair[0] is "modal_id" or "aweme_id" or "vid" or "item_id"
                && Regex.IsMatch(Uri.UnescapeDataString(pair[1]), @"^\d+$"));
        }
        return HasVideo(currentUrl) ? currentUrl : HasVideo(originalUrl) ? originalUrl : "";
    }

    public async Task<IReadOnlyList<WebsiteCookie>> ReadSessionCookiesAsync()
    {
        var cookies = new Dictionary<(string, string, string), WebsiteCookie>();
        var hosts = _platform == "douyin" ? new[] { "www.douyin.com", "www.iesdouyin.com" } : new[] { "www.xiaohongshu.com" };
        foreach (var host in hosts)
        foreach (var item in await Website.CoreWebView2.CookieManager.GetCookiesAsync("https://" + host + "/"))
        {
            if (string.IsNullOrWhiteSpace(item.Name)) continue;
            var cookie = item.ToSystemNetCookie();
            var expires = item.IsSession ? 0 : new DateTimeOffset(cookie.Expires.ToUniversalTime()).ToUnixTimeSeconds();
            if (expires > 0 && expires <= DateTimeOffset.UtcNow.ToUnixTimeSeconds()) continue;
            cookies[(cookie.Domain, cookie.Name, cookie.Path)] = new(cookie.Domain, cookie.Name, cookie.Value,
                cookie.Path, cookie.Secure, cookie.HttpOnly, expires);
        }
        return cookies.Values.ToArray();
    }

    private void Home_Click(object sender, RoutedEventArgs e) => Website.CoreWebView2.Navigate(
        _platform == "douyin" ? "https://www.douyin.com/" : "https://www.xiaohongshu.com/explore");

    private async void Reload_Click(object sender, RoutedEventArgs e)
    {
        if (_saveFailed)
        { _saveFailed = false; ConnectionStatus.Text = "正在重试连接…"; return; }
        ConnectionStatus.Text = _selectingVideo ? "正在刷新网页…" : "正在刷新二维码…";
        _qrLoadRetried = false;
        _qrNavigationStarted = DateTime.UtcNow;
        try
        {
            if (IsActive) Website.Focus();
            if (!_selectingVideo && await Website.CoreWebView2.ExecuteScriptAsync(
                _platform == "xiaohongshu" ? XiaohongshuRefreshScript : DouyinRefreshScript) == "true") return;
            if (!_closed) Website.Reload();
        }
        catch (Exception) { if (!_closed) ConnectionStatus.Text = "刷新未完成，请重试。"; }
    }

    private void Cancel_Click(object sender, RoutedEventArgs e) => Close();

    private async void UseVideo_Click(object sender, RoutedEventArgs e)
    {
        var url = SelectedVideoUrl(_platform, Website.Source?.AbsoluteUri ?? "", _originalUrl);
        if (url.Length == 0) { ConnectionStatus.Text = "请先打开要下载的视频，再点击“使用此视频”。"; return; }
        UseVideoButton.IsEnabled = false;
        try
        {
            var cookies = await ReadSessionCookiesAsync();
            if (_closed) return;
            if (cookies.Count == 0 || _platform == "xiaohongshu" && !cookies.Any(c => c.Name == "web_session"))
            { ConnectionStatus.Text = "官网会话尚未就绪，请完成登录或刷新网页后重试。"; return; }
            await _save(cookies);
            if (_closed) return;
            VideoUrl = url;
            DialogResult = true;
        }
        catch (Exception) { if (!_closed) ConnectionStatus.Text = "连接未能保存，请检查本地引擎连接后重试。"; }
        finally { if (!_closed) UseVideoButton.IsEnabled = true; }
    }
}
