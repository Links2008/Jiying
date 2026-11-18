using System.Windows;
using System.Windows.Controls;
using System.Windows.Media;
using System.Windows.Media.Animation;

namespace Jiying.Desktop;

public partial class MainWindow
{
    private long _modalSequence;

    internal void OpenModal(FrameworkElement dialog)
    {
        if (!ReferenceEquals(dialog, SubtitleDialog) && SubtitleDialog.Visibility == Visibility.Visible &&
            (_subtitleBusy || !ConfirmDiscardSubtitleChanges())) return;
        if (!ReferenceEquals(dialog, SubtitleDialog)) CancelSubtitleLoad();
        _modalSequence++;
        SubtitleDialog.Visibility = DeleteDialog.Visibility = Visibility.Collapsed;
        dialog.Visibility = Visibility.Visible;
        ModalOverlay.Visibility = Visibility.Visible;
        ModalOverlay.BeginAnimation(OpacityProperty, null);
        dialog.BeginAnimation(OpacityProperty, null);
        ModalOverlay.Opacity = SystemParameters.ClientAreaAnimation ? 0 : 1;
        dialog.Opacity = SystemParameters.ClientAreaAnimation ? 0 : 1;
        dialog.RenderTransformOrigin = new Point(.5, .5);
        var transforms = new TransformGroup();
        transforms.Children.Add(new ScaleTransform(.97, .97));
        transforms.Children.Add(new TranslateTransform(0, 8));
        dialog.RenderTransform = transforms;
        if (!SystemParameters.ClientAreaAnimation) return;
        ModalOverlay.BeginAnimation(OpacityProperty, new DoubleAnimation(1, TimeSpan.FromMilliseconds(180)));
        dialog.BeginAnimation(OpacityProperty, new DoubleAnimation(1, TimeSpan.FromMilliseconds(300)) { EasingFunction = EaseOut });
        ((ScaleTransform)transforms.Children[0]).BeginAnimation(ScaleTransform.ScaleXProperty, new DoubleAnimation(1, TimeSpan.FromMilliseconds(300)) { EasingFunction = EaseOut });
        ((ScaleTransform)transforms.Children[0]).BeginAnimation(ScaleTransform.ScaleYProperty, new DoubleAnimation(1, TimeSpan.FromMilliseconds(300)) { EasingFunction = EaseOut });
        ((TranslateTransform)transforms.Children[1]).BeginAnimation(TranslateTransform.YProperty, new DoubleAnimation(0, TimeSpan.FromMilliseconds(300)) { EasingFunction = EaseOut });
    }

    private void CloseModal_Click(object sender, RoutedEventArgs e) => CloseModal();
    internal void CloseModal()
    {
        if (_subtitleBusy && SubtitleDialog.Visibility == Visibility.Visible) return;
        if (!ConfirmDiscardSubtitleChanges()) return;
        CancelSubtitleLoad();
        if (ModalOverlay.Visibility != Visibility.Visible) return;
        var modalSequence = ++_modalSequence;
        if (!SystemParameters.ClientAreaAnimation) { FinishCloseModal(); return; }
        var fade = new DoubleAnimation(0, TimeSpan.FromMilliseconds(150));
        fade.Completed += (_, _) => { if (modalSequence == _modalSequence) FinishCloseModal(); };
        ModalOverlay.BeginAnimation(OpacityProperty, fade);
    }

    private void FinishCloseModal()
    {
        ModalOverlay.Visibility = Visibility.Collapsed;
        ModalOverlay.BeginAnimation(OpacityProperty, null);
        ModalOverlay.Opacity = 1;
        SubtitleDialog.Visibility = DeleteDialog.Visibility = Visibility.Collapsed;
    }

    private void ShowError(string message)
    {
        if (_stop.IsCancellationRequested) return;
        if (!Dispatcher.CheckAccess()) { Dispatcher.Invoke(() => ShowError(message)); return; }
        ShowToast("操作失败", FriendlyError(message));
    }

    private static string FriendlyError(string message) =>
        message.Contains("sending the request", StringComparison.OrdinalIgnoreCase)
            ? "本地视频引擎连接刚刚中断，正在自动恢复，请稍后重试。"
            : message;

    private static void ClearError() { }

    private void NotifyCompleted(TaskItem task)
    {
        ShowToast("下载已完成", task.DisplayTitle);
    }

