using System.Diagnostics;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Input;
using System.Windows.Media;
using System.Windows.Media.Media3D;

namespace Jiying.Desktop;

public partial class MainWindow
{
    private ScrollViewer? _wheelScroller;
    private long _wheelStarted;
    private double _wheelFrom;
    private double _wheelTarget;
    private double _lastWheelOffset;

    private void PageScroll_PreviewMouseWheel(object sender, MouseWheelEventArgs e)
    {
        if (e.Delta == 0 || Keyboard.Modifiers != ModifierKeys.None) return;
        var viewer = FindWheelScroller(e.OriginalSource as DependencyObject);
        if (viewer is null || viewer.ScrollableHeight <= 0 ||
            (viewer != sender && (sender is not ListView || viewer.TemplatedParent != sender)))
            return;

        var lines = SystemParameters.WheelScrollLines;
        if (lines == 0)
        {
            e.Handled = true;
            StopWheelScroll();
            return;
        }
        if (!SystemParameters.ClientAreaAnimation) return;

        var offset = viewer.VerticalOffset;
        var distance = -(e.Delta / 120d) * (lines < 0 ? viewer.ViewportHeight : lines * 16d);
        // Extend same-direction input and start reversals from the position already on screen.
        var start = viewer == _wheelScroller && (_wheelTarget - offset) * distance > 0 ? _wheelTarget : offset;
        var target = Math.Clamp(start + distance, 0, viewer.ScrollableHeight);
        e.Handled = true;
        if (target == offset)
        {
            StopWheelScroll();
            return;
        }

        if (_wheelScroller != viewer)
        {
            StopWheelScroll();
            _wheelScroller = viewer;
            viewer.PreviewMouseDown += StopWheelScrollOnInput;
            viewer.PreviewKeyDown += StopWheelScrollOnInput;
            viewer.PreviewTouchDown += StopWheelScrollOnInput;
            viewer.Unloaded += StopWheelScrollOnInput;
            CompositionTarget.Rendering += RenderWheelScroll;
        }
        _wheelFrom = _lastWheelOffset = offset;
        _wheelTarget = target;
        _wheelStarted = Stopwatch.GetTimestamp();
    }

    private static ScrollViewer? FindWheelScroller(DependencyObject? element)
    {
        while (element is not null)
        {
            if (element is ScrollViewer viewer) return viewer;
            if (element is ComboBox or Slider) return null;
            element = element switch
            {
                Visual or Visual3D => VisualTreeHelper.GetParent(element),
                ContentElement content => ContentOperations.GetParent(content) ?? (content as FrameworkContentElement)?.Parent,
                _ => null
            };
        }
        return null;
    }

    private void RenderWheelScroll(object? sender, EventArgs e)
    {
        if (_wheelScroller is not { IsVisible: true } viewer || WindowState == WindowState.Minimized)
        {
            StopWheelScroll();
            return;
        }
        var progress = Math.Min(1, Stopwatch.GetElapsedTime(_wheelStarted).TotalMilliseconds / 140);
        var target = Math.Min(_wheelTarget, viewer.ScrollableHeight);
        var offset = _wheelFrom + (target - _wheelFrom) * EaseOut.Ease(progress);
        if (progress == 1 || Math.Abs(offset - _lastWheelOffset) >= .1)
        {
            viewer.ScrollToVerticalOffset(offset);
            _lastWheelOffset = offset;
        }
        if (progress == 1) StopWheelScroll();
    }

    private void StopWheelScrollOnInput(object? sender, RoutedEventArgs e) => StopWheelScroll();

    private void StopWheelScroll()
    {
        if (_wheelScroller is not { } viewer) return;
        _wheelScroller = null;
        CompositionTarget.Rendering -= RenderWheelScroll;
        viewer.PreviewMouseDown -= StopWheelScrollOnInput;
        viewer.PreviewKeyDown -= StopWheelScrollOnInput;
        viewer.PreviewTouchDown -= StopWheelScrollOnInput;
        viewer.Unloaded -= StopWheelScrollOnInput;
    }
}
