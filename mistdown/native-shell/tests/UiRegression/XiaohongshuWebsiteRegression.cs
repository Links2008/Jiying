using System.IO;
using System.Reflection;
using System.Text.Json;
using System.Windows;
using System.Windows.Controls;
using Jiying.Desktop;
using Microsoft.Web.WebView2.Core;
using Microsoft.Web.WebView2.Wpf;

internal static class XiaohongshuWebsiteRegression
{
    internal static async Task Run(MainWindow owner, string url, string output, Action<bool, string> check)
    {
        var saved = 0;
        var isolatedCookieReceived = false;
        var connection = new PlatformConnectionWindow(owner, "xiaohongshu", url, cookies =>
        {
            saved++;
            isolatedCookieReceived = cookies.Any(c => c.Name == "web_session" && c.Value == "isolated-fixture");
            return Task.CompletedTask;
        });
        var finished = new TaskCompletionSource();
        _ = owner.Dispatcher.InvokeAsync(async () =>
        {
            try { await Observe(); finished.SetResult(); }
            catch (Exception e) { finished.SetException(e); }
            finally { if (connection.IsVisible) connection.Close(); }
        });
        var result = connection.ShowDialog();
        await finished.Task;
        check(result == true && saved == 1 && isolatedCookieReceived && connection.VideoUrl == url,
            "隔离WebView扫码确认自动保存一次、关闭窗口并返回原链接");

        async Task Observe()
        {
            var browser = (WebView2)connection.FindName("Website");
            check(await Wait(() => Task.FromResult(browser.CoreWebView2 is not null)), "小红书系统WebView2初始化成功");
            var core = browser.CoreWebView2 ?? throw new InvalidOperationException("WebView2 not initialized");
            const string ready = "!!document.querySelector('.qrcode-img')?.complete && !!document.querySelector('.qrcode-img')?.naturalWidth";
            var qrReady = await Wait(async () => await core.ExecuteScriptAsync(ready) == "true");
            check(qrReady, "官网真实二维码加载成功");
            if (!qrReady) throw new InvalidOperationException("Xiaohongshu official QR did not load");
            var stateScript = (string)typeof(PlatformConnectionWindow).GetField("XiaohongshuLoginStateScript",
                BindingFlags.Static | BindingFlags.NonPublic)!.GetRawConstantValue()!;
            using var state = JsonDocument.Parse(await core.ExecuteScriptAsync(stateScript));
            check(!state.RootElement.GetProperty("authenticated").GetBoolean() && saved == 0,
                "官网访客状态不自动保存或关闭窗口");
            using var layout = JsonDocument.Parse(await core.ExecuteScriptAsync("""
                (()=>{const q=document.querySelector('.qrcode-img'),r=q.getBoundingClientRect();
                  const phone=document.querySelector('.input-container');
                  return {width:r.width,height:r.height,left:r.left,top:r.top,
                    viewportWidth:innerWidth,viewportHeight:innerHeight,
                    phoneHidden:!phone||getComputedStyle(phone).display==='none'};})()
                """));
            var l = layout.RootElement;
            check(l.GetProperty("width").GetDouble() >= 199 && l.GetProperty("height").GetDouble() >= 199
                && l.GetProperty("left").GetDouble() >= 0 && l.GetProperty("top").GetDouble() >= 0
                && l.GetProperty("top").GetDouble() + l.GetProperty("height").GetDouble() <= l.GetProperty("viewportHeight").GetDouble(),
                "原生紧凑窗口内200像素二维码完整显示");
            check(l.GetProperty("phoneHidden").GetBoolean(), "官网手机号和验证码输入区已隐藏");
            File.WriteAllText(Path.Combine(output, "qr-layout.json"), layout.RootElement.ToString());
            using (var preview = File.Create(Path.Combine(output, "qr-webview.png")))
                await core.CapturePreviewAsync(CoreWebView2CapturePreviewImageFormat.Png, preview);
            await Refresh(core, ready);
            await AuthenticateFixture(core, stateScript);
        }

        async Task Refresh(CoreWebView2 core, string ready)
        {
            var previousQr = await core.ExecuteScriptAsync("document.querySelector('.qrcode-img').src");
            ((Button)connection.FindName("ReloadButton")).RaiseEvent(new RoutedEventArgs(Button.ClickEvent));
            check(await Wait(async () => await core.ExecuteScriptAsync(ready) == "true"
                && await core.ExecuteScriptAsync("document.querySelector('.qrcode-img').src") != previousQr),
                "刷新按钮重新取得官网二维码");
        }

        async Task AuthenticateFixture(CoreWebView2 core, string stateScript)
        {
            var fixture = Path.Combine(output, "fixture");
            Directory.CreateDirectory(fixture);
            File.WriteAllText(Path.Combine(fixture, "index.html"), """
                <!doctype html><title>JIYING_QR_FIXTURE</title>
                <script>window.__INITIAL_STATE__={user:{loggedIn:{value:false}}};</script>
                <div class="login-modal"><div class="login-container"><div class="qrcode">
                  <div class="status"><div class="status-desc refresh" onclick="window.fixtureRefreshed=true">点击刷新</div></div>
                </div></div></div>
                """);
            core.SetVirtualHostNameToFolderMapping("www.xiaohongshu.com", fixture, CoreWebView2HostResourceAccessKind.DenyCors);
            core.Navigate("https://www.xiaohongshu.com/index.html");
            if (!await Wait(async () => await core.ExecuteScriptAsync("document.title") == "\"JIYING_QR_FIXTURE\""))
                throw new InvalidOperationException("Isolated fixture did not load");
            await core.ExecuteScriptAsync("document.body.insertAdjacentHTML('beforeend','<div id=fixture-captcha style=\"width:200px;height:80px\">Isolated verification</div>')");
            using (var verification = JsonDocument.Parse(await core.ExecuteScriptAsync(stateScript)))
                check(verification.RootElement.GetProperty("verification").GetBoolean(), "小红书隔离安全验证状态可检测");
            check(await Wait(() => Task.FromResult(((TextBlock)connection.FindName("ConnectionStatus")).Text.Contains("安全验证")), 3),
                "小红书安全验证时明确提示先完成官网验证");
            await core.ExecuteScriptAsync("document.getElementById('fixture-captcha').remove()");
            var cookie = core.CookieManager.CreateCookie("web_session", "isolated-fixture", ".xiaohongshu.com", "/");
            cookie.IsSecure = true; cookie.IsHttpOnly = true;
            core.CookieManager.AddOrUpdateCookie(cookie);
            using var state = JsonDocument.Parse(await core.ExecuteScriptAsync(stateScript));
            check(!state.RootElement.GetProperty("authenticated").GetBoolean() && saved == 0,
                "隔离页面的访客会话仍不会自动完成");
            var navigations = 0;
            core.NavigationStarting += (_, _) => navigations++;
            ((Button)connection.FindName("ReloadButton")).RaiseEvent(new RoutedEventArgs(Button.ClickEvent));
            check(await Wait(async () => await core.ExecuteScriptAsync("window.fixtureRefreshed===true") == "true") && navigations == 0,
                "过期二维码直接触发官网刷新控件，不重载整页");
            await core.ExecuteScriptAsync("window.__INITIAL_STATE__.user.loggedIn.value=true");
            check(await Wait(() => Task.FromResult(!connection.IsVisible)), "登录状态改变后无需手工点击即自动关闭");
        }
    }

    private static async Task<bool> Wait(Func<Task<bool>> condition, int seconds = 75)
    {
        var deadline = DateTime.UtcNow.AddSeconds(seconds);
        while (DateTime.UtcNow < deadline)
        {
            try { if (await condition()) return true; }
            catch (Exception) { }
            await Task.Delay(250);
        }
        return false;
    }
}
