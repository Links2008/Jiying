using System.Text.Json;
using Microsoft.Web.WebView2.Core;

namespace Jiying.Desktop;

public partial class PlatformConnectionWindow
{
    private bool _savingSession, _sessionSaved, _saveFailed;
    private void RecoverQrLoad(CoreWebView2 core, JsonElement state)
    {
        if (state.GetProperty("qrReady").GetBoolean() || state.GetProperty("authenticated").GetBoolean())
        { _qrLoadRetried = true; return; }
        if (_qrLoadRetried || _closed || _saveFailed || _selectingVideo
            || DateTime.UtcNow - _qrNavigationStarted < TimeSpan.FromSeconds(_platform == "douyin" ? 20 : 10)
            || _platform == "douyin" && state.GetProperty("loginReady").GetBoolean()
            || state.GetProperty("verification").GetBoolean()) return;
        _qrLoadRetried = true;
        ConnectionStatus.Text = "官网加载较慢，正在重试二维码…";
        core.Navigate(_platform == "douyin" ? "https://www.douyin.com/" : "https://www.xiaohongshu.com/explore");
    }
    // Keep the official QR's expiry, refresh and verification handlers intact.
    internal const string XiaohongshuQrStyleScript = """
        (() => {
          if (!/(^|\.)xiaohongshu\.com$/.test(location.hostname)) return;
          const install = () => {
            if (!document.documentElement || document.getElementById('jiying-scan-style')) return;
            const style=document.createElement('style'); style.id='jiying-scan-style';
            style.textContent=`
              html,body {margin:0!important;overflow:hidden!important;background:#fff!important;}
              .main-container,.header-container,.reds-mask,.login-container>.close {display:none!important;}
              .login-modal {position:fixed!important;inset:0!important;background:#fff!important;}
              .login-container {position:fixed!important;inset:0!important;transform:none!important;
                width:100%!important;height:100%!important;border-radius:0!important;box-shadow:none!important;background:#fff!important;}
              .login-container>.container {height:100%!important;padding:0!important;
                display:flex!important;align-items:center!important;justify-content:center!important;background:#fff!important;}
              .login-container>.container>:not(.code-area),.course-video {display:none!important;}
              .login-container .code-area {margin:0!important;padding:16px!important;border:0!important;}
              .login-container .qrcode {width:232px!important;height:232px!important;padding:16px!important;}
              .login-container .qrcode-img {width:200px!important;height:200px!important;max-width:none!important;}
              .login-container .tip,.login-container .tip>* {color:#444!important;}
            `;
            (document.head||document.documentElement).appendChild(style);
          };
          install(); document.addEventListener('DOMContentLoaded',install,{once:true});
        })();
        """;
    internal const string XiaohongshuLoginStateScript = """
        (() => {
          const flag=window.__INITIAL_STATE__?.user?.loggedIn;
          const value=typeof flag==='boolean'?flag:flag?.value;
          const modal=document.querySelector('.login-container');
          const profile=document.querySelector('.bottom-channel a[href^="/user/profile/"]');
          const authenticated=typeof value==='boolean'?value:!!profile&&!modal;
          const qr=modal?.querySelector('.qrcode-img');
          if (!authenticated && !modal && !window.__jiyingLoginRequested) {
            const trigger=document.querySelector('.bottom-menu-component');
            if (trigger) {window.__jiyingLoginRequested=true; trigger.click();}
          }
          const verification=Array.from(document.querySelectorAll('[id*="captcha"],[class*="captcha"],iframe'))
            .some(e=>{const r=e.getBoundingClientRect();return r.width>100&&r.height>40&&getComputedStyle(e).visibility!=='hidden';});
          return {authenticated,qrReady:!!qr&&qr.complete&&qr.naturalWidth>0,verification,
            qrStatus:modal?.querySelector('.qrcode .status')?.innerText||''};
        })();
        """;
    internal const string XiaohongshuRefreshScript = """
        (()=>{
          const refresh=document.querySelector('.qrcode .status .refresh');
          if (!refresh) return false;refresh.click();return true;
        })()
        """;

    private async Task InitializeXiaohongshuAsync(CoreWebView2 core)
    {
        await core.AddScriptToExecuteOnDocumentCreatedAsync(XiaohongshuQrStyleScript);
        if (_closed) return;
        core.NavigationCompleted += (_, e) =>
        {
            if (_closed) return;
            ReloadButton.IsEnabled = true;
            if (!e.IsSuccess) ConnectionStatus.Text = "二维码未能加载，请检查网络后刷新。";
        };
        ConnectionStatus.Text = "正在加载二维码…";
        core.Navigate("https://www.xiaohongshu.com/explore");
        await WatchXiaohongshuLoginAsync();
    }
    private async Task WatchXiaohongshuLoginAsync()
    {
        while (!_closed)
        {
            try
            {
                var core = Website.CoreWebView2;
                if (core is not null && !_saveFailed)
                {
                    using var json = JsonDocument.Parse(await core.ExecuteScriptAsync(XiaohongshuLoginStateScript));
                    if (_closed) return;
                    var state = json.RootElement;
                    if (state.ValueKind == JsonValueKind.Object)
                    {
                        RecoverQrLoad(core, state);
                        if (state.GetProperty("authenticated").GetBoolean())
                        {
                            if (await TrySaveXiaohongshuLoginAsync(true, await ReadSessionCookiesAsync()))
                            { if (!_closed) DialogResult = true; return; }
                        }
                        else if (state.GetProperty("verification").GetBoolean())
                            ConnectionStatus.Text = "请先完成官网安全验证，完成后将自动显示二维码。";
                        else if (state.GetProperty("qrReady").GetBoolean())
                        {
                            var status = state.GetProperty("qrStatus").GetString();
                            ConnectionStatus.Text = string.IsNullOrWhiteSpace(status)
                                ? "等待扫码，手机确认后会自动继续。" : status;
                        }
                    }
                }
            }
            catch (Exception) { if (_closed) return; }
            await Task.Delay(200);
        }
    }

    private async Task<bool> TrySaveXiaohongshuLoginAsync(bool authenticated, IReadOnlyList<WebsiteCookie> cookies)
    {
        var now = DateTimeOffset.UtcNow.ToUnixTimeSeconds();
        if (_closed || _savingSession || _sessionSaved || !authenticated || !cookies.Any(cookie =>
            cookie.Name == "web_session" && !string.IsNullOrWhiteSpace(cookie.Value)
            && (cookie.Domain.TrimStart('.').Equals("xiaohongshu.com", StringComparison.OrdinalIgnoreCase)
                || cookie.Domain.EndsWith(".xiaohongshu.com", StringComparison.OrdinalIgnoreCase))
            && (cookie.Expires == 0 || cookie.Expires > now))) return false;
        _savingSession = true;
        ConnectionStatus.Text = "登录成功，正在连接…";
        try
        {
            await _save(cookies);
            if (_closed) return false;
            _sessionSaved = true;
            VideoUrl = _originalUrl;
            ConnectionStatus.Text = "已连接，继续解析…";
            return true;
        }
        catch (Exception)
        {
            if (!_closed)
            {
                _saveFailed = true;
                ReloadButton.Content = "重试连接";
                ConnectionStatus.Text = "扫码已完成，但连接保存失败，请重试连接。";
            }
            return false;
        }
        finally { _savingSession = false; }
    }
}
