using System.Text.Json;
using Microsoft.Web.WebView2.Core;

namespace Jiying.Desktop;

public partial class PlatformConnectionWindow
{
    internal const string DouyinQrStyleScript = """
        (()=>{
          if (!/(^|\.)douyin\.com$/.test(location.hostname)) return;
          const install=()=>{
            if (!document.documentElement || document.getElementById('jiying-scan-style')) return;
            const style=document.createElement('style');style.id='jiying-scan-style';
            style.textContent=`
              html:has(#douyin_login_comp_scan_code),body:has(#douyin_login_comp_scan_code) {margin:0!important;overflow:hidden!important;background:#fff!important;}
              body:has(#douyin_login_comp_scan_code)>* {visibility:hidden!important;}
              [role="dialog"],[role="dialog"] *,[id*="captcha"],[id*="captcha"] *,
              [class*="captcha"],[class*="captcha"] *,iframe {visibility:visible!important;}
              #douyin_login_comp_scan_code,#douyin_login_comp_scan_code * {visibility:visible!important;}
              #douyin_login_comp_scan_code {position:fixed!important;inset:0!important;
                display:flex!important;align-items:center!important;justify-content:center!important;background:#fff!important;}
              #animate_qrcode_container {width:auto!important;height:auto!important;padding:16px!important;margin:0!important;transform:none!important;}
              #animate_qrcode_container>div:has(>img[aria-label="二维码"]) {position:relative!important;inset:auto!important;width:200px!important;height:200px!important;box-sizing:content-box!important;overflow:visible!important;}
              #animate_qrcode_container:has(img[aria-label="二维码"])>div:not(:has(img[aria-label="二维码"])),#default_scan_code_guide {display:none!important;}
              #douyin_login_comp_scan_code img[aria-label="二维码"] {display:block!important;width:200px!important;height:200px!important;}
              #douyin_login_comp_scan_code {color:#444!important;}
            `;
            (document.head||document.documentElement).appendChild(style);
          };
          install();if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',install,{once:true});
        })();
        """;
    internal const string DouyinLoginStateScript = """
        (()=>{
          if (!window.__jiyingDouyinLoginObserved && typeof window.douyinPanel?.showLoginCheck==='function') {
            window.__jiyingDouyinLoginObserved=true;
            window.__jiyingDouyinLoginAccepted=false;
            try { window.douyinPanel.showLoginCheck({onSuccess:()=>{window.__jiyingDouyinAuthenticated=true;},
              onCancel:()=>{window.__jiyingDouyinLoginObserved=false;}}); }
            catch { window.__jiyingDouyinLoginObserved=false; }
          }
          if (window.__jiyingDouyinLoginObserved && !window.__jiyingDouyinLoginAccepted) {
            const title=Array.from(document.querySelectorAll('div')).find(e=>!e.children.length&&e.textContent.trim()==='请登录后继续使用');
            const panel=title?.parentElement;
            const confirm=Array.from(panel?.querySelectorAll('button')||[]).find(b=>b.textContent.trim()==='确定'&&!b.disabled);
            if (confirm && panel.textContent.includes('为保障更好的访问体验，请在登录后继续使用抖音')) {
              window.__jiyingDouyinLoginAccepted=true;confirm.click();
            }
          }
          const scan=document.querySelector('#douyin_login_comp_scan_code');
          const qr=scan?.querySelector('img[aria-label="二维码"]');
          const verification=/验证码|安全验证/.test(document.title)||Array.from(document.querySelectorAll('[id*="captcha"],[class*="captcha"],iframe'))
            .some(e=>{const r=e.getBoundingClientRect();return r.width>100&&r.height>40&&getComputedStyle(e).visibility!=='hidden';});
          return {authenticated:window.__jiyingDouyinAuthenticated===true,verification,
            loginReady:typeof window.douyinPanel?.showLoginCheck==='function',
            qrReady:!!qr&&qr.complete&&qr.naturalWidth>0,
            qrStatus:scan?.innerText.match(/二维码(?:已)?(?:过期|失效)|扫码成功|请在手机上确认/)?.[0]||''};
        })()
        """;
    internal const string DouyinRefreshScript = """
        (()=>{
          const scan=document.querySelector('#douyin_login_comp_scan_code');
          const refresh=Array.from(scan?.querySelectorAll('div,span,button')||[])
            .filter(e=>e.innerText?.trim()==='点击刷新' && e.getBoundingClientRect().width>0).at(-1);
          if (!refresh) return false;refresh.click();return true;
        })()
        """;

    private static bool DeferScanResource(string url, CoreWebView2WebResourceContext context, bool selectingVideo)
    {
        if (selectingVideo) return false;
        if (context == CoreWebView2WebResourceContext.Media) return true;
        if (context != CoreWebView2WebResourceContext.Image || !Uri.TryCreate(url, UriKind.Absolute, out var uri)) return false;
        return uri.Host.StartsWith("sns-webpic-", StringComparison.OrdinalIgnoreCase) && uri.Host.EndsWith(".xhscdn.com", StringComparison.OrdinalIgnoreCase)
            || uri.Host.EndsWith("-pc-sign.douyinpic.com", StringComparison.OrdinalIgnoreCase)
            || uri.Host.Equals("p-pc-weboff.byteimg.com", StringComparison.OrdinalIgnoreCase);
    }

