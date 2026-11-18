namespace Jiying.Desktop;

internal static class Program
{
    [STAThread]
    private static int Main(string[] args)
    {
        // OCR 命令不构造 Application，也不占用桌面单实例或通知入口。
        if (args is ["--ocr-worker", var pipe])
            return Task.Run(() => FrameOcr.ServeAsync(pipe)).GetAwaiter().GetResult();
        if (args is ["--ocr-probe", var probe])
            return Task.Run(() => FrameOcr.ProbeAsync(probe)).GetAwaiter().GetResult();
        if (args is ["--ocr-frames", var directory, var output])
            return Task.Run(() => FrameOcr.ScanFileBatchAsync(directory, output)).GetAwaiter().GetResult();
        if (args.Length > 0 && args[0] is "--ocr-worker" or "--ocr-probe" or "--ocr-frames") return 2;

        var app = new App();
        app.InitializeComponent();
        return app.Run();
    }
}
