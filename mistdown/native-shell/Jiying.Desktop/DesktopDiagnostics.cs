using System.Diagnostics;
using System.IO;

namespace Jiying.Desktop;

internal static class DesktopDiagnostics
{
    private const long MaxBytes = 512 * 1024;
    private static readonly object FileGate = new();

    public static void Write(string operation, Exception error)
    {
        try
        {
            var path = Path.Combine(Path.GetDirectoryName(UiSettings.NotificationStatusPath)!, "desktop-errors.log");
            Directory.CreateDirectory(Path.GetDirectoryName(path)!);
            // 仅记录程序自己的方法名与错误类型。异常正文、文件名和请求内容可能含凭据。
            var frames = new StackTrace(error, false).GetFrames()
                .Select(frame => frame.GetMethod())
                .Where(method => method?.DeclaringType?.Assembly == typeof(DesktopDiagnostics).Assembly)
                .Take(12)
                .Select(method => $"{method!.DeclaringType!.Name}.{method.Name}");
            var line = $"{DateTimeOffset.Now:O} | {operation} | {error.GetType().Name} 0x{error.HResult:X8} | {string.Join(" > ", frames)}";
            lock (FileGate)
            {
                if (File.Exists(path) && new FileInfo(path).Length >= MaxBytes)
                    File.Move(path, path + ".1", overwrite: true);
                File.AppendAllText(path, line + Environment.NewLine);
            }
        }
        catch (Exception failure) when (failure is IOException or UnauthorizedAccessException or ArgumentException or NotSupportedException) { }
    }
}
