// 隔离窗口验证选中框随动、重复点击与紧凑布局，不连接真实引擎。
using System.IO;
using System.Reflection;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Media;
using System.Windows.Media.Imaging;
using Jiying.Desktop;

internal static class NavOutlineRegression
{
    internal static async Task Run(MainWindow window, Action<bool, string> check)
    {
        const BindingFlags flags = BindingFlags.Instance | BindingFlags.NonPublic;
        T Field<T>(string name) => (T)typeof(MainWindow).GetField(name, flags)!.GetValue(window)!;
        void Call(string name, params object[] args) => typeof(MainWindow).GetMethod(name, flags)!.Invoke(window, args);
        void Show(string name) => Call("ShowView", name);
        void Capture(string name)
        {
            var directory = Environment.GetEnvironmentVariable("JIYING_NAV_OUTLINE_OUTPUT");
            if (string.IsNullOrWhiteSpace(directory)) return;
            Directory.CreateDirectory(directory);
            var host = Field<Grid>("NavHost");
            var bitmap = new RenderTargetBitmap((int)Math.Ceiling(host.ActualWidth), (int)Math.Ceiling(host.ActualHeight), 96, 96, PixelFormats.Pbgra32);
            bitmap.Render(host);
            var encoder = new PngBitmapEncoder(); encoder.Frames.Add(BitmapFrame.Create(bitmap));
            using var file = File.Create(Path.Combine(directory, name + ".png")); encoder.Save(file);
        }
        window.Width = 1160; window.Height = 720; window.UpdateLayout();
        Show("parse"); await Task.Delay(400);
        var thumb = Field<Border>("NavThumb"); var move = Field<TranslateTransform>("NavThumbMove");
        var accent = (SolidColorBrush)window.Resources["AccentBrush"];
        check(thumb.BorderBrush is SolidColorBrush brush && brush.Color == accent.Color, "选中蓝框属于移动底板，共用同一坐标");
        var button = Field<Button>("SettingsNav"); button.ApplyTemplate(); button.Focus();
        var focus = (Border)button.Template.FindName("NavFocus", button);
        check(focus.BorderBrush is SolidColorBrush { Color.A: 0 }, "鼠标取得焦点不会产生提前跳到目标的第二个蓝框");
        check(button.FocusVisualStyle is { Setters.Count: > 0 } && button.Focusable && button.IsTabStop, "键盘焦点提示与Tab导航仍保留");
        Show("settings");
        check(Field<Grid>("SettingsView").Visibility == Visibility.Visible && Field<Grid>("SettingsView").IsHitTestVisible, "更强动效不延迟目标页操作");
        await Task.Delay(125);
        check(!SystemParameters.ClientAreaAnimation || move.Y is > 7.2 and < 129.6, "125毫秒仍有明显可见移动，而非开头就贴到终点");
        check(Math.Abs(thumb.TranslatePoint(new Point(), Field<Grid>("NavHost")).Y - move.Y) < 1, "蓝框实际绘制位置跟随平滑轨迹");
        Capture("竖向移动中");
        var before = move.Y; Show("settings");
        check(Math.Abs(move.Y - before) < 1, "重复点击当前目标不截断正在进行的移动");
        before = move.Y; Show("done");
        check(!SystemParameters.ClientAreaAnimation || Math.Abs(move.Y - before) < 1, "改点另一目标从当前坐标接续");
        await Task.Delay(400);
        check(Math.Abs(move.Y - 96) < .01, "最后点击的目标与蓝框最终一致"); Capture("竖向移动完成");
        window.Width = 860; window.Height = 560; window.UpdateLayout(); await Task.Delay(150);
        Show("parse"); await Task.Delay(400); Show("settings"); await Task.Delay(125);
        var target = 3 * thumb.Width;
        check(!SystemParameters.ClientAreaAnimation || move.X > target * .05 && move.X < target * .9, "窄窗口底栏同样平滑移动");
        before = move.X; Show("settings");
        check(Math.Abs(move.X - before) < 1, "底栏重复点击也不发生突跳");
        await Task.Delay(400);
        check(Math.Abs(move.X - target) < .01 && Math.Abs(move.Y) < .01, "底栏最终位置和宽度对齐");
        Capture("横向移动完成");
        Show("done"); await Task.Delay(80); window.Width = 1160; window.Height = 720; window.UpdateLayout(); await Task.Delay(150);
        check(Math.Abs(move.X) < .01 && Math.Abs(move.Y - 96) < .01, "移动中调整窗口尺寸，蓝框不会残留旧轴坐标");
        Show("settings"); Call("MoveNavThumb", false);
        check(Math.Abs(move.Y - 144) < .01 && !move.HasAnimatedProperties, "直接定位路径保持可用，减少动画时立即对齐");
    }
}
