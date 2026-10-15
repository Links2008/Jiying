// 实际事件循环只使用隔离 HTTP 处理器，检查旧监听错误不能切断重连后的引擎。
using System.Net.Http;
using System.Reflection;
using System.Windows.Controls;
using Jiying.Desktop;

internal static class EventConnectionRegression
{
    private const BindingFlags Flags = BindingFlags.Instance | BindingFlags.NonPublic;

    public static async Task Run(MainWindow window)
    {
        foreach (var samePort in new[] { false, true }) await RunOne(window, samePort);
        Console.WriteLine("通过：实际 WPF 旧事件监听晚到错误保留新连接与连接提示，并继续监听新连接；同端口重连同样受保护。");
    }

    private static async Task RunOne(MainWindow window, bool samePort)
    {
        var engine = typeof(MainWindow).GetField("_engine", Flags)!.GetValue(window)!;
        var eventsField = engine.GetType().GetField("_events", Flags)!;
        var portProperty = engine.GetType().GetProperty("Port")!;
        var originalEvents = (HttpClient)eventsField.GetValue(engine)!;
        var originalPort = portProperty.GetValue(engine);
        var connectionText = (TextBlock)window.FindName("ConnectionText");
        var originalText = connectionText.Text;
        var setConnection = typeof(MainWindow).GetMethod("SetConnection", Flags)!;
        using var handler = new DelayedEventHandler();
        using var events = new HttpClient(handler, disposeHandler: false) { Timeout = Timeout.InfiniteTimeSpan };
        using var stop = new CancellationTokenSource();
        Task? listening = null;
        try
        {
            eventsField.SetValue(engine, events);
            portProperty.SetValue(engine, 12345);
            listening = (Task)typeof(MainWindow).GetMethod("EventLoopAsync", Flags)!.Invoke(window, [stop.Token])!;
            await handler.FirstStarted.Task.WaitAsync(TimeSpan.FromSeconds(5));
            var newPort = samePort ? 12345 : 12346;
            portProperty.SetValue(engine, newPort);
            var newHint = "隔离测试的新连接 · " + newPort;
            setConnection.Invoke(window, [true, newHint]);
            handler.ReleaseFirst.TrySetResult();
            await handler.FirstFailed.Task.WaitAsync(TimeSpan.FromSeconds(5));
            // 让实际事件循环处理异常，防止只在抛错前检查端口。
            await Task.Delay(150);
            Require((int?)portProperty.GetValue(engine) == newPort,
                "旧事件监听异常清除了重连后的引擎端口");
            Require(connectionText.Text == newHint,
                "旧事件监听异常覆盖了新连接的提示");
            await handler.SecondStarted.Task.WaitAsync(TimeSpan.FromSeconds(5));
            Require(handler.SecondPort == newPort, "事件循环未继续监听重连后的引擎");
            Require(connectionText.Text == newHint, "开始新监听后仍显示旧连接的错误提示");
        }
        finally
        {
            // 只取消本次反射启动的循环，保留窗口及其他专项的生命周期。
            stop.Cancel();
            handler.ReleaseFirst.TrySetResult();
            try
            {
                if (listening is not null) await listening.WaitAsync(TimeSpan.FromSeconds(5));
            }
            finally
            {
                eventsField.SetValue(engine, originalEvents);
                portProperty.SetValue(engine, originalPort);
                setConnection.Invoke(window, [originalPort is not null, originalText]);
            }
        }
    }

    private static void Require(bool condition, string message)
    {
        if (!condition) throw new Exception(message);
    }

    private sealed class DelayedEventHandler : HttpMessageHandler
    {
        private int _requests;
        public TaskCompletionSource FirstStarted { get; } = new(TaskCreationOptions.RunContinuationsAsynchronously);
        public TaskCompletionSource ReleaseFirst { get; } = new(TaskCreationOptions.RunContinuationsAsynchronously);
        public TaskCompletionSource FirstFailed { get; } = new(TaskCreationOptions.RunContinuationsAsynchronously);
        public TaskCompletionSource SecondStarted { get; } = new(TaskCreationOptions.RunContinuationsAsynchronously);
        public int SecondPort { get; private set; }

        protected override async Task<HttpResponseMessage> SendAsync(HttpRequestMessage request, CancellationToken cancellationToken)
        {
            Require(request.RequestUri?.AbsolutePath == "/api/v1/events", "隔离事件专项发出了非事件请求");
            if (Interlocked.Increment(ref _requests) == 1)
            {
                FirstStarted.TrySetResult();
                await ReleaseFirst.Task.WaitAsync(cancellationToken);
                FirstFailed.TrySetResult();
                throw new HttpRequestException("隔离测试：旧连接晚到的监听错误");
            }
            SecondPort = request.RequestUri!.Port;
            SecondStarted.TrySetResult();
            // 保持新监听存活到专项清理，不发事件，避免触发任务刷新。
            await Task.Delay(Timeout.InfiniteTimeSpan, cancellationToken);
            throw new InvalidOperationException("隔离事件监听应由取消结束");
        }
    }
}
