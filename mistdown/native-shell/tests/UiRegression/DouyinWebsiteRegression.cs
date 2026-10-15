using System.IO;
using System.Reflection;
using System.Text.Json;
using System.Windows;
using System.Windows.Controls;
using Jiying.Desktop;
using Microsoft.Web.WebView2.Core;
using Microsoft.Web.WebView2.Wpf;

internal static class DouyinWebsiteRegression
{
    internal static async Task Run(MainWindow owner, string output, Action<bool, string> check)
    {
        const string url = "https://v.douyin.com/sample/";
        var saved = 0;
        var connection = new PlatformConnectionWindow(owner, "douyin", url, _ => { saved++; return Task.CompletedTask; });
        var finished = new TaskCompletionSource();
        _ = owner.Dispatcher.InvokeAsync(async () =>
        {
            try { await Observe(); finished.SetResult(); }
            catch (Exception e) { finished.SetException(e); }
            finally { if (connection.IsVisible) connection.Close(); }
        });
        var result = connection.ShowDialog();
        await finished.Task;
        check(result == true && saved == 1 && connection.VideoUrl == url, "抖音隔离扫码回调自动保存一次并返回原分享链接");
        await RunHomepage(owner, output, check);

        async Task Observe()
        {
            var browser = (WebView2)connection.FindName("Website");
            check(await Wait(() => Task.FromResult(browser.CoreWebView2 is not null)), "抖音系统WebView2初始化成功");
            var core = browser.CoreWebView2 ?? throw new InvalidOperationException("No WebView2");
            if (Environment.GetEnvironmentVariable("JIYING_FIXTURE_ONLY") != "1")
            {
            check(await Wait(async () => await core.ExecuteScriptAsync("document.hasFocus()") == "true", 3), "扫码官网自动获得焦点，避免组件暂停出码");
            var ready = await Wait(async () => await core.ExecuteScriptAsync("(()=>{const q=document.querySelector('#douyin_login_comp_scan_code img[aria-label=\"二维码\"]');return !!q&&q.complete&&q.naturalWidth>0;})()") == "true");
            check(ready, "抖音官网真实二维码加载成功");
            if (!ready)
            {
                File.WriteAllText(Path.Combine(output, "douyin-loading-diagnostic.json"), await core.ExecuteScriptAsync("({path:location.pathname,title:document.title,text:document.body?.innerText.slice(0,1500),sdk:!!window.douyinPanel?.showLoginCheck,scripts:document.scripts.length,ready:document.readyState,focused:document.hasFocus(),visibility:document.visibilityState})"));
                throw new InvalidOperationException("Douyin QR did not load; isolated diagnostic saved");
            }
            var stateScript = (string)typeof(PlatformConnectionWindow).GetField("DouyinLoginStateScript", BindingFlags.Static | BindingFlags.NonPublic)!.GetRawConstantValue()!;
            using var state = JsonDocument.Parse(await core.ExecuteScriptAsync(stateScript));
            check(!state.RootElement.GetProperty("authenticated").GetBoolean() && saved == 0, "抖音官网访客不会自动保存");
            File.WriteAllText(Path.Combine(output, "douyin-sdk-api.json"), await core.ExecuteScriptAsync("""
                (()=>{const title=Array.from(document.querySelectorAll('body *')).find(e=>!e.children.length&&e.textContent.trim()==='请登录后继续使用');
                  return {functions:Object.fromEntries(Object.entries(window.douyinPanel).filter(([k,v])=>/login/i.test(k)&&typeof v==='function').map(([k,v])=>[k,String(v)])),
                    scripts:Array.from(document.scripts).map(s=>s.src).filter(Boolean),
                    loginCheck:title?.parentElement.parentElement.outerHTML??''};})()
                """));
            using var layout = JsonDocument.Parse(await core.ExecuteScriptAsync("""
                (()=>{const q=document.querySelector('#douyin_login_comp_scan_code img[aria-label="二维码"]'),r=q.getBoundingClientRect();
                  const phone=document.querySelector('#douyin_login_comp_mobile_code');
                  let visible={left:r.left,top:r.top,right:r.right,bottom:r.bottom};const clippingParents=[],ancestors=[];
                  for(let p=q.parentElement;p;p=p.parentElement){const s=getComputedStyle(p),b=p.getBoundingClientRect();
                    ancestors.push({id:p.id,className:p.className,position:s.position,visibility:s.visibility,transform:s.transform,
                      width:b.width,height:b.height,left:b.left,top:b.top});
                    if(/hidden|clip|scroll|auto/.test(s.overflowX)){visible.left=Math.max(visible.left,b.left);visible.right=Math.min(visible.right,b.right);}
                    if(/hidden|clip|scroll|auto/.test(s.overflowY)){visible.top=Math.max(visible.top,b.top);visible.bottom=Math.min(visible.bottom,b.bottom);}
                    if(visible.right-visible.left<r.width-1||visible.bottom-visible.top<r.height-1)
                      clippingParents.push({id:p.id,width:b.width,height:b.height,overflowX:s.overflowX,overflowY:s.overflowY});
                  }
                  const guide=document.getElementById('animate_qrcode_container').parentElement.querySelector(':scope>p');
                  return {width:r.width,height:r.height,left:r.left,top:r.top,viewportWidth:innerWidth,viewportHeight:innerHeight,
                    instructionsTop:guide?.getBoundingClientRect().top??-1,
                    clipped:clippingParents.length>0,clippingParents,ancestors,
                    styleInstalled:!!document.getElementById('jiying-scan-style'),
                    dialogs:Array.from(document.querySelectorAll('[role="dialog"]')).map(d=>({text:d.innerText.slice(0,250),className:d.className})),
                    phoneHidden:!phone||getComputedStyle(phone).visibility==='hidden',modalCount:document.querySelectorAll('#douyin_login_comp_flat_panel').length};})()
                """));
            var l = layout.RootElement;
            check(l.GetProperty("width").GetDouble() >= 199 && l.GetProperty("height").GetDouble() >= 199
                && l.GetProperty("left").GetDouble() >= 0 && l.GetProperty("top").GetDouble() >= 0
                && l.GetProperty("top").GetDouble() + l.GetProperty("height").GetDouble() <= l.GetProperty("viewportHeight").GetDouble(), "抖音200像素二维码完整显示");
            check(l.GetProperty("phoneHidden").GetBoolean() && l.GetProperty("modalCount").GetInt32() == 1, "抖音扫码隐藏手机号区且不重复创建弹窗");
            check(!l.GetProperty("clipped").GetBoolean(), "抖音二维码不被官网祖先容器裁切");
            check(Math.Abs(l.GetProperty("left").GetDouble() + l.GetProperty("width").GetDouble() / 2
                - l.GetProperty("viewportWidth").GetDouble() / 2) < 3, "抖音二维码在窗口中央显示");
            check(l.GetProperty("instructionsTop").GetDouble() >= l.GetProperty("top").GetDouble()
                + l.GetProperty("height").GetDouble() + 8, "抖音扫码说明显示在二维码下方且不重叠");
            File.WriteAllText(Path.Combine(output, "douyin-qr-layout.json"), l.ToString());
            using (var preview = File.Create(Path.Combine(output, "douyin-qr-webview.png"))) await core.CapturePreviewAsync(CoreWebView2CapturePreviewImageFormat.Png, preview);
            }
            else await Wait(() => Task.FromResult(core.Source.StartsWith("https://www.douyin.com/", StringComparison.Ordinal)));
            var fixtureStateScript = (string)typeof(PlatformConnectionWindow).GetField("DouyinLoginStateScript", BindingFlags.Static | BindingFlags.NonPublic)!.GetRawConstantValue()!;
            var fixture = Path.Combine(output, "douyin-fixture"); Directory.CreateDirectory(fixture);
            File.WriteAllText(Path.Combine(fixture, "index.html"), """
                <!doctype html><title>JIYING_DOUYIN_FIXTURE</title>
                <script>window.fixtureSDKReady=false;window.douyinPanel={showLoginCheck:options=>{
                  if(!window.fixtureSDKReady)throw new Error('Isolated initialization delay');window.fixtureCallbacks=options;}};</script>
                <div id="douyin_login_comp_scan_code"><div id="animate_qrcode_container">
                  <div id="fixture-qr-loading" style="width:200px;height:200px">Isolated loading fixture</div>
                </div></div>
                """);
            core.SetVirtualHostNameToFolderMapping("www.douyin.com", fixture, CoreWebView2HostResourceAccessKind.DenyCors);
            core.Navigate("https://www.douyin.com/index.html");
            if (!await Wait(async () => await core.ExecuteScriptAsync("document.title") == "\"JIYING_DOUYIN_FIXTURE\"")) throw new InvalidOperationException("No isolated fixture");
            await core.ExecuteScriptAsync("document.getElementById('jiying-scan-style')?.remove()");
            check(await Wait(async () => await core.ExecuteScriptAsync("!!document.getElementById('jiying-scan-style')") == "true", 3),
                "官网更新移除扫码样式后自动恢复");
            check(await core.ExecuteScriptAsync("getComputedStyle(document.getElementById('fixture-qr-loading')).display!=='none'") == "true",
                "二维码未返回时保留官网加载占位，避免阻塞出码");
            await core.ExecuteScriptAsync(fixtureStateScript);
            check(await core.ExecuteScriptAsync("window.__jiyingDouyinLoginObserved!==true") == "true", "官网组件尚未就绪时可再次尝试打开扫码");
            await core.ExecuteScriptAsync("window.fixtureScan=document.querySelector('#douyin_login_comp_scan_code');window.fixtureScan.remove();document.body.insertAdjacentHTML('beforeend','<div id=fixture-verification>Isolated verification</div>');document.title='验证码中间页'");
            check(await core.ExecuteScriptAsync("getComputedStyle(document.getElementById('fixture-verification')).visibility!=='hidden'") == "true",
                "官网安全验证页不会被扫码样式遮住");
            check(await Wait(() => Task.FromResult(((TextBlock)connection.FindName("ConnectionStatus")).Text.Contains("安全验证")), 5), "官网安全验证时给出明确提示");
            await core.ExecuteScriptAsync("document.getElementById('fixture-verification').remove();document.body.append(window.fixtureScan);document.title='JIYING_DOUYIN_FIXTURE'");
            await core.ExecuteScriptAsync("""
                const fixtureQr=document.createElement('img');fixtureQr.setAttribute('aria-label','二维码');
                fixtureQr.src='data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/l8sAAAAASUVORK5CYII=';
                document.getElementById('animate_qrcode_container').append(fixtureQr);
                document.getElementById('douyin_login_comp_scan_code').insertAdjacentHTML('beforeend','<p id=fixture-qr-status></p>');
                """);
            foreach (var status in new[] { "二维码失效", "二维码已过期", "二维码已失效", "二维码过期" })
            {
                await core.ExecuteScriptAsync("document.getElementById('fixture-qr-status').textContent=" + JsonSerializer.Serialize(status));
                using var expiryState = JsonDocument.Parse(await core.ExecuteScriptAsync(fixtureStateScript));
                check(expiryState.RootElement.GetProperty("qrStatus").GetString() == status, "抖音识别官网过期提示：" + status);
            }
            check(await Wait(() => Task.FromResult(((TextBlock)connection.FindName("ConnectionStatus")).Text == "二维码过期"), 3),
                "抖音过期状态同步到原生窗口，不误提示继续扫码");
            await core.ExecuteScriptAsync("document.getElementById('fixture-qr-status').remove()");
            await core.ExecuteScriptAsync("""
                window.fixtureSDKReady=true;window.__jiyingDouyinLoginObserved=false;
                const unrelated=document.createElement('div');unrelated.id='fixture-other-prompt';
                unrelated.innerHTML='<div>其他操作</div><button>确定</button>';
                unrelated.querySelector('button').addEventListener('click',()=>{window.fixtureUnrelatedClicks=(window.fixtureUnrelatedClicks||0)+1;});
                const prompt=document.createElement('div');prompt.id='fixture-login-prompt';
                prompt.innerHTML='<div>请登录后继续使用</div><p>为保障更好的访问体验，请在登录后继续使用抖音</p><button>确定</button><button>取消</button>';
                prompt.querySelector('button').addEventListener('click',()=>{window.fixtureLoginConfirmations=(window.fixtureLoginConfirmations||0)+1;});
                document.body.append(unrelated,prompt);
                """);
            check(await Wait(async () => await core.ExecuteScriptAsync("window.fixtureLoginConfirmations===1") == "true", 3),
                "抖音官网登录入口确认自动展开，不要求重复点击确定");
            await Task.Delay(450);
            check(await core.ExecuteScriptAsync("window.fixtureLoginConfirmations===1&&!window.fixtureUnrelatedClicks") == "true",
                "官网登录入口只展开一次，其他确定按钮保持不动");
            await core.ExecuteScriptAsync("document.getElementById('fixture-login-prompt').remove();document.getElementById('fixture-other-prompt').remove()");
            var cookie = core.CookieManager.CreateCookie("sessionid", "isolated-fixture", ".douyin.com", "/"); cookie.IsSecure = cookie.IsHttpOnly = true;
            core.CookieManager.AddOrUpdateCookie(cookie);
            await core.ExecuteScriptAsync("document.cookie='=isolated-unnamed; Path=/; Secure'");
            check((await core.CookieManager.GetCookiesAsync("https://www.douyin.com/")).Any(c => c.Name.Length == 0),
                "隔离浏览器覆盖官网无名Cookie情况");
            var sessionReadable = false;
            try { var exported = await connection.ReadSessionCookiesAsync(); sessionReadable = exported.Any(c => c.Name == "sessionid") && exported.All(c => !string.IsNullOrWhiteSpace(c.Name)); }
            catch { }
            check(sessionReadable, "官网无名Cookie不阻断有效登录会话读取");
            await core.ExecuteScriptAsync(fixtureStateScript);
            check(saved == 0 && connection.IsVisible, "抖音有Cookie但无官网成功回调仍不完成");
            await core.ExecuteScriptAsync("window.fixtureCallbacks.onSuccess()");
            var completed = await Wait(() => Task.FromResult(!connection.IsVisible), 10);
            check(completed, "抖音官网确认回调后自动关闭扫码窗口");
            if (!completed)
            {
                var cookies = await core.CookieManager.GetCookiesAsync("https://www.douyin.com/");
                var conversions = new List<object>();
                foreach (var item in cookies)
                {
                    try { _ = item.ToSystemNetCookie(); conversions.Add(new { item.Name, item.Domain, item.IsSession, converted = true, error = "" }); }
                    catch (Exception e) { conversions.Add(new { item.Name, item.Domain, item.IsSession, converted = false, error = e.GetType().Name }); }
                }
                File.WriteAllText(Path.Combine(output, "douyin-completion-diagnostic.json"), JsonSerializer.Serialize(new {
                    state = JsonDocument.Parse(await core.ExecuteScriptAsync(fixtureStateScript)).RootElement.Clone(),
                    saved, status = ((TextBlock)connection.FindName("ConnectionStatus")).Text, cookies = conversions,
                    saveFailed = typeof(PlatformConnectionWindow).GetField("_saveFailed", BindingFlags.Instance | BindingFlags.NonPublic)!.GetValue(connection)
                }));
            }
        }
    }

