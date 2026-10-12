using System.Reflection;
using System.Windows;
using System.Windows.Controls;
using Jiying.Desktop;
using Microsoft.Web.WebView2.Core;

internal static class QrLoadingRegression
{
    internal static async Task Run(MainWindow owner, Action<bool, string> check)
    {
        var type = typeof(PlatformConnectionWindow);
        var policy = type.GetMethod("DeferScanResource", BindingFlags.Static | BindingFlags.NonPublic);
        check(policy is not null, "扫码阶段具有轻量资源策略");
        if (policy is not null)
        {
            bool Defer(string url, CoreWebView2WebResourceContext kind, bool selecting = false)
                => (bool)policy.Invoke(null, [url, kind, selecting])!;
            check(Defer("https://video.douyin.com/feed.mp4", CoreWebView2WebResourceContext.Media), "扫码阶段暂停网页视频加载");
            check(!Defer("https://video.douyin.com/feed.mp4", CoreWebView2WebResourceContext.Media, true), "选视频阶段恢复网页媒体加载");
            check(Defer("https://sns-webpic-qc.xhscdn.com/note", CoreWebView2WebResourceContext.Image), "扫码阶段不加载笔记封面");
            check(!Defer("https://fe-platform.xhscdn.com/qrcode.png", CoreWebView2WebResourceContext.Image), "二维码图片正常加载");
            check(!Defer("https://verify.snssdk.com/captcha.png", CoreWebView2WebResourceContext.Image), "安全验证图片正常加载");
            check(!Defer("https://www.douyin.com/passport/check", CoreWebView2WebResourceContext.XmlHttpRequest), "官网登录及验证请求保持完整");
        }
        var save = type.GetMethod("TrySaveDouyinLoginAsync", BindingFlags.Instance | BindingFlags.NonPublic);
        check(save is not null, "抖音使用官网确认状态自动完成登录");
        var count = 0;
        var video = new PlatformConnectionWindow(owner, "douyin", "https://v.douyin.com/sample/", _ => { count++; return Task.CompletedTask; });
        check(video.Width <= 560 && ((Button)video.FindName("UseVideoButton")).Visibility == Visibility.Collapsed, "抖音首先只显示紧凑扫码窗口");
        if (save is not null)
        {
            Task<bool> Save(PlatformConnectionWindow target, bool authenticated, IReadOnlyList<WebsiteCookie> cookies)
                => (Task<bool>)save.Invoke(target, [authenticated, cookies])!;
            WebsiteCookie Cookie(string name = "sessionid", string domain = ".douyin.com", long expires = 0)
                => new(domain, name, "isolated-test-value", "/", true, true, expires);
            check(!await Save(video, false, [Cookie()]) && count == 0, "抖音未确认登录不自动完成");
            check(!await Save(video, true, [Cookie("ttwid")]) && count == 0, "抖音访客Cookie不误报登录成功");
            check(!await Save(video, true, [Cookie(domain: ".douyin.com.evil.test")]) && count == 0, "抖音拒绝外站会话");
            check(!await Save(video, true, [Cookie(expires: 1)]) && count == 0, "抖音拒绝过期会话");
            check(await Save(video, true, [Cookie()]) && count == 1 && video.VideoUrl == "https://v.douyin.com/sample/", "抖音扫码成功保留原分享链接并自动继续");
            check(!await Save(video, true, [Cookie()]) && count == 1, "抖音自动完成仅保存一次");
            var feed = new PlatformConnectionWindow(owner, "douyin", "https://www.douyin.com/?recommend=1", _ => Task.CompletedTask);
            check(!await Save(feed, true, [Cookie()]) && feed.VideoUrl.Length == 0 && feed.Width == 1040
                && ((Button)feed.FindName("UseVideoButton")).Visibility == Visibility.Visible,
                "抖音首页登录后展开官网选择视频，不能误作具体视频");
            feed.Close();
            var attempts = 0;
            var retry = new PlatformConnectionWindow(owner, "douyin", "https://www.douyin.com/video/123", _ => ++attempts == 1
                ? Task.FromException(new System.IO.IOException("isolated failure")) : Task.CompletedTask);
            check(!await Save(retry, true, [Cookie()]) && retry.VideoUrl.Length == 0, "抖音保存失败不误报成功");
            check(await Save(retry, true, [Cookie()]) && attempts == 2, "抖音保存失败可重试，无需重新扫码");
            retry.Close();
            var cancelled = new PlatformConnectionWindow(owner, "douyin", "https://www.douyin.com/video/123", _ => { count++; return Task.CompletedTask; });
            cancelled.Close();
            check(!await Save(cancelled, true, [Cookie()]) && count == 1, "抖音关闭窗口后不开始保存");
        }
        video.Close();
        var input = (TextBox)owner.FindName("UrlBox");
        var before = input.Text; input.Text = "https://www.douyin.com/video/123";
        check(((Button)owner.FindName("ConnectPlatformButton")).Content.ToString()!.Contains("扫码"), "抖音解析入口明确为扫码登录");
        var previousProfile = Environment.GetEnvironmentVariable("JIYING_PLATFORM_PROFILE");
        Environment.SetEnvironmentVariable("JIYING_PLATFORM_PROFILE", System.IO.Path.Combine(System.IO.Path.GetTempPath(), "Jiying.QrEntry", Guid.NewGuid().ToString("N")));
        var opened = false;
        var inspected = owner.Dispatcher.InvokeAsync(() =>
        {
            var popup = Application.Current.Windows.OfType<PlatformConnectionWindow>().FirstOrDefault(w => w.IsVisible);
            opened = popup is not null;
            popup?.Close();
        }, System.Windows.Threading.DispatcherPriority.Background);
        ((Button)owner.FindName("ConnectPlatformButton")).RaiseEvent(new RoutedEventArgs(Button.ClickEvent));
        await inspected.Task;
        check(opened, "本地引擎未就绪时也能立即打开扫码窗口");
        Environment.SetEnvironmentVariable("JIYING_PLATFORM_PROFILE", previousProfile);
        input.Text = before;
    }
}
