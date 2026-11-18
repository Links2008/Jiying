using System.Text.RegularExpressions;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Input;
using System.Windows.Media;
using System.Windows.Media.Animation;

namespace Jiying.Desktop;

public partial class MainWindow
{
    private readonly HashSet<string> _selectedLanguages = ["zh"];
    private CancellationTokenSource? _previewStop;
    private bool _subtitleDragging;

    private void BuildLanguageChoices()
    {
        foreach (var item in new[]
        {
            ("zh", "中文"), ("en", "英语"), ("ja", "日语"), ("ko", "韩语"),
            ("es", "西语"), ("fr", "法语"), ("de", "德语"), ("ru", "俄语"),
            ("pt", "葡语"), ("th", "泰语"), ("vi", "越南语")
        })
        {
            var check = new CheckBox { Content = item.Item2, Tag = item.Item1, Style = (Style)Resources["LanguageChip"], IsChecked = item.Item1 == "zh" };
            check.Click += Language_Click;
            LanguagePanel.Children.Add(check);
        }
    }

    private void Language_Click(object sender, RoutedEventArgs e)
    {
        if (sender is not CheckBox { Tag: string language } check) return;
        if (check.IsChecked == true) _selectedLanguages.Add(language);
        else _selectedLanguages.Remove(language);
        if (_selectedLanguages.Count == 0)
        {
            check.IsChecked = true;
            _selectedLanguages.Add(language);
        }
        UpdateSubmitHint();
    }

    private void SetSubtitleSection(FrameworkElement section, bool show, bool animate)
    {
        var width = (section.Parent as FrameworkElement)?.ActualWidth ?? ParseLayout.ActualWidth;
        ExpansionFor(section).Set(show, animate && IsLoaded && SystemParameters.ClientAreaAnimation, width,
            EaseOut, show ? 220 : 170);
    }

    private void SubtitleChoice_Changed(object sender, SelectionChangedEventArgs e)
    {
        if (_loadingControls) return;
        var mode = Selected(SubtitleModeBox, "none");
        _ui.VisualNamesOnly = mode == "visual";
        WithControlLoading(() =>
        {
            RecognizeCheck.IsChecked = mode is "source" or "translate";
            TranslateCheck.IsChecked = mode == "translate" || _ui.VisualNamesOnly && _ui.TranslateVisualNames;
        });
        SubtitleMode_Changed(mode == "translate" ? TranslateCheck : RecognizeCheck, new RoutedEventArgs());
    }

    internal void SubtitleMode_Changed(object sender, RoutedEventArgs e) =>
        _ = RunWindowOperationAsync("更换字幕模式", () => ChangeSubtitleModeAsync(sender));

    private async Task ChangeSubtitleModeAsync(object sender)
    {
        if (_loadingControls) return;
        var enableTranslation = ReferenceEquals(sender, TranslateCheck) && TranslateCheck.IsChecked == true;
        if (enableTranslation && !_ui.VisualNamesOnly)
        {
            RecognizeCheck.IsChecked = true;
            WithControlLoading(() =>
            {
                Select(TranslationParseBox, "llm");
                Select(TranslationSettingsBox, "llm");
            });
        }
        if (ReferenceEquals(sender, RecognizeCheck) && RecognizeCheck.IsChecked != true && !_ui.VisualNamesOnly)
        {
            TranslateCheck.IsChecked = false;
            Select(SubtitleOutputBox, "softmux");
        }
        _ui.Recognize = RecognizeCheck.IsChecked == true;
        if (_ui.VisualNamesOnly) _ui.TranslateVisualNames = TranslateCheck.IsChecked == true;
        else _ui.Translate = TranslateCheck.IsChecked == true;
        _ui.Burn = Selected(SubtitleOutputBox, "softmux") == "burn";
        _ui.Save();
        SyncDependentControls(true);
        UpdateSubmitHint();
        QueueSubtitlePreview();
        if (enableTranslation) await SaveEngineConfigAsync(false);
    }

