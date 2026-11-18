// 隔离窗口验证选中框随动、重复点击与紧凑布局，不连接真实引擎。
using System.IO;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Media;
using System.Windows.Media.Effects;
using System.Windows.Media.Imaging;
using Jiying.Desktop;

internal static class NavOutlineRegression
{
    internal static async Task Run(MainWindow window, Action<bool, string> check)
    {
        static void Click(Button button) => button.RaiseEvent(new RoutedEventArgs(Button.ClickEvent, button));
        var animate = SystemParameters.ClientAreaAnimation;
        void Capture(string name)
        {
            var directory = Environment.GetEnvironmentVariable("JIYING_NAV_OUTLINE_OUTPUT");
            if (string.IsNullOrWhiteSpace(directory)) return;
            Directory.CreateDirectory(directory);
            var host = window.NavHost;
            var bitmap = new RenderTargetBitmap((int)Math.Ceiling(host.ActualWidth), (int)Math.Ceiling(host.ActualHeight), 96, 96, PixelFormats.Pbgra32);
            bitmap.Render(host);
            var encoder = new PngBitmapEncoder(); encoder.Frames.Add(BitmapFrame.Create(bitmap));
            using var file = File.Create(Path.Combine(directory, name + ".png")); encoder.Save(file);
        }
        window.Width = 1160; window.Height = 720; window.UpdateLayout();
        Click(window.ParseNav); await Task.Delay(400);
        await CheckMaximizedParse(window, check);
        var thumb = window.NavThumb;
        var move = window.NavThumbMove;
        var accent = (SolidColorBrush)window.Resources["AccentBrush"];
        check(thumb.BorderBrush is SolidColorBrush brush && brush.Color == accent.Color, "选中蓝框属于移动底板，共用同一坐标");
        var button = window.SettingsNav; button.ApplyTemplate(); button.Focus();
        var focus = (Border)button.Template.FindName("NavFocus", button);
        check(focus.BorderBrush is SolidColorBrush { Color.A: 0 }, "鼠标取得焦点不会产生提前跳到目标的第二个蓝框");
        check(button.FocusVisualStyle is { Setters.Count: > 0 } && button.Focusable && button.IsTabStop, "键盘焦点提示与Tab导航仍保留");
        var start = move.Y;
        Click(window.SettingsNav);
        check(!animate || window.ParseView is { Visibility: Visibility.Visible, IsHitTestVisible: false } &&
              window.SettingsView.Visibility == Visibility.Collapsed, "目标页在旧页退场后入场");
        check(!animate || Math.Abs(move.Y - start) < 1, "侧栏起步停在原位，避免闪到终点");
        check(!animate || await UiTiming.Until(() => window.ParseView is
            { Visibility: Visibility.Visible, Effect: BlurEffect { Radius: > 0 and < 3 },
              Opacity: > 0 and < 1, RenderTransform: TranslateTransform { Y: < 0 and > -6 } } &&
            window.SettingsView.Visibility == Visibility.Collapsed), "旧页自然上移、模糊并淡出");
        check(!animate || await UiTiming.Until(() => move.Y is > 7.2 and < 129.6),
              "竖向侧栏自然播放经过中间位置");
        check(Math.Abs(thumb.TranslatePoint(new Point(), window.NavHost).Y - move.Y) < 1, "蓝框实际绘制位置跟随平滑轨迹");
        Capture("竖向移动中");
        var effect = window.ParseView.Effect;
        var before = move.Y; Click(window.SettingsNav);
        check(Math.Abs(move.Y - before) < 1, "重复点击当前目标不截断正在进行的移动");
        check(!animate || ReferenceEquals(window.ParseView.Effect, effect), "重复点击沿用同一段退场");
        before = move.Y; Click(window.DoneNav);
        check(!animate || ReferenceEquals(window.ParseView.Effect, effect), "改点新目标时旧页继续退场");
        check(!animate || Math.Abs(move.Y - before) < 1, "改点另一目标从当前坐标接续");
        check(!animate || await UiTiming.Until(() => window.DoneView is
            { Visibility: Visibility.Visible, IsHitTestVisible: true, Effect: BlurEffect { Radius: > 0 and < 3 },
              Opacity: > 0 and < 1, RenderTransform: TranslateTransform { Y: > 0 } } &&
            window.ParseView is { Visibility: Visibility.Collapsed, Effect: null, Opacity: 1 } &&
            window.SettingsView.Visibility == Visibility.Collapsed), "退场收尾后仅最终目标淡入并变清晰");
        check(await UiTiming.Until(() => Math.Abs(move.Y - 96) < .01 && !move.HasAnimatedProperties),
              "最后点击的目标与蓝框最终一致");
        check(await UiTiming.Until(() => window.DoneView is
            { Visibility: Visibility.Visible, Effect: null, Opacity: 1, IsHitTestVisible: true } &&
            ReferenceEquals(window.DoneView.RenderTransform, Transform.Identity)),
            "过渡结束后页面完全清晰且可操作");
        Capture("竖向移动完成");
        if (animate)
        {
            Click(window.SettingsNav);
            check(await UiTiming.Until(() => window.DoneView is
                { Visibility: Visibility.Visible, Effect: BlurEffect { Radius: > 0 }, Opacity: > 0 and < 1 }),
                "返回测试先观察到旧页退场");
            var opacity = window.DoneView.Opacity;
            Click(window.DoneNav);
            check(window.DoneView.Visibility == Visibility.Visible && window.DoneView.IsHitTestVisible &&
                  Math.Abs(window.DoneView.Opacity - opacity) < .01 && window.SettingsView.Visibility == Visibility.Collapsed,
                  "返回正在淡出的页时从当前画面恢复");
            check(await UiTiming.Until(() => window.DoneView is { Visibility: Visibility.Visible, Effect: null, Opacity: 1 }),
                  "返回原页后恢复清晰");
        }
        window.Width = 860; window.Height = 560; window.UpdateLayout(); await Task.Delay(150);
        Click(window.ParseNav); await Task.Delay(400);
        check(await UiTiming.Until(() => HasNoUnusedScroll(window)), "紧凑窗口的空白解析页没有多余滚动量");
        start = move.X;
        Click(window.SettingsNav);
        var target = 3 * thumb.Width;
        check(!animate || Math.Abs(move.X - start) < 1, "紧凑底栏起步停在原位");
        check(!animate || await UiTiming.Until(() => move.X > target * .05 && move.X < target * .9),
              "紧凑底栏自然播放经过中间位置");
        before = move.X; Click(window.SettingsNav);
        check(Math.Abs(move.X - before) < 1, "底栏重复点击也不发生突跳");
        check(await UiTiming.Until(() => Math.Abs(move.X - target) < .01 && Math.Abs(move.Y) < .01 && !move.HasAnimatedProperties),
              "底栏最终位置和宽度对齐");
        Capture("横向移动完成");
        Click(window.DoneNav);
        check(!animate || await UiTiming.Until(() => move.X < target - 1 && move.X > 2 * thumb.Width + 1),
              "调整窗口尺寸前底栏正在滑移");
        window.Width = 1160; window.Height = 720; window.UpdateLayout();
        check(await UiTiming.Until(() => Math.Abs(move.X) < .01 && Math.Abs(move.Y - 96) < .01),
              "移动中调整窗口尺寸，蓝框不会残留旧轴坐标");
        Click(window.SettingsNav); window.MoveNavThumb(false);
        check(Math.Abs(move.Y - 144) < .01 && !move.HasAnimatedProperties, "直接定位路径保持可用，减少动画时立即对齐");
        window.ShowView("parse", animateDeparture: false);
        check(window.ParseView.Visibility == Visibility.Visible && window.ParseView.IsHitTestVisible,
              "需要聚焦输入时可直接显示解析页");
    }