    internal void ShowToast(string title, string detail = "")
    {
        if (_stop.IsCancellationRequested) return;
        if (!Dispatcher.CheckAccess()) { Dispatcher.Invoke(() => ShowToast(title, detail)); return; }
        var iconPath = new System.Windows.Shapes.Path
        {
            Data = title.Contains("失败") || title.Contains("错误") || title.Contains("未完成")
                ? Geometry.Parse("M3,12 A9,9 0 1 0 21,12 A9,9 0 1 0 3,12 M12,9 L12.01,9 M11,12 L12,12 L12,16 L13,16")
                : (Geometry)FindResource("IconCheck"),
            Fill = Brushes.Transparent, StrokeThickness = 1.8,
            StrokeStartLineCap = PenLineCap.Round, StrokeEndLineCap = PenLineCap.Round, StrokeLineJoin = PenLineJoin.Round
        };
        iconPath.SetResourceReference(System.Windows.Shapes.Shape.StrokeProperty, "AccentBrush");
        var icon = new Border { Width = 30, Height = 30, CornerRadius = new CornerRadius(9), Child = new Viewbox { Width = 16, Height = 16, Child = iconPath } };
        icon.SetResourceReference(Border.BackgroundProperty, "AccentSoftBrush");
        var titleText = new TextBlock { Text = title, FontSize = 13.5, FontWeight = FontWeights.SemiBold };
        titleText.SetResourceReference(TextBlock.ForegroundProperty, "TextBrush");
        var detailText = new TextBlock { Text = detail, FontSize = 12, Margin = new Thickness(0, 2, 0, 0), TextWrapping = TextWrapping.Wrap };
        detailText.SetResourceReference(TextBlock.ForegroundProperty, "MutedBrush");
        detailText.Visibility = string.IsNullOrWhiteSpace(detail) ? Visibility.Collapsed : Visibility.Visible;
        var text = new StackPanel();
        text.Children.Add(titleText);
        text.Children.Add(detailText);
        var content = new Grid();
        content.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
        content.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
        content.Children.Add(icon);
        Grid.SetColumn(text, 1);
        text.Margin = new Thickness(11, 0, 0, 0);
        content.Children.Add(text);
        var scale = new ScaleTransform(SystemParameters.ClientAreaAnimation ? .98 : 1, SystemParameters.ClientAreaAnimation ? .98 : 1);
        var move = new TranslateTransform(0, SystemParameters.ClientAreaAnimation ? 14 : 0);
        var card = new Border { Width = 360, Padding = new Thickness(14, 13, 14, 13), CornerRadius = new CornerRadius(11), BorderThickness = new Thickness(1), Margin = new Thickness(0, ToastHost.Children.Count == 0 ? 0 : 10, 0, 0), RenderTransformOrigin = new Point(.5, 1), RenderTransform = new TransformGroup { Children = { scale, move } }, Child = content };
        card.SetResourceReference(Border.BackgroundProperty, "ToastBrush");
        card.SetResourceReference(Border.BorderBrushProperty, "BorderStrongBrush");
        ToastHost.Children.Add(card);
        card.Opacity = SystemParameters.ClientAreaAnimation ? 0 : 1;
        scale.ScaleX = scale.ScaleY = SystemParameters.ClientAreaAnimation ? .98 : 1;
        if (SystemParameters.ClientAreaAnimation)
        {
            var duration = TimeSpan.FromMilliseconds(240);
            card.BeginAnimation(OpacityProperty, new DoubleAnimation(1, duration) { EasingFunction = EaseOut });
            scale.BeginAnimation(ScaleTransform.ScaleXProperty, new DoubleAnimation(1, duration) { EasingFunction = EaseOut });
            scale.BeginAnimation(ScaleTransform.ScaleYProperty, new DoubleAnimation(1, duration) { EasingFunction = EaseOut });
            move.BeginAnimation(TranslateTransform.YProperty, new DoubleAnimation(0, duration) { EasingFunction = EaseOut });
        }
        _ = HideToastLaterAsync(card);
    }

    private async Task HideToastLaterAsync(Border card)
    {
        try
        {
            await Task.Delay(4200, _windowToken);
            await Dispatcher.InvokeAsync(() => HideToast(card));
        }
        catch (OperationCanceledException) { }
    }

    private void HideToast(Border card)
    {
        if (!ToastHost.Children.Contains(card)) return;
        if (!SystemParameters.ClientAreaAnimation) { ToastHost.Children.Remove(card); return; }
        var transforms = (TransformGroup)card.RenderTransform;
        var scale = (ScaleTransform)transforms.Children[0];
        var move = (TranslateTransform)transforms.Children[1];
        var fade = new DoubleAnimation(0, TimeSpan.FromMilliseconds(150));
        fade.Completed += (_, _) => ToastHost.Children.Remove(card);
        card.BeginAnimation(OpacityProperty, fade);
        scale.BeginAnimation(ScaleTransform.ScaleXProperty, new DoubleAnimation(.98, TimeSpan.FromMilliseconds(150)));
        scale.BeginAnimation(ScaleTransform.ScaleYProperty, new DoubleAnimation(.98, TimeSpan.FromMilliseconds(150)));
        move.BeginAnimation(TranslateTransform.YProperty, new DoubleAnimation(6, TimeSpan.FromMilliseconds(150)));
    }
}
