// 只操作隔离窗口：按键轨迹、结果中间帧与封面解码后的渐入。
using System.Reflection;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Media;
using System.Windows.Media.Imaging;
using Jiying.Desktop;

internal static class ResultMotionRegression
{
    internal static async Task Run(MainWindow window, Action<bool, string> check)
    {
        const BindingFlags flags = BindingFlags.Instance | BindingFlags.NonPublic;
        T Field<T>(string name) => (T)typeof(MainWindow).GetField(name, flags)!.GetValue(window)!;
        void Call(string name, params object[] args) => typeof(MainWindow).GetMethod(name, flags | BindingFlags.Static)!.Invoke(window, args);
        Call("ShowView", "parse");
        await Task.Delay(330);
        Call("ShowView", "settings");
        check(Field<Grid>("SettingsView").Visibility == Visibility.Visible && Field<Grid>("SettingsView").IsHitTestVisible,
              "恢复按键移动时页面仍立即可操作");
        await Task.Delay(125);
        var thumb = Field<TranslateTransform>("NavThumbMove");
        check(!SystemParameters.ClientAreaAnimation || (thumb.Y > 0 && thumb.Y < 143.99),
              "按键125毫秒时仍在平滑移动，不提前跳到目标");
        var previous = thumb.Y;
        Call("ShowView", "done");
        check(!SystemParameters.ClientAreaAnimation || Math.Abs(thumb.Y - previous) < 1,
              "连续点击从当前轨迹接续，按键不跳回起点");
        await Task.Delay(380);
        check(Math.Abs(thumb.Y - 96) < .01, "按键移动结束后停在最后目标");
        Call("ShowView", "parse");
        await Task.Delay(330);
        var panel = Field<FrameworkElement>("ProbePanel");
        panel.Visibility = Visibility.Visible;
        Call("AnimateCard", panel);
        await Task.Delay(150);
        check(!SystemParameters.ClientAreaAnimation || (panel.Opacity > 0 && panel.Opacity < .9 &&
              panel.RenderTransform is TranslateTransform { Y: > 0 }), "解析结果保留Linkshub式可见中间帧");
        await Task.Delay(550);
        check(Math.Abs(panel.Opacity - 1) < .01 && panel.IsHitTestVisible, "结果入场完成后完全清晰且可操作");
        var pixel = BitmapSource.Create(1, 1, 96, 96, PixelFormats.Bgra32, null, new byte[] { 100, 80, 60, 255 }, 4);
        pixel.Freeze();
        var image = Field<Image>("ProbeThumbnail");
        var reveal = typeof(MainWindow).GetMethod("RevealThumbnail", flags | BindingFlags.Static);
        check(reveal is not null, "晚到的封面有独立渐入而非突然贴入");
        if (reveal is null) return;
        reveal.Invoke(window, [image, pixel]);
        await Task.Delay(70);
        check(!SystemParameters.ClientAreaAnimation || (image.Opacity > 0 && image.Opacity < 1), "封面解码后平滑渐入");
        await Task.Delay(650);
        check(ReferenceEquals(image.Source, pixel) && Math.Abs(image.Opacity - 1) < .01, "封面最终显示解码后的图片");
    }
}
