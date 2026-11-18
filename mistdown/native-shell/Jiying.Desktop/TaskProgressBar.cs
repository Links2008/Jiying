using System.Windows;
using System.Windows.Controls;
using System.Windows.Media;
using System.Windows.Media.Animation;

namespace Jiying.Desktop;

public sealed class TaskProgressBar : ProgressBar
{
    private static readonly IEasingFunction ProgressEase = new CubicBezierEase(.2, .65, .36, 1);
    private ScaleTransform? _fill;
    private DoubleAnimation? _animation;
    private object? _paintedTask;

    public TaskProgressBar() => IsVisibleChanged += (_, _) => UpdateFill(false);

    public override void OnApplyTemplate()
    {
        base.OnApplyTemplate();
        _fill = null;
        if (GetTemplateChild("ProgressFill") is FrameworkElement indicator)
            indicator.RenderTransform = _fill = new ScaleTransform(0, 1);
        UpdateFill(false);
    }

    protected override void OnValueChanged(double oldValue, double newValue)
    {
        base.OnValueChanged(oldValue, newValue);
        UpdateFill(newValue > oldValue);
    }

    private void UpdateFill(bool animate)
    {
        if (_fill is null) return;
        var from = _fill.ScaleX;
        var to = Maximum > Minimum ? Math.Clamp((Value - Minimum) / (Maximum - Minimum), 0, 1) : 0;
        animate &= ReferenceEquals(_paintedTask, DataContext) && IsLoaded && IsVisible &&
                   SystemParameters.ClientAreaAnimation && to - from >= .002;
        _paintedTask = DataContext;
        _fill.BeginAnimation(ScaleTransform.ScaleXProperty, null);
        if (animate)
        {
            _fill.ScaleX = from;
            var fill = _fill;
            var animation = new DoubleAnimation(from, to, TimeSpan.FromMilliseconds(150)) { EasingFunction = ProgressEase };
            animation.Completed += (_, _) =>
            {
                if (!ReferenceEquals(_fill, fill) || !ReferenceEquals(_animation, animation)) return;
                fill.ScaleX = to;
                fill.BeginAnimation(ScaleTransform.ScaleXProperty, null);
                _animation = null;
            };
            _animation = animation;
            _fill.BeginAnimation(ScaleTransform.ScaleXProperty,
                animation);
        }
        else
        {
            _animation = null;
            _fill.ScaleX = to;
        }
    }
}
