using System.IO;
using System.Reflection;
using System.Windows;
using System.Windows.Controls;
using Jiying.Desktop;

internal static class XiaohongshuLoginRegression
{
    internal static async Task Run(MainWindow owner, Action<bool, string> check)
    {
        const string url = "https://xhslink.com/o/sample";
        var type = typeof(PlatformConnectionWindow);
        var saveMethod = type.GetMethod("TrySaveXiaohongshuLoginAsync", BindingFlags.Instance | BindingFlags.NonPublic);
        var saved = 0;
        var dialog = new PlatformConnectionWindow(owner, "xiaohongshu", url, _ => { saved++; return Task.CompletedTask; });
        check(dialog.Width <= 560 && dialog.Height <= 620, "小红书使用紧凑扫码窗口");
        check(((Button)dialog.FindName("UseVideoButton")).Visibility == Visibility.Collapsed,
            "扫码登录不要求选择视频或手工确认");
        check(((Button)dialog.FindName("HomeButton")).Visibility == Visibility.Collapsed,
            "扫码窗口不显示官网浏览入口");
        check(saveMethod is not null, "小红书自动登录完成只保存真实登录态");
        var input = (TextBox)owner.FindName("UrlBox");
        var previous = input.Text;
        input.Text = url;
        owner.UpdateLayout();
        check(((Button)owner.FindName("ConnectPlatformButton")).Content.ToString()!.Contains("扫码"), "小红书主入口明确为扫码登录");
        check(((Expander)owner.FindName("BilibiliLoginAdvanced")).Visibility == Visibility.Collapsed,
            "小红书入口隐藏浏览器与Cookie文件步骤");
        input.Text = previous;
        if (saveMethod is null) { dialog.Close(); return; }
        Task<bool> Save(PlatformConnectionWindow target, bool authenticated, IReadOnlyList<WebsiteCookie> cookies)
            => (Task<bool>)saveMethod.Invoke(target, [authenticated, cookies])!;
        WebsiteCookie Cookie(string domain = ".xiaohongshu.com", long expires = 0)
            => new(domain, "web_session", "isolated-test-value", "/", true, true, expires);
        check(!await Save(dialog, false, [Cookie()]) && saved == 0, "访客web_session不会被误判为登录成功");
        check(!await Save(dialog, true, []) && saved == 0, "未取得会话不得完成登录");
        check(!await Save(dialog, true, [Cookie(".xiaohongshu.com.evil.test")]) && saved == 0,
            "非小红书域会话不得完成登录");
        check(!await Save(dialog, true, [Cookie(expires: 1)]) && saved == 0, "过期会话不得完成登录");
        check(await Save(dialog, true, [Cookie()]) && saved == 1 && dialog.VideoUrl == url,
            "扫码成功自动保存并保留原始分享链接");
        check(!await Save(dialog, true, [Cookie()]) && saved == 1, "自动完成只保存一次");
        dialog.Close();
        var attempts = 0;
        var retry = new PlatformConnectionWindow(owner, "xiaohongshu", url, _ => ++attempts == 1
            ? Task.FromException(new IOException("isolated failure")) : Task.CompletedTask);
        check(!await Save(retry, true, [Cookie()]) && retry.VideoUrl.Length == 0, "保存失败不误报登录完成");
        check(await Save(retry, true, [Cookie()]) && attempts == 2, "保存失败后可重试连接");
        retry.Close();
        var cancelled = new PlatformConnectionWindow(owner, "xiaohongshu", url, _ => { saved++; return Task.CompletedTask; });
        cancelled.Close();
        check(!await Save(cancelled, true, [Cookie()]) && saved == 1, "已关闭窗口不开始保存会话");
        var douyin = new PlatformConnectionWindow(owner, "douyin", "https://www.douyin.com/", _ => Task.CompletedTask);
        check(((Button)douyin.FindName("UseVideoButton")).Visibility == Visibility.Collapsed && douyin.Width == 520,
            "抖音先扫码，首页登录后再展开选视频");
        douyin.Close();
    }
}