    private async Task InitializeDouyinAsync(CoreWebView2 core)
    {
        _douyinStyleId = await core.AddScriptToExecuteOnDocumentCreatedAsync(DouyinQrStyleScript);
        if (_closed) return;
        core.NavigationCompleted += (_, e) =>
        {
            if (_closed) return;
            ReloadButton.IsEnabled = true;
            UseVideoButton.IsEnabled = _selectingVideo && e.IsSuccess;
            if (!e.IsSuccess) ConnectionStatus.Text = "网页未能加载，请检查网络后刷新。";
        };
        ConnectionStatus.Text = "正在加载二维码…";
        core.Navigate("https://www.douyin.com/");
        while (!_closed && !_selectingVideo)
        {
            try
            {
                if (!_saveFailed)
                {
                    using var json = JsonDocument.Parse(await core.ExecuteScriptAsync(DouyinQrStyleScript + "\n" + DouyinLoginStateScript));
                    if (_closed) return;
                    var state = json.RootElement;
                    if (state.ValueKind == JsonValueKind.Object)
                    {
                        RecoverQrLoad(core, state);
                        if (state.GetProperty("authenticated").GetBoolean())
                        {
                            if (await TrySaveDouyinLoginAsync(true, await ReadSessionCookiesAsync()))
                            { if (!_closed) DialogResult = true; return; }
                        }
                        else if (state.GetProperty("verification").GetBoolean())
                            ConnectionStatus.Text = "请先完成官网安全验证，完成后将自动显示二维码。";
                        else if (state.GetProperty("qrReady").GetBoolean())
                        {
                            var status = state.GetProperty("qrStatus").GetString();
                            ConnectionStatus.Text = string.IsNullOrWhiteSpace(status) ? "等待扫码，手机确认后会自动继续。" : status;
                        }
                    }
                }
            }
            catch (Exception) { if (_closed) return; }
            await Task.Delay(200);
        }
    }
    private string? _douyinStyleId;

    private async Task<bool> TrySaveDouyinLoginAsync(bool authenticated, IReadOnlyList<WebsiteCookie> cookies)
    {
        if (_closed || _savingSession || _sessionSaved || !authenticated || !cookies.Any(c =>
            c.Name is "sessionid" or "sessionid_ss" && !string.IsNullOrWhiteSpace(c.Value)
            && (c.Domain.TrimStart('.').Equals("douyin.com", StringComparison.OrdinalIgnoreCase)
                || c.Domain.EndsWith(".douyin.com", StringComparison.OrdinalIgnoreCase))
            && (c.Expires == 0 || c.Expires > DateTimeOffset.UtcNow.ToUnixTimeSeconds()))) return false;
        _savingSession = true;
        ConnectionStatus.Text = "登录成功，正在连接…";
        try
        {
            await _save(cookies);
            if (_closed) return false;
            _sessionSaved = true;
            var shortVideo = Uri.TryCreate(_originalUrl, UriKind.Absolute, out var uri)
                && uri.Host.Equals("v.douyin.com", StringComparison.OrdinalIgnoreCase) && uri.AbsolutePath.Length > 1;
            if (shortVideo || SelectedVideoUrl("douyin", _originalUrl, "").Length > 0)
            { VideoUrl = _originalUrl; return true; }
            ShowDouyinVideoPicker();
            return false;
        }
        catch (Exception)
        {
            if (!_closed) { _saveFailed = true; ReloadButton.Content = "重试连接"; ConnectionStatus.Text = "扫码已完成，但连接保存失败，请重试连接。"; }
            return false;
        }
        finally { _savingSession = false; }
    }

    private void ShowDouyinVideoPicker()
    {
        _selectingVideo = true;
        MinWidth = 720; MinHeight = 540; Width = 1040; Height = 760; ResizeMode = System.Windows.ResizeMode.CanResize;
        ConnectionTitle.Text = "选择抖音视频";
        Title = ConnectionTitle.Text + " · 即影";
        var area = System.Windows.SystemParameters.WorkArea;
        Left = Math.Clamp(Owner.Left + (Owner.ActualWidth - Width) / 2, area.Left, Math.Max(area.Left, area.Right - Width));
        Top = Math.Clamp(Owner.Top + (Owner.ActualHeight - Height) / 2, area.Top, Math.Max(area.Top, area.Bottom - Height));
        ConnectionInstructions.Text = "已连接账号。在官网打开要下载的视频，点击“使用此视频”。";
        ConnectionStatus.Text = "请选择要下载的具体视频。";
        HomeButton.Visibility = UseVideoButton.Visibility = System.Windows.Visibility.Visible;
        HomeButton.IsEnabled = true; ReloadButton.Content = "刷新网页";
        if (Website.CoreWebView2 is not { } core) return;
        if (_douyinStyleId is not null) core.RemoveScriptToExecuteOnDocumentCreated(_douyinStyleId);
        core.Navigate(_originalUrl);
    }
}
