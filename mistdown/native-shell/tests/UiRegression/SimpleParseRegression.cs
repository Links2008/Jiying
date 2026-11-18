using System.Net;
using System.Net.Http;
using System.Text;
using System.Windows;
using System.Windows.Controls;
using Jiying.Desktop;

internal static class SimpleParseRegression
{
    internal static async Task Run(MainWindow window, Action<bool, string> check)
    {
        var engine = window.Engine;
        var previousApi = engine.RequestTransport;
        var previousPort = engine.Port;
        var input = window.UrlBox;
        var previousUrl = input.Text;
        using var handler = new Handler();
        using var client = new HttpClient(handler);
        engine.RequestTransport = client;
        engine.Port = 12345;
        Task Probe() => window.ProbeAsync(false);
        try
        {
            foreach (var url in new[] { "https://www.bilibili.com/video/BVsample", "https://v.douyin.com/sample/", "https://xhslink.com/o/sample" })
            {
                input.Text = url;
                check(window.BilibiliLoginBar.Visibility == Visibility.Collapsed,
                    "粘贴平台链接时不提前显示账号配置：" + new Uri(url).Host);
            }
            check(handler.Probes == 0 && handler.Submissions == 0, "填写链接不解析，也不提交下载");
            await Probe();
            check(handler.Probes == 1 && window.ProbePanel.Visibility == Visibility.Visible,
                "显式解析后显示视频与画质");
            check(window.FormatList.SelectedItem is FormatInfo && handler.Submissions == 0,
                "解析完成预选画质，仍等待用户点击下载");
            handler.MoreQuality = true;
            await Probe();
            check(window.AccountQualityButton.Visibility == Visibility.Visible,
                "小红书公开档位较低时提供更多画质入口");
            handler.MoreQuality = false;
            handler.AuthRequired = true;
            await Probe();
            check(window.BilibiliLoginBar.Visibility == Visibility.Visible &&
                window.ParseFailure.Visibility == Visibility.Collapsed,
                "需要登录时只显示一个可继续的账号入口");
            handler.AuthRequired = false;
            await Probe();
            check(window.BilibiliLoginBar.Visibility == Visibility.Collapsed,
                "登录后解析成功收起账号区域");
            handler.Gate = new(TaskCreationOptions.RunContinuationsAsynchronously);
            var before = handler.Probes;
            var first = Probe();
            var second = Probe();
            check(handler.Probes == before + 1, "连续触发解析只发送一次请求");
            input.Text = "https://www.bilibili.com/video/BVnext";
            handler.Gate.TrySetResult();
            await Task.WhenAll(first, second);
            check(window.ProbePanel.Visibility == Visibility.Collapsed,
                "换链接后旧响应不能显示为新链接结果");
        }
        finally
        {
            input.Text = previousUrl;
            engine.RequestTransport = previousApi;
            engine.Port = previousPort;
        }
    }

    private sealed class Handler : HttpMessageHandler
    {
        internal int Probes, Submissions;
        internal bool AuthRequired;
        internal bool MoreQuality;
        internal TaskCompletionSource? Gate;
        protected override async Task<HttpResponseMessage> SendAsync(HttpRequestMessage request, CancellationToken token)
        {
            var path = request.RequestUri!.AbsolutePath;
            if (path == "/api/v1/tasks" && request.Method == HttpMethod.Post) Submissions++;
            if (path != "/api/v1/probe")
                return new(HttpStatusCode.OK) { Content = new StringContent("{}", Encoding.UTF8, "application/json") };
            Probes++;
            if (Gate is not null) await Gate.Task; // 模拟已发出的响应晚于用户切换链接。
            var body = AuthRequired
                ? """{"detail":{"code":"E-AUTH-301","message":"请登录","suggestion":"连接后继续"}}"""
                : """{"title":"隔离平台测试","duration":15,"quality_notice":"NOTICE","formats":[{"format_id":"best","height":1080,"label":"1080p","ext":"mp4"}]}""".Replace("NOTICE", MoreQuality ? "当前最高 1080p。可连接小红书尝试更多画质。" : "");
            return new(AuthRequired ? HttpStatusCode.Forbidden : HttpStatusCode.OK)
                { Content = new StringContent(body, Encoding.UTF8, "application/json") };
        }
    }
}
