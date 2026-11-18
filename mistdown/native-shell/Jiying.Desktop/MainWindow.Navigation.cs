using System.IO;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Controls.Primitives;
using System.Windows.Input;
using System.Windows.Media;
using System.Windows.Media.Animation;
using System.Windows.Media.Effects;

namespace Jiying.Desktop;

public partial class MainWindow
{
    private string _currentView = "parse";
    private int _viewAnimationSequence;
    private FrameworkElement? _departingView;
    private NavigationIndicator? _navigationIndicator;
    internal NavigationIndicator NavigationMotion => _navigationIndicator ??= new(NavThumbMove);

    private void Window_PreviewKeyDown(object sender, KeyEventArgs e)
    {
        if (e.Key == Key.Escape && ModalOverlay.Visibility == Visibility.Visible)
        {
            CloseModal();
            e.Handled = true;
            return;
        }
        if (e.Key != Key.V || Keyboard.Modifiers != ModifierKeys.Control ||
            Keyboard.FocusedElement is TextBoxBase or PasswordBox) return;
        try
        {
            var value = Clipboard.GetText().Trim();
            var url = NormalizeUrl(value);
            if (!Uri.TryCreate(url, UriKind.Absolute, out var uri) || uri.Scheme is not ("http" or "https")) return;
            UrlBox.Text = url;
            ShowView("parse", animateDeparture: false);
            UrlBox.Focus();
            e.Handled = true;
        }
        catch { }
    }

    private void Nav_Click(object sender, RoutedEventArgs e)
    {
        if (sender is Button { Tag: string view }) ShowView(view);
    }

    internal void ShowView(string view, bool animateDeparture = true)
    {
        var views = new[] { ParseView, QueueView, DoneView, SettingsView };
        var current = views.FirstOrDefault(candidate => candidate.Visibility == Visibility.Visible) ?? ParseView;
        var next = ViewFor(view);
        var changed = ViewFor(_currentView) != next;
        if (changed) StopWheelScroll();
        var animationsEnabled = SystemParameters.ClientAreaAnimation;
        var animatePage = IsLoaded && animationsEnabled;
        _currentView = view;
        var labels = view switch
        {
            "queue" => ("传输队列", "实时查看每一步进度，可随时取消"),
            "done" => ("已完成", "查看和管理已完成的下载"),
            "settings" => ("设置", "下载、识别、翻译、网络与外观"),
            _ => ("解析", "粘贴链接，选画质，开始下载")
        };
        PageTitle.Text = labels.Item1;
        PageSubtitle.Text = labels.Item2;
        var navButtons = new[] { ParseNav, QueueNav, DoneNav, SettingsNav };
        var navIcons = new[] { ParseNavIcon, QueueNavIcon, DoneNavIcon, SettingsNavIcon };
        for (var i = 0; i < navButtons.Length; i++)
        {
            var active = (string?)navButtons[i].Tag == view;
            navButtons[i].Background = Brushes.Transparent;
            navButtons[i].Foreground = active ? Resources["TextBrush"] as Brush : Resources["MutedBrush"] as Brush;
            navIcons[i].Stroke = active ? Resources["AccentBrush"] as Brush : Resources["MutedBrush"] as Brush;
        }
        if (changed || !NavThumbMove.HasAnimatedProperties)
            MoveNavThumb(changed && animationsEnabled);
        if (animatePage && animateDeparture && current != next)
        {
            if (_departingView != current)
            {
                var sequence = ++_viewAnimationSequence;
                _departingView = current;
                AnimatePage(current, PageFrame(current), (0, -6, 3), 130, () =>
                {
                    if (sequence != _viewAnimationSequence) return;
                    PresentPage(ViewFor(_currentView),
                        IsLoaded && SystemParameters.ClientAreaAnimation ? (0, 12, 3) : null);
                });
            }
        }
        else if (changed || !animatePage || !animateDeparture)
        {
            (double Opacity, double Y, double Radius)? start = null;
            if (animatePage)
            {
                if (current != next) start = (0, 12, 3);
                else if (_departingView == next) start = PageFrame(next);
            }
            PresentPage(next, start);
        }
        if (changed && (view is "queue" or "done") && !HasLoadedTasks) QueueRefresh();
        if (changed && view == "settings")
        {
            if (File.Exists(UiSettings.NotificationStatusPath))
            {
                try
                {
                    using var status = System.Text.Json.JsonDocument.Parse(File.ReadAllText(UiSettings.NotificationStatusPath));
                    NotifyStatusText.Text = status.RootElement.GetProperty("message").GetString() ?? "软件内提示与 Windows 系统通知";
                }
                catch (Exception) { }
            }
            StartWhisperStatus(Selected(WhisperSettingsBox, "small"));
            StartGpuStatus();
        }

        void PresentPage(FrameworkElement page, (double Opacity, double Y, double Radius)? start)
        {
            var sequence = ++_viewAnimationSequence;
            _departingView = null;
            foreach (var candidate in views)
            {
                ResetView(candidate);
                candidate.Visibility = candidate == page ? Visibility.Visible : Visibility.Collapsed;
            }
            if (start is { } frame)
                AnimatePage(page, frame, (1, 0, 0), 240, () =>
                {
                    if (sequence == _viewAnimationSequence) ResetView(page);
                });
        }
    }

    private static (double Opacity, double Y, double Radius) PageFrame(FrameworkElement page) =>
        (page.Opacity, (page.RenderTransform as TranslateTransform)?.Y ?? 0, (page.Effect as BlurEffect)?.Radius ?? 0);

    private static void AnimatePage(FrameworkElement page, (double Opacity, double Y, double Radius) start,
        (double Opacity, double Y, double Radius) end, int milliseconds, Action completed)
    {
        page.BeginAnimation(OpacityProperty, null);
        var move = new TranslateTransform(0, start.Y);
        var blur = new BlurEffect { Radius = start.Radius };
        page.RenderTransform = move;
        page.Effect = blur;
        page.Opacity = start.Opacity;
        page.IsHitTestVisible = end.Opacity > 0;
        var duration = TimeSpan.FromMilliseconds(milliseconds);
        var fade = new DoubleAnimation(end.Opacity, duration) { EasingFunction = EaseOut };
        fade.Completed += (_, _) => completed();
        move.BeginAnimation(TranslateTransform.YProperty, new DoubleAnimation(end.Y, duration) { EasingFunction = EaseOut });
        blur.BeginAnimation(BlurEffect.RadiusProperty, new DoubleAnimation(end.Radius, duration) { EasingFunction = EaseOut });
        page.BeginAnimation(OpacityProperty, fade);
    }

    private FrameworkElement ViewFor(string view) => view switch
    {
        "queue" => QueueView,
        "done" => DoneView,
        "settings" => SettingsView,
        _ => ParseView
    };

    private static void ResetView(FrameworkElement view)
    {
        view.BeginAnimation(OpacityProperty, null);
        view.Opacity = 1;
        view.RenderTransform = Transform.Identity;
        view.Effect = null;
        view.IsHitTestVisible = true;
    }

    internal void MoveNavThumb(bool animate)
    {
        var index = _currentView switch { "queue" => 1, "done" => 2, "settings" => 3, _ => 0 };
        var x = _compactLayout ? index * NavThumb.Width : 0;
        var y = _compactLayout ? 0 : index * 48;
        NavigationMotion.Move(new Point(x,y), animate, NavEase);
    }

    private void RestoreNavigationAfterResize() =>
        NavigationMotion.AlignIfTargetChanged(_compactLayout, NavThumb.Width,
            () => MoveNavThumb(false));

    internal void ShowCompleted() => ShowView("done");
}