    private void BurnOption_Changed(object sender, RoutedEventArgs e)
    {
        if (_loadingControls) return;
        _ui.Burn = Selected(SubtitleOutputBox, "softmux") == "burn";
        _ui.Bilingual = BilingualCheck.IsChecked == true;
        _ui.Save();
        SyncDependentControls(true);
        UpdateSubmitHint();
        QueueSubtitlePreview();
    }

    private void PersonNotes_Changed(object sender, RoutedEventArgs e)
    {
        if (_loadingControls) return;
        _ui.PersonNotes = PersonNotesCheck.IsChecked == true;
        _ui.Save();
        SyncDependentControls(true);
        QueueSubtitlePreview();
    }

    private void BurnStyle_Changed(object sender, SelectionChangedEventArgs e)
    {
        if (_loadingControls) return;
        if (ReferenceEquals(sender, SubtitlePositionBox))
        {
            _ui.SubtitleHorizontalOffset = 0;
            WithControlLoading(() => VerticalOffsetSlider.Value = 0);
        }
        SyncDependentControls();
        SaveSubtitleStyle();
        QueueSubtitlePreview();
    }

    private void BurnColor_LostFocus(object sender, RoutedEventArgs e)
    {
        if (_loadingControls) return;
        SaveSubtitleStyle();
        QueueSubtitlePreview();
    }

    private void ResetSubtitlePosition_Click(object sender, RoutedEventArgs e)
    {
        WithControlLoading(() =>
        {
            Select(SubtitlePositionBox, "bottom");
            VerticalOffsetSlider.Value = 0;
        });
        _ui.SubtitleHorizontalOffset = 0;
        SubtitleDragMarker.Visibility = Visibility.Collapsed;
        SaveSubtitleStyle();
        QueueSubtitlePreview();
    }

    private bool HasSubtitlePreviewArea => SubtitlePreviewSurface.ActualWidth > 0 && SubtitlePreviewSurface.ActualHeight > 0;

    private void SubtitlePreview_MouseDown(object sender, MouseButtonEventArgs e)
    {
        if (!HasSubtitlePreviewArea || Selected(SubtitleOutputBox, "softmux") != "burn" ||
            !SubtitlePreviewSurface.CaptureMouse()) return;
        _subtitleDragging = true;
        MoveSubtitleMarker(e.GetPosition(SubtitlePreviewSurface));
        e.Handled = true;
    }

    private void SubtitlePreview_MouseMove(object sender, MouseEventArgs e)
    {
        if (_subtitleDragging) MoveSubtitleMarker(e.GetPosition(SubtitlePreviewSurface));
    }

    private void SubtitlePreview_MouseUp(object sender, MouseButtonEventArgs e)
    {
        if (!_subtitleDragging) return;
        var width = SubtitlePreviewSurface.ActualWidth;
        var height = SubtitlePreviewSurface.ActualHeight;
        var hasArea = width > 0 && height > 0;
        var point = hasArea ? e.GetPosition(SubtitlePreviewSurface) : default;
        _subtitleDragging = false;
        SubtitlePreviewSurface.ReleaseMouseCapture();
        e.Handled = true;
        if (!hasArea) return;
        var x = Math.Clamp((int)Math.Round(point.X / width * 640), 50, 590);
        var y = Math.Clamp((int)Math.Round(point.Y / height * 360), 18, 342);
        var position = y < 108 ? "top" : y < 252 ? "middle" : "bottom";
        var baseY = position == "top" ? 45 : position == "middle" ? 180 : 315;
        WithControlLoading(() =>
        {
            Select(SubtitlePositionBox, position);
            VerticalOffsetSlider.Value = y - baseY;
        });
        _ui.SubtitleHorizontalOffset = x - 320;
        SaveSubtitleStyle();
        QueueSubtitlePreview();
    }

    private void SubtitlePreview_LostCapture(object sender, MouseEventArgs e)
    {
        _subtitleDragging = false;
        SubtitleDragMarker.Visibility = Visibility.Collapsed;
    }

