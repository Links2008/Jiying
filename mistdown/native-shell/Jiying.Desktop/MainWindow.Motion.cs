using System.Windows;
using System.Windows.Controls;
using System.Windows.Controls.Primitives;
using System.Windows.Media;
using System.Windows.Media.Animation;

namespace Jiying.Desktop;

public partial class MainWindow
{
    private readonly Dictionary<FrameworkElement, ExpansionTransition> _expansionTransitions = new();

    private ExpansionTransition ExpansionFor(FrameworkElement body)
    {
        if (!_expansionTransitions.TryGetValue(body, out var transition))
            _expansionTransitions.Add(body, transition = new ExpansionTransition(body));
        return transition;
    }
    private void UpdateParseLayout(bool compact)
    {
        var changed = ParseContent.VerticalAlignment != (compact ? VerticalAlignment.Top : VerticalAlignment.Center);
        var oldTop = IsLoaded ? ParseHero.TranslatePoint(new Point(), ParseView).Y : 0;
        ParseHeroMove.BeginAnimation(TranslateTransform.YProperty, null);
        ParseHeroMove.Y = 0;
        ParseContent.VerticalAlignment = compact ? VerticalAlignment.Top : VerticalAlignment.Center;
        ParseHeading.Visibility = ParseDescription.Visibility = ParseExamples.Visibility = compact ? Visibility.Collapsed : Visibility.Visible;
        ParseHeading.FontSize = compact ? 24 : Math.Clamp(RootGrid.ActualWidth * .034, 28, 40);
        ParseHeading.LineHeight = ParseHeading.FontSize * 1.15;
        ParseHeading.Margin = compact ? new Thickness(0, 0, 0, 14) : new Thickness(0);
        if (!IsLoaded || !changed || !SystemParameters.ClientAreaAnimation) return;
        if (!compact) ParseScroller.ScrollToTop();
        ParseView.UpdateLayout();
        ParseHeroMove.BeginAnimation(TranslateTransform.YProperty,
            new DoubleAnimation(oldTop - ParseHero.TranslatePoint(new Point(), ParseView).Y, 0, TimeSpan.FromMilliseconds(320))
            { EasingFunction = EaseOut, FillBehavior = FillBehavior.Stop });
    }

    private void SettingsFold_Changed(object sender, RoutedEventArgs e)
    {
        if (sender is not Expander group || (e.RoutedEvent != LoadedEvent && e.OriginalSource != sender)) return;
        group.ApplyTemplate();
        if (group.Template.FindName("ExpandSite", group) is not FrameworkElement body) return;
        var show = group.IsExpanded;
        if (!show && body.IsKeyboardFocusWithin && group.Template.FindName("HeaderSite", group) is ToggleButton header) header.Focus();
        var animate = e.RoutedEvent != LoadedEvent && IsLoaded && SystemParameters.ClientAreaAnimation;
        var width = VisualTreeHelper.GetParent(body) is FrameworkElement { ActualWidth: > 0 } parent
            ? parent.ActualWidth : group.ActualWidth;
        if (group == SpeechSettings || group == AdvancedSettings)
            ExpansionFor(body).Set(show, animate, width, show ? GroupOpenEase : CssEase, show ? 320 : 210,
                show ? GroupFadeInEase : EaseOut);
        else
            ExpansionFor(body).Set(show, animate, width, EaseOut, show ? 240 : 180);
    }

    private void SettingsArrow_Changed(object sender, RoutedEventArgs e)
    {
        if (sender is not ToggleButton header || e.OriginalSource != sender) return;
        header.ApplyTemplate();
        if (header.Template.FindName("Arrow", header) is not FrameworkElement { RenderTransform: RotateTransform rotate } arrow) return;
        if (rotate.IsFrozen) arrow.RenderTransform = rotate = rotate.Clone();
        var from = rotate.Angle;
        rotate.BeginAnimation(RotateTransform.AngleProperty, null);
        rotate.Angle = header.IsChecked == true ? 180 : 0;
        if (!IsLoaded || !SystemParameters.ClientAreaAnimation) return;
        rotate.BeginAnimation(RotateTransform.AngleProperty, new DoubleAnimation(from, rotate.Angle, TimeSpan.FromMilliseconds(180))
            { EasingFunction = EaseOut, FillBehavior = FillBehavior.Stop });
    }

    private static readonly IEasingFunction EaseOut = new CubicBezierEase(.23, 1, .32, 1);
    private static readonly IEasingFunction EaseInOut = new CubicBezierEase(.77, 0, .175, 1);
    private static readonly IEasingFunction CssEase = new CubicBezierEase(.25, .1, .25, 1);
    private static readonly IEasingFunction GroupOpenEase = new CubicBezierEase(.28, .08, .24, 1);
    private static readonly IEasingFunction GroupFadeInEase = new CubicBezierEase(.40, 0, .25, 1);
    private static readonly IEasingFunction NavEase = new CubicBezierEase(.25, .65, .45, 1);

    internal static void AnimateCard(FrameworkElement card)
    {
        ResetView(card);
        if (!SystemParameters.ClientAreaAnimation) return;
        var move = new TranslateTransform();
        card.RenderTransform = move;
        var duration = TimeSpan.FromMilliseconds(220);
        card.BeginAnimation(OpacityProperty,
            new DoubleAnimation(.52, 1, duration) { EasingFunction = EaseOut, FillBehavior = FillBehavior.Stop });
        move.BeginAnimation(TranslateTransform.YProperty,
            new DoubleAnimation(9, 0, duration) { EasingFunction = EaseOut, FillBehavior = FillBehavior.Stop });
    }
}

internal sealed class CubicBezierEase(double x1, double y1, double x2, double y2) : IEasingFunction
{
    public double Ease(double normalizedTime)
    {
        var u = normalizedTime;
        for (var i = 0; i < 6; i++)
        {
            var error = Bezier(u, x1, x2) - normalizedTime;
            var derivative = BezierDerivative(u, x1, x2);
            if (Math.Abs(error) < .0001 || Math.Abs(derivative) < .0001) break;
            u = Math.Clamp(u - error / derivative, 0, 1);
        }
        return Bezier(u, y1, y2);
    }

    private static double Bezier(double t, double p1, double p2)
    {
        var inverse = 1 - t;
        return 3 * inverse * inverse * t * p1 + 3 * inverse * t * t * p2 + t * t * t;
    }

    private static double BezierDerivative(double t, double p1, double p2)
    {
        var inverse = 1 - t;
        return 3 * inverse * inverse * p1 + 6 * inverse * t * (p2 - p1) + 3 * t * t * (1 - p2);
    }
}
