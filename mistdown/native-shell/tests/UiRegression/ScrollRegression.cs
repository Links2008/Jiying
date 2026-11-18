using System.Collections.ObjectModel;
using System.Diagnostics;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Controls.Primitives;
using System.Windows.Input;
using System.Windows.Media;
using Jiying.Desktop;

internal static class ScrollRegression
{
    internal static async Task Run(MainWindow window, Action<bool, string> check)
    {
        await CheckList(window, "done", window.DoneList, window.DoneEmpty, window.DoneTasks, check);
        await CheckList(window, "queue", window.QueueList, window.QueueEmpty, window.QueueTasks, check);
        var speech = window.SpeechSettings;
        var wasExpanded = speech.IsExpanded;
        try
        {
            window.ShowView("settings", animateDeparture: false);
            speech.IsExpanded = true;
            window.SettingsScroller.ScrollToTop();
            await Task.Delay(330);
            double Left(FrameworkElement control) => control.TranslatePoint(new Point(), window).X;
            var button = window.DownloadWhisperButton;
            var model = window.WhisperSettingsBox;
            var buttonEdge = button.TranslatePoint(new Point(button.ActualWidth, button.ActualHeight / 2), model);
            check(Math.Abs(buttonEdge.X + 12) <= 1 && Math.Abs(buttonEdge.Y - model.ActualHeight / 2) <= 1,
                "下载模型在选择框左侧，相隔 12 DIP 且垂直居中");
            check(Math.Abs(Left(window.WhisperSettingsBox) - Left(window.AsrProviderSettingsBox)) < 1 &&
                  Math.Abs(Left(window.WhisperSettingsBox) - Left(window.TranslationSettingsBox)) < 1,
                "识别模型与识别方式、字幕翻译的左边缘对齐");
            window.LlmCloudSettings.Visibility = Visibility.Visible;
            DependencyObject parent = window.LlmBaseSettingsBox;
            while (parent is not Expander) parent = LogicalTreeHelper.GetParent(parent);
            var details = (Expander)parent;
            details.IsExpanded = true;
            await Task.Delay(320);
            var header = (ToggleButton)details.Template.FindName("HeaderSite", details);
            var title = FindVisual<TextBlock>(header)!;
            var arrow = FindVisual<Viewbox>(header)!;
            var label = ((Grid)window.LlmKeySettingsBox.Parent).Children.OfType<TextBlock>().Single();
            var inset = title.TranslatePoint(new Point(), header).X;
            var rightInset = header.ActualWidth - arrow.TranslatePoint(new Point(arrow.ActualWidth, 0), header).X;
            check(inset >= 12 && rightInset >= 12 && Math.Abs(Left(title) - Left(label)) <= 1 &&
                  title.FontSize == label.FontSize, $"接口与模型标题和箭头留白且文字对齐（{inset:F1} / {rightInset:F1}）");
            CheckPageEdge(window, window.SettingsScroller, "设置", check);
            var comboArrow = FindVisual<System.Windows.Shapes.Path>(
                window.TranslationSettingsBox.Template.FindName("Toggle", window.TranslationSettingsBox) as ToggleButton)!;
            double CenterX(FrameworkElement element) =>
                element.TranslatePoint(new Point(element.ActualWidth / 2, element.ActualHeight / 2), window).X;
            foreach (var section in new[] { window.SpeechSettings, window.AdvancedSettings, window.GpuAdvancedSettings, details })
            {
                var toggle = (ToggleButton)section.Template.FindName("HeaderSite", section);
                var icon = (FrameworkElement)toggle.Template.FindName("Arrow", toggle);
                var delta = CenterX(icon) - CenterX(comboArrow);
                check(Math.Abs(delta) < 1.5, $"{section.Header} 折叠箭头与下拉箭头居中对齐，差值 {delta:F2}");
            }
            check(Math.Abs(Left(window.LlmBaseSettingsBox) - Left(window.LlmModelSettingsBox)) < 1 &&
                  Math.Abs(window.LlmBaseSettingsBox.ActualWidth - window.LlmModelSettingsBox.ActualWidth) < 1,
                "接口地址与模型输入框的两端对齐");
            if (SystemParameters.ClientAreaAnimation && SystemParameters.WheelScrollLines != 0)
                await CheckWheel(window.SettingsScroller, "settings", check);
            window.ShowView("parse", animateDeparture: false);
            var minHeight = window.ParseLayout.MinHeight;
            window.ParseLayout.MinHeight = window.RootGrid.ActualHeight + 400;
            window.UpdateLayout();
            await Task.Delay(350);
            CheckPageEdge(window, window.ParseScroller, "解析", check);
            window.ParseLayout.MinHeight = minHeight;
        }
        finally
        {
            speech.IsExpanded = wasExpanded;
            window.ShowView("parse", animateDeparture: false);
        }
    }

