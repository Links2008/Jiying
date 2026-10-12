using System.Windows;
using System.Windows.Controls;
using System.Windows.Controls.Primitives;
using System.Windows.Media;
using System.Windows.Media.Animation;

namespace Jiying.Desktop;

public partial class MainWindow
{
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
        var from = body.Visibility == Visibility.Visible ? body.ActualHeight : 0;
        body.BeginAnimation(MaxHeightProperty, null);
        body.BeginAnimation(OpacityProperty, null);
        body.MaxHeight = double.PositiveInfinity;
        body.Opacity = 1;
        if (e.RoutedEvent == LoadedEvent || !IsLoaded || !SystemParameters.ClientAreaAnimation)
        {
            body.Visibility = show ? Visibility.Visible : Visibility.Collapsed;
            return;
        }
        body.Visibility = Visibility.Visible;
        body.IsHitTestVisible = show;
        if (!show && body.IsKeyboardFocusWithin && group.Template.FindName("HeaderSite", group) is ToggleButton header) header.Focus();
        body.Measure(new Size(Math.Max(1, group.ActualWidth), double.PositiveInfinity));
        var to = show ? body.DesiredSize.Height : 0;
        var duration = TimeSpan.FromMilliseconds(show ? 240 : 180);
        var motion = new DoubleAnimation(from, to, duration) { EasingFunction = EaseOut, FillBehavior = FillBehavior.Stop };
        body.Tag = motion;
        motion.Completed += (_, _) =>
        {
            if (!ReferenceEquals(body.Tag, motion)) return;
            body.MaxHeight = double.PositiveInfinity;
            body.Opacity = 1;
            body.Visibility = group.IsExpanded ? Visibility.Visible : Visibility.Collapsed;
        };
        body.MaxHeight = from;
        body.Opacity = show && from == 0 ? 0 : 1;
        body.BeginAnimation(MaxHeightProperty, motion);
        body.BeginAnimation(OpacityProperty, new DoubleAnimation(body.Opacity, show ? 1 : 0, duration) { FillBehavior = FillBehavior.Stop });
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
}
