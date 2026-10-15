// 按已观察的窗口句柄截取隔离测试窗口，不要求用户切换当前前台应用。
using System.Runtime.InteropServices;
using System.Windows;
using System.Windows.Interop;
using System.Windows.Media.Imaging;

internal static class NativeWindowCapture
{
    [StructLayout(LayoutKind.Sequential)] private struct Rectangle { internal int Left, Top, Right, Bottom; }
    [DllImport("user32.dll")] private static extern bool GetWindowRect(nint window, out Rectangle rectangle);
    [DllImport("user32.dll")] private static extern bool PrintWindow(nint window, nint dc, uint flags);
    [DllImport("user32.dll")] private static extern nint GetDC(nint window);
    [DllImport("user32.dll")] private static extern int ReleaseDC(nint window, nint dc);
    [DllImport("gdi32.dll")] private static extern nint CreateCompatibleDC(nint dc);
    [DllImport("gdi32.dll")] private static extern nint CreateCompatibleBitmap(nint dc, int width, int height);
    [DllImport("gdi32.dll")] private static extern nint SelectObject(nint dc, nint item);
    [DllImport("gdi32.dll")] private static extern bool DeleteObject(nint item);
    [DllImport("gdi32.dll")] private static extern bool DeleteDC(nint dc);

    internal static BitmapSource Capture(Window window)
    {
        var handle = new WindowInteropHelper(window).Handle;
        if (!window.IsVisible || handle == 0 || !GetWindowRect(handle, out var bounds))
            throw new InvalidOperationException("隔离预览窗口不可读取，停止截图。");
        var screen = GetDC(0);
        var target = CreateCompatibleDC(screen);
        var bitmap = CreateCompatibleBitmap(screen, bounds.Right - bounds.Left, bounds.Bottom - bounds.Top);
        var previous = SelectObject(target, bitmap);
        try
        {
            if (!PrintWindow(handle, target, 2))
                throw new InvalidOperationException("窗口截图失败。");
            var result = Imaging.CreateBitmapSourceFromHBitmap(bitmap, 0, Int32Rect.Empty, BitmapSizeOptions.FromEmptyOptions());
            result.Freeze();
            return result;
        }
        finally
        {
            SelectObject(target, previous);
            DeleteObject(bitmap);
            DeleteDC(target);
            ReleaseDC(0, screen);
        }
    }
}
