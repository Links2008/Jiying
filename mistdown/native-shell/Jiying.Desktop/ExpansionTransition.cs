using System.Windows;
using System.Windows.Controls;
using System.Windows.Media.Animation;

namespace Jiying.Desktop;

internal sealed class ExpansionTransition(FrameworkElement body)
{
    private int _revision;
    private bool? _target;

    public void Set(bool show, bool animate, double width, IEasingFunction easing, int milliseconds,
        IEasingFunction? opacityEasing = null)
    {
        if (_target == show && animate) return;
        _target = show;
        var revision = ++_revision;
        var shown = body.Visibility == Visibility.Visible;
        // 连续输入可能发生在两次布局之间。动画属性代表当前帧，
        // ActualHeight 此时仍可能是刚才那次布局的高度。
        var from = shown ? (double.IsFinite(body.Height) ? body.Height : body.ActualHeight) : 0;
        var opacity = shown ? body.Opacity : 0;
        body.BeginAnimation(FrameworkElement.HeightProperty, null);
        body.BeginAnimation(UIElement.OpacityProperty, null);
        body.IsHitTestVisible = show;
        var border = body as Border;
        var content = border?.Child as ExpansionContent;
        void Finish()
        {
            body.Visibility = show ? Visibility.Visible : Visibility.Collapsed;
            body.Height = double.NaN;
            body.Opacity = 1;
            body.BeginAnimation(FrameworkElement.HeightProperty, null);
            body.BeginAnimation(UIElement.OpacityProperty, null);
            content?.Release();
        }
        if (!animate || !show && !shown)
        {
            Finish();
            return;
        }

        body.Visibility = Visibility.Visible;
        var to = 0d;
        if (content is not null && border is not null)
        {
            var padding = border.Padding;
            var thickness = border.BorderThickness;
            var innerWidth = Math.Max(1, width - body.Margin.Left - body.Margin.Right
                - padding.Left - padding.Right - thickness.Left - thickness.Right);
            var contentHeight = content.Freeze(innerWidth);
            if (show) to = contentHeight + padding.Top + padding.Bottom + thickness.Top + thickness.Bottom;
        }
        else if (show)
        {
            body.Height = double.NaN;
            body.Measure(new Size(Math.Max(1, width), double.PositiveInfinity));
            to = Math.Max(0, body.DesiredSize.Height - body.Margin.Top - body.Margin.Bottom);
        }
        if (Math.Abs(from - to) < .01)
        {
            Finish();
            return;
        }

        var duration = TimeSpan.FromMilliseconds(milliseconds);
        body.Height = to;
        body.Opacity = show ? 1 : 0;
        var height = new DoubleAnimation(from, to, duration) { EasingFunction = easing, FillBehavior = FillBehavior.Stop };
        height.Completed += (_, _) =>
        {
            if (revision == _revision) Finish();
        };
        body.BeginAnimation(FrameworkElement.HeightProperty, height, HandoffBehavior.SnapshotAndReplace);
        body.BeginAnimation(UIElement.OpacityProperty,
            new DoubleAnimation(opacity, show ? 1 : 0, duration) { EasingFunction = opacityEasing, FillBehavior = FillBehavior.Stop },
            HandoffBehavior.SnapshotAndReplace);
    }
}

// 外层高度保持推拉布局。内部在过渡期间始终收到同一个排列矩形。
internal sealed class ExpansionContent : Decorator
{
    private Size? _frozen;
    private UIElement? _frozenChild;

    internal double Freeze(double width)
    {
        var child = Child;
        if (_frozen is Size previous && previous.Width == width && ReferenceEquals(child, _frozenChild))
            return previous.Height;
        child?.Measure(new Size(width, double.PositiveInfinity));
        var height = child?.DesiredSize.Height ?? 0;
        _frozenChild = child;
        _frozen = new Size(width, height);
        InvalidateMeasure();
        return height;
    }

    internal void Release()
    {
        if (_frozen is null) return;
        _frozen = null;
        _frozenChild = null;
        InvalidateMeasure();
    }

    protected override Size MeasureOverride(Size constraint) =>
        _frozen ?? base.MeasureOverride(constraint);

    protected override Size ArrangeOverride(Size size)
    {
        if (_frozen is not Size frozen) return base.ArrangeOverride(size);
        Child?.Arrange(new Rect(frozen));
        return size;
    }
}