    private static async Task RunHomepage(MainWindow owner, string output, Action<bool, string> check)
    {
        const string homepage = "https://www.douyin.com/?recommend=1";
        var saved = 0;
        var connection = new PlatformConnectionWindow(owner, "douyin", homepage, _ => { saved++; return Task.CompletedTask; });
        var finished = new TaskCompletionSource();
        _ = owner.Dispatcher.InvokeAsync(async () =>
        {
            try
            {
                var browser = (WebView2)connection.FindName("Website");
                if (!await Wait(() => Task.FromResult(browser.CoreWebView2?.Source.StartsWith("https://www.douyin.com/", StringComparison.Ordinal) == true)))
                    throw new InvalidOperationException("Homepage WebView did not initialize");
                var core = browser.CoreWebView2;
                core.AddWebResourceRequestedFilter("*", CoreWebView2WebResourceContext.Document, CoreWebView2WebResourceRequestSourceKinds.Document);
                core.WebResourceRequested += (_, e) =>
                {
                    if (Uri.TryCreate(e.Request.Uri, UriKind.Absolute, out var address) && address.Host == "www.douyin.com" && address.AbsolutePath is "/" or "/index.html")
                        e.Response = core.Environment.CreateWebResourceResponse(new MemoryStream(File.ReadAllBytes(Path.Combine(output, "douyin-fixture", "index.html"))),
                            200, "OK", "Content-Type: text/html; charset=utf-8");
                };
                core.Navigate("https://www.douyin.com/index.html");
                if (!await Wait(async () => await core.ExecuteScriptAsync("document.title") == "\"JIYING_DOUYIN_FIXTURE\""))
                    throw new InvalidOperationException("Homepage fixture did not load");
                var cookie = core.CookieManager.CreateCookie("sessionid", "isolated-fixture", ".douyin.com", "/");
                cookie.IsSecure = cookie.IsHttpOnly = true; core.CookieManager.AddOrUpdateCookie(cookie);
                var script = (string)typeof(PlatformConnectionWindow).GetField("DouyinLoginStateScript", BindingFlags.Static | BindingFlags.NonPublic)!.GetRawConstantValue()!;
                await core.ExecuteScriptAsync("window.fixtureSDKReady=true");
                await core.ExecuteScriptAsync(script);
                await core.ExecuteScriptAsync("window.fixtureCallbacks.onSuccess()");
                check(await Wait(() => Task.FromResult(connection.Width == 1040)) && saved == 1 && connection.VideoUrl.Length == 0,
                    "抖音隔离首页扫码后保持窗口并展开视频选择");
                await Task.Delay(600);
                File.WriteAllText(Path.Combine(output, "douyin-picker-diagnostic.json"), JsonSerializer.Serialize(new {
                    document = JsonDocument.Parse(await core.ExecuteScriptAsync("({path:location.pathname,query:location.search,style:!!document.getElementById('jiying-scan-style')})")).RootElement.Clone(),
                    status = ((TextBlock)connection.FindName("ConnectionStatus")).Text,
                    saveFailed = typeof(PlatformConnectionWindow).GetField("_saveFailed", BindingFlags.Instance | BindingFlags.NonPublic)!.GetValue(connection)
                }));
                check(await Wait(async () => await core.ExecuteScriptAsync("!document.getElementById('jiying-scan-style') && location.search==='?recommend=1'") == "true", 10),
                    "选择视频时移除扫码样式并打开原推荐首页");
                var use = (Button)connection.FindName("UseVideoButton");
                var pageReady = await Wait(() => Task.FromResult(use.IsEnabled), 10);
                check(pageReady, "官网页面完成后可选择视频");
                if (!pageReady) throw new InvalidOperationException("Fixture homepage did not finish loading");
                use.RaiseEvent(new RoutedEventArgs(Button.ClickEvent));
                check(connection.IsVisible && saved == 1 && connection.VideoUrl.Length == 0, "推荐首页自身不会误作下载视频");
                core.Navigate(homepage + "&modal_id=12345");
                if (!await Wait(() => Task.FromResult(use.IsEnabled && browser.Source?.Query.Contains("modal_id=12345") == true)))
                    throw new InvalidOperationException("Selected video did not load");
                use.RaiseEvent(new RoutedEventArgs(Button.ClickEvent));
                check(await Wait(() => Task.FromResult(!connection.IsVisible)), "首页选择具体视频后可完成连接");
                finished.SetResult();
            }
            catch (Exception e) { finished.SetException(e); }
            finally { if (connection.IsVisible) connection.Close(); }
        });
        var result = connection.ShowDialog();
        await finished.Task;
        check(result == true && saved == 2 && connection.VideoUrl.Contains("modal_id=12345"), "隔离首页连接保留选中的具体视频");
    }

    private static async Task<bool> Wait(Func<Task<bool>> condition, int seconds = 75)
    {
        var deadline = DateTime.UtcNow.AddSeconds(seconds);
        while (DateTime.UtcNow < deadline)
        {
            try { if (await condition()) return true; } catch { }
            await Task.Delay(150);
        }
        return false;
    }
}