    private static async Task CheckList(MainWindow window, string view, ListView list, FrameworkElement empty,
        ObservableCollection<TaskItem> items, Action<bool, string> check)
    {
        var listVisibility = list.Visibility;
        var emptyVisibility = empty.Visibility;
        var tasks = items.ToArray();
        try
        {
            items.Clear();
            if (view == "done")
            {
                window.ShowView(view, animateDeparture: false);
                empty.Visibility = Visibility.Visible;
                window.UpdateLayout();
                var emptyScroller = FindScroller(list);
                check(empty.IsVisible && window.DoneCountText.IsVisible && emptyScroller?.ScrollableHeight == 0,
                    "空历史保留页头与居中提示，页面没有多余滚动范围");
            }
            for (var i = 0; i < 80; i++)
                items.Add(new TaskItem { Id = $"scroll-{i}", Title = $"Scroll fixture {i}",
                    Status = view == "done" ? "completed" : "queued", CreatedAt = "2026-11-17T12:00:00" });
            list.Visibility = Visibility.Visible;
            empty.Visibility = Visibility.Collapsed;
            window.ShowView(view, animateDeparture: false);
            await Task.Delay(300);
            window.UpdateLayout();

            var viewer = FindScroller(list) ?? throw new InvalidOperationException($"No ScrollViewer in {view}");
            CheckPageEdge(window, viewer, view, check);
            check(viewer.CanContentScroll && VirtualizingPanel.GetScrollUnit(list) == ScrollUnit.Pixel &&
                  VirtualizingPanel.GetVirtualizationMode(list) == VirtualizationMode.Recycling,
                $"{view} list uses virtualized pixel scrolling with recycled cards");
            check(list.ItemContainerGenerator.ContainerFromItem(items[0]) is not null &&
                  list.ItemContainerGenerator.ContainerFromItem(items[^1]) is null,
                $"{view} list realizes visible rows and skips distant rows");
            if (!SystemParameters.ClientAreaAnimation || SystemParameters.WheelScrollLines == 0) return;

            double HeaderY() => window.DoneCountText.TranslatePoint(new Point(), window.DoneList).Y;
            var headerY = view == "done" ? HeaderY() : 0;
            var notch = await CheckWheel(viewer, view, check);
            if (view == "done")
            {
                check(window.DoneCountText.IsVisible && Math.Abs(headerY - HeaderY() - notch) <= 1,
                    $"记录数与卡片按相同的像素偏移滚动（{headerY:F1} → {HeaderY():F1}，列表移动 {notch:F1}）");
                var start = viewer.VerticalOffset;
                Wheel(viewer, -60);
                await Task.Delay(270);
                var end = viewer.VerticalOffset;
                check(notch > 0 && Math.Abs(end - start - notch / 2) <= 1,
                    "Completed list follows fractional wheel deltas");
                Wheel(viewer, -120);
                var timeout = Stopwatch.StartNew();
                while (viewer.VerticalOffset <= end + .5 && timeout.ElapsedMilliseconds < 100)
                    await Task.Delay(5);
                var reversal = viewer.VerticalOffset;
                Wheel(viewer, 120);
                await Task.Delay(270);
                check(reversal > end + .5 && viewer.VerticalOffset < reversal - .5,
                    "Quick wheel reversal moves from the visible position in the new direction");
            }
        }
        finally
        {
            window.ShowView("parse", animateDeparture: false);
            items.Clear();
            foreach (var task in tasks) items.Add(task);
            list.Visibility = listVisibility;
            empty.Visibility = emptyVisibility;
        }
    }

    private static async Task<double> CheckWheel(ScrollViewer viewer, string view, Action<bool, string> check)
    {
        var frameOffsets = new List<double>();
        void Sample(object? sender, EventArgs e) => frameOffsets.Add(viewer.VerticalOffset);
        var start = viewer.VerticalOffset;
        CompositionTarget.Rendering += Sample;
        try
        {
            Wheel(viewer, -120);
            await Task.Delay(330);
            var end = viewer.VerticalOffset;
            var intermediate = frameOffsets.Distinct().Count(value => value > start + .5 && value < end - .5);
            check(end > start && intermediate >= 2,
                $"{view} wheel moves through intermediate offsets ({start:F1} -> {end:F1}, extent {viewer.ScrollableHeight:F1}, {intermediate} intermediate)");
            return end - start;
        }
        finally { CompositionTarget.Rendering -= Sample; }
    }

    private static void Wheel(ScrollViewer viewer, int delta)
    {
        var preview = new MouseWheelEventArgs(Mouse.PrimaryDevice, Environment.TickCount, delta)
            { RoutedEvent = Mouse.PreviewMouseWheelEvent };
        viewer.RaiseEvent(preview);
        if (!preview.Handled)
            viewer.RaiseEvent(new MouseWheelEventArgs(Mouse.PrimaryDevice, Environment.TickCount, delta)
                { RoutedEvent = Mouse.MouseWheelEvent });
    }

    private static ScrollViewer? FindScroller(DependencyObject parent)
        => FindVisual<ScrollViewer>(parent);

    private static void CheckPageEdge(MainWindow window, ScrollViewer viewer, string page, Action<bool, string> check)
    {
        var bar = (ScrollBar)viewer.Template.FindName("PART_VerticalScrollBar", viewer);
        var right = bar.TranslatePoint(new Point(bar.ActualWidth, 0), window.RootGrid).X;
        var gap = window.RootGrid.ActualWidth - right;
        check(bar.IsVisible && Math.Abs(gap) <= 1, $"{page} 纵向滚动条贴齐客户区右沿（间隙 {gap:F1} DIP）");
    }

    private static T? FindVisual<T>(DependencyObject parent) where T : DependencyObject
    {
        for (var index = 0; index < VisualTreeHelper.GetChildrenCount(parent); index++)
        {
            var child = VisualTreeHelper.GetChild(parent, index);
            if ((child as T ?? FindVisual<T>(child)) is { } match) return match;
        }
        return null;
    }
}
