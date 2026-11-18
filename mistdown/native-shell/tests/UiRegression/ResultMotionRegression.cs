// 只操作隔离窗口：按键轨迹、结果中间帧与封面解码后的渐入。
using System.Windows;
using System.Windows.Controls;
using System.Windows.Media;
using System.Windows.Media.Imaging;
using Jiying.Desktop;

internal static class ResultMotionRegression
{
    internal static async Task Run(MainWindow window, Action<bool, string> check)
    {
        window.ShowView("parse");
        await Task.Delay(330);
        window.ShowView("settings", animateDeparture: false);
        check(window.SettingsView.Visibility == Visibility.Visible && window.SettingsView.IsHitTestVisible,
              "恢复按键移动时页面仍立即可操作");
        await Task.Delay(80);
        var thumb = window.NavThumbMove;
        check(!SystemParameters.ClientAreaAnimation || (thumb.Y > 0 && thumb.Y < 143.99),
              "侧栏高亮保持短滑移，不提前跳到目标");
        var previous = thumb.Y;
        window.ShowView("done");
        check(!SystemParameters.ClientAreaAnimation || Math.Abs(thumb.Y - previous) < 1,
              "连续点击从当前轨迹接续，按键不跳回起点");
        await Task.Delay(380);
        check(Math.Abs(thumb.Y - 96) < .01, "按键移动结束后停在最后目标");
        window.ShowView("parse");
        await Task.Delay(330);
        var panel = window.ProbePanel;
        panel.Visibility = Visibility.Visible;
        MainWindow.AnimateCard(panel);
        check(panel.IsHitTestVisible, "解析结果一出现即可操作");
        await Task.Delay(60);
        check(!SystemParameters.ClientAreaAnimation || (panel.Opacity > 0 && panel.Opacity < 1 &&
              panel.RenderTransform is TranslateTransform { Y: > 0 }), "解析结果保留轻量中间帧");
        await Task.Delay(240);
        check(Math.Abs(panel.Opacity - 1) < .01 && panel.IsHitTestVisible, "结果入场完成后完全清晰且可操作");
        var pixel = BitmapSource.Create(1, 1, 96, 96, PixelFormats.Bgra32, null, new byte[] { 100, 80, 60, 255 }, 4);
        pixel.Freeze();
        var image = window.ProbeThumbnail;
        MainWindow.RevealThumbnail(image, pixel);
        await Task.Delay(70);
        check(!SystemParameters.ClientAreaAnimation || (image.Opacity > 0 && image.Opacity < 1), "封面解码后平滑渐入");
        await Task.Delay(220);
        check(ReferenceEquals(image.Source, pixel) && Math.Abs(image.Opacity - 1) < .01, "封面最终显示解码后的图片");
    }
}
