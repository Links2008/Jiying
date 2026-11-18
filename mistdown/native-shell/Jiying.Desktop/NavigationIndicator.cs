using System.Windows;
using System.Windows.Media;
using System.Windows.Media.Animation;

namespace Jiying.Desktop;

internal sealed class NavigationIndicator(TranslateTransform transform)
{
    private (bool Compact, double Width)? _layout;
    private Point? _target;
    private int _revision;

    public void AlignIfTargetChanged(bool compact, double width, Action align)
    {
        var layout = (compact, Width: compact ? width : 0);
        if (_layout == layout) return;
        _layout = layout;
        align();
    }

    public void Move(Point target, bool animate, IEasingFunction easing)
    {
        if (animate && _target == target) return;
        var sequence = ++_revision;
        _target = target;
        var from = new Point(transform.X, transform.Y);
        Slide(TranslateTransform.XProperty, from.X, target.X);
        Slide(TranslateTransform.YProperty, from.Y, target.Y);

        void Slide(DependencyProperty property, double start, double end)
        {
            transform.ApplyAnimationClock(property, null);
            if (!animate || Math.Abs(start - end) < .01)
            {
                transform.SetValue(property, end);
                return;
            }
            // 首个自然时钟还未更新时，保留当前坐标。
            transform.SetValue(property, start);
            var animation = new DoubleAnimation(start, end, TimeSpan.FromMilliseconds(235))
                { EasingFunction = easing, FillBehavior = FillBehavior.HoldEnd };
            var clock = (AnimationClock)animation.CreateClock(true);
            clock.Completed += (_, _) =>
            {
                if (sequence != _revision) return;
                transform.SetValue(property, end);
                transform.ApplyAnimationClock(property, null);
            };
            transform.ApplyAnimationClock(property, clock, HandoffBehavior.SnapshotAndReplace);
        }
    }
}