    private void MoveSubtitleMarker(Point point)
    {
        if (!HasSubtitlePreviewArea)
        {
            SubtitleDragMarker.Visibility = Visibility.Collapsed;
            return;
        }
        Canvas.SetLeft(SubtitleDragMarker, Math.Clamp(point.X - 7, 0, Math.Max(0, SubtitlePreviewSurface.ActualWidth - 14)));
        Canvas.SetTop(SubtitleDragMarker, Math.Clamp(point.Y - 7, 0, Math.Max(0, SubtitlePreviewSurface.ActualHeight - 14)));
        SubtitleDragMarker.Visibility = Visibility.Visible;
    }

    private void VerticalOffsetSlider_ValueChanged(object sender, RoutedPropertyChangedEventArgs<double> e)
    {
        if (VerticalOffsetValue is null) return;
        VerticalOffsetValue.Text = ((int)e.NewValue).ToString();
        if (_loadingControls) return;
        SaveSubtitleStyle();
        QueueSubtitlePreview();
    }

    private void OutlineSlider_ValueChanged(object sender, RoutedPropertyChangedEventArgs<double> e)
    {
        if (OutlineValue is null) return;
        OutlineValue.Text = ((int)e.NewValue).ToString();
        if (_loadingControls) return;
        SaveSubtitleStyle();
        QueueSubtitlePreview();
    }

    private void SaveSubtitleStyle()
    {
        _ui.FontName = Selected(FontNameBox);
        _ui.FontSize = IntValue(Selected(FontSizeBox), 18);
        _ui.SubtitlePosition = Selected(SubtitlePositionBox, "bottom");
        _ui.Outline = (int)OutlineSlider.Value;
        var color = SubtitleColorValue();
        if (Regex.IsMatch(color, "^#[0-9A-Fa-f]{6}$")) _ui.SubtitleColor = color.ToUpperInvariant();
        _ui.SubtitleVerticalOffset = (int)VerticalOffsetSlider.Value;
        _ui.Save();
    }

    private string SubtitleColorValue() => Selected(SubtitleColorBox, "#FFFFFF") == "custom"
        ? SubtitleCustomColorBox.Text.Trim() : Selected(SubtitleColorBox, "#FFFFFF");

    private void QueueSubtitlePreview()
    {
        _previewStop?.Cancel();
        var personNotes = _ui.VisualNamesOnly || RecognizeCheck.IsChecked == true && PersonNotesCheck.IsChecked == true;
        if (!_engine.IsConnected || SubtitlePreviewImage is null ||
            Selected(SubtitleOutputBox, "softmux") != "burn" && !personNotes) return;
        _previewStop?.Dispose();
        _previewStop = CancellationTokenSource.CreateLinkedTokenSource(_windowToken);
        var token = _previewStop.Token;
        var font = Selected(FontNameBox);
        var size = IntValue(Selected(FontSizeBox), 18);
        var position = Selected(SubtitlePositionBox, "bottom");
        var outline = (int)OutlineSlider.Value;
        var color = SubtitleColorValue();
        if (!Regex.IsMatch(color, "^#[0-9A-Fa-f]{6}$")) return;
        var verticalOffset = (int)VerticalOffsetSlider.Value;
        var horizontalOffset = _ui.SubtitleHorizontalOffset;
        var text = BilingualCheck.IsChecked == true ? "中文字幕示例\nEnglish subtitle sample" : "字幕预览 Subtitle Preview 123";
        _ = Task.Run(async () =>
        {
            try
            {
                await Task.Delay(180, token);
                var bitmap = BitmapFromBytes(await _engine.GetSubtitlePreviewAsync(font, size, position, outline, color,
                    verticalOffset, horizontalOffset, text, token, personNotes));
                await Dispatcher.InvokeAsync(() =>
                {
                    if (!token.IsCancellationRequested) SubtitlePreviewImage.Source = bitmap;
                });
            }
            catch (OperationCanceledException error) when (token.IsCancellationRequested && error.InnerException is not TimeoutException) { }
            catch (Exception error) { DesktopDiagnostics.Write("字幕样式预览", error); }
        }, token);
    }
}