    private static bool HasNoUnusedScroll(MainWindow window) =>
        window.ParseScroller.ViewportHeight > 0 && window.ParseScroller.ScrollableHeight < .5 &&
        window.ParseScroller.ComputedVerticalScrollBarVisibility == Visibility.Collapsed;

    private static async Task CheckMaximizedParse(MainWindow window, Action<bool, string> check)
    {
        var state = window.WindowState;
        var result = window.ProbePanel.Visibility;
        var loading = window.ParseLoading.Visibility;
        var longContent = new Border();
        try
        {
            window.ProbePanel.Visibility = window.ParseLoading.Visibility = Visibility.Collapsed;
            window.WindowState = WindowState.Maximized;
            window.UpdateLayout();
            check(await UiTiming.Until(() => HasNoUnusedScroll(window)), "最大化空白解析页没有多余滚动量");
            longContent.Height = window.ParseScroller.ViewportHeight;
            window.ParseContent.Children.Add(longContent);
            window.UpdateLayout();
            check(await UiTiming.Until(() => window.ParseScroller.ScrollableHeight > 1 &&
                  window.ParseScroller.ComputedVerticalScrollBarVisibility == Visibility.Visible),
                  "最大化时真实超长内容仍可滚动");
        }
        finally
        {
            if (longContent.Parent == window.ParseContent) window.ParseContent.Children.Remove(longContent);
            window.ProbePanel.Visibility = result;
            window.ParseLoading.Visibility = loading;
            window.WindowState = state;
            window.UpdateLayout();
        }
    }
}
