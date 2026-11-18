using System.IO;
using System.Runtime.InteropServices;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Controls.Primitives;
using System.Windows.Input;
using System.Windows.Interop;
using System.Windows.Media;
using System.Windows.Media.Animation;
using System.Windows.Media.Effects;
using System.Windows.Media.Imaging;
using Microsoft.Win32;

namespace Jiying.Desktop;

public partial class MainWindow
{
    [StructLayout(LayoutKind.Sequential)]
    private struct AccentPolicy
    {
        public int State;
        public int Flags;
        public int GradientColor;
        public int AnimationId;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct CompositionAttributeData
    {
        public int Attribute;
        public nint Data;
        public int SizeOfData;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct Margins
    {
        public int Left;
        public int Right;
        public int Top;
        public int Bottom;
    }

    [DllImport("user32.dll")]
    private static extern int SetWindowCompositionAttribute(nint hwnd, ref CompositionAttributeData data);

    [DllImport("dwmapi.dll")]
    private static extern int DwmExtendFrameIntoClientArea(nint hwnd, ref Margins margins);

    [DllImport("dwmapi.dll")]
    private static extern int DwmSetWindowAttribute(nint hwnd, int attribute, ref int value, int size);

    private bool _windowClosing;
    private bool _compactLayout;
    private BitmapSource? _darkArtwork;
    private BitmapSource? _lightArtwork;
    internal BitmapSource? DarkArtwork => _darkArtwork;
    internal BitmapSource? LightArtwork => _lightArtwork;

    private void ApplyScreenBlendArtwork()
    {
        if (BackgroundImage.Source is not BitmapSource source) return;
        var light = _ui.Theme == "light" || (_ui.Theme == "auto" && SystemUsesLightTheme());
        if (light) _lightArtwork = CreateBlendArtwork(source, 1.18, 1.06, true);
        else _darkArtwork = CreateBlendArtwork(source, 1.12, 1.04, false);
        BackgroundImage.Source = light ? _lightArtwork : _darkArtwork;
    }

    private static BitmapSource LoadBlendArtwork(bool light) => CreateBlendArtwork(
        new BitmapImage(new Uri("pack://application:,,,/Jiying.Native;component/Assets/etch-bg.jpg")),
        light ? 1.18 : 1.12, light ? 1.06 : 1.04, light);

    private static BitmapSource CreateBlendArtwork(BitmapSource source, double contrast, double brightness, bool darkInk)
    {
        var bitmap = new FormatConvertedBitmap(source, PixelFormats.Bgra32, null, 0);
        var stride = bitmap.PixelWidth * 4;
        var pixels = new byte[stride * bitmap.PixelHeight];
        bitmap.CopyPixels(pixels, stride, 0);
        for (var i = 0; i < pixels.Length; i += 4)
        {
            var luminance = (77 * pixels[i + 2] + 150 * pixels[i + 1] + 29 * pixels[i]) >> 8;
            var alpha = (byte)Math.Clamp((int)(((luminance - 128) * contrast + 128) * brightness), 0, 255);
            pixels[i] = pixels[i + 1] = pixels[i + 2] = darkInk ? (byte)0 : alpha;
            pixels[i + 3] = alpha;
        }
        var artwork = BitmapSource.Create(bitmap.PixelWidth, bitmap.PixelHeight, bitmap.DpiX, bitmap.DpiY,
            PixelFormats.Pbgra32, null, pixels, stride);
        artwork.Freeze();
        return artwork;
    }

    private void Window_SourceInitialized(object? sender, EventArgs e)
    {
        var hwnd = new WindowInteropHelper(this).Handle;
        if (HwndSource.FromHwnd(hwnd)?.CompositionTarget is { } target)
            target.BackgroundColor = Colors.Transparent;

        try
        {
            var margins = new Margins { Left = -1, Right = -1, Top = -1, Bottom = -1 };
            _ = DwmExtendFrameIntoClientArea(hwnd, ref margins);
            var rounded = 2;
            _ = DwmSetWindowAttribute(hwnd, 33, ref rounded, sizeof(int));
            ApplyNativeBackdrop();
        }
        catch (DllNotFoundException) { }
        catch (EntryPointNotFoundException) { }
    }

    private void LayoutBackground()
    {
        var width = Math.Max(1, RootGrid.ActualWidth);
        var height = Math.Max(1, RootGrid.ActualHeight);
        BackgroundImage.Height = height * 1.70;
        BackgroundImage.Width = Math.Max(width, height * 1.70 * 1.60);
    }

    private void ApplyNativeBackdrop()
    {
        var hwnd = new WindowInteropHelper(this).Handle;
        if (hwnd == nint.Zero) return;
        var light = _ui.Theme == "light" || (_ui.Theme == "auto" && SystemUsesLightTheme());
        var dark = light ? 0 : 1;
        _ = DwmSetWindowAttribute(hwnd, 20, ref dark, sizeof(int));
        var t = (Math.Clamp(_ui.BackgroundOpacity, 40, 100) - 40d) / 60d;
        var alpha = (byte)Math.Round((light ? 85 : 75) + (light ? 95 : 85) * t);
        var tint = ((uint)alpha << 24) | (light ? 0x00F8F6F4u : 0x0013100Du);
        var accent = new AccentPolicy { State = 4, Flags = 2, GradientColor = unchecked((int)tint) };
        var pointer = Marshal.AllocHGlobal(Marshal.SizeOf<AccentPolicy>());
        try
        {
            Marshal.StructureToPtr(accent, pointer, false);
            var data = new CompositionAttributeData { Attribute = 19, Data = pointer, SizeOfData = Marshal.SizeOf<AccentPolicy>() };
            _ = SetWindowCompositionAttribute(hwnd, ref data);
        }
        finally { Marshal.FreeHGlobal(pointer); }
    }

    private void Accent_Click(object sender, RoutedEventArgs e)
    {
        if (sender is not Button { Tag: string accent }) return;
        _ui.Accent = accent;
        _ui.Save();
        ApplyAppearance();
    }

    private void Theme_Changed(object sender, SelectionChangedEventArgs e)
    {
        if (_loadingControls) return;
        _ui.Theme = Selected(ThemeBox, "auto");
        _ui.Save();
        ApplyThemeWithTransition();
    }

    private void ThemeButton_Click(object sender, RoutedEventArgs e)
    {
        if (sender is not Button { Tag: string theme }) return;
        _ui.Theme = theme;
        _ui.Save();
        WithControlLoading(() => Select(ThemeBox, theme, "auto"));
        ApplyThemeWithTransition();
    }

    internal void ApplyThemeWithTransition()
    {
        void ClearSnapshot()
        {
            ThemeTransitionLayer.BeginAnimation(OpacityProperty, null);
            ThemeTransitionLayer.Visibility = Visibility.Collapsed;
            ThemeTransitionLayer.Source = null;
        }

        ClearSnapshot();
        if (!IsLoaded || !SystemParameters.ClientAreaAnimation || RootGrid.ActualWidth <= 0 || RootGrid.ActualHeight <= 0)
        {
            ApplyAppearance();
            return;
        }

        var dpi = VisualTreeHelper.GetDpi(RootGrid);
        var snapshot = new RenderTargetBitmap(
            Math.Max(1, (int)Math.Ceiling(RootGrid.ActualWidth * dpi.DpiScaleX)),
            Math.Max(1, (int)Math.Ceiling(RootGrid.ActualHeight * dpi.DpiScaleY)),
            dpi.PixelsPerInchX, dpi.PixelsPerInchY, PixelFormats.Pbgra32);
        snapshot.Render(RootGrid);
        snapshot.Freeze();
        ThemeTransitionLayer.Source = snapshot;
        ThemeTransitionLayer.Opacity = 1;
        ThemeTransitionLayer.Visibility = Visibility.Visible;
        ApplyAppearance();
        var fade = new DoubleAnimation(1, 0, TimeSpan.FromMilliseconds(240))
            { EasingFunction = EaseOut, FillBehavior = FillBehavior.Stop };
        ThemeTransitionLayer.Opacity = 0;
        fade.Completed += (_, _) =>
        {
            if (ReferenceEquals(ThemeTransitionLayer.Source, snapshot)) ClearSnapshot();
        };
        ThemeTransitionLayer.BeginAnimation(OpacityProperty, fade);
    }

    private void BackgroundOpacity_ValueChanged(object sender, RoutedPropertyChangedEventArgs<double> e)
    {
        if (BackgroundOpacityText is null) return;
        BackgroundOpacityText.Text = $"{(int)e.NewValue}%";
        if (_loadingControls) return;
        _ui.BackgroundOpacity = (int)e.NewValue;
        _ui.Save();
        ApplyAppearance();
    }

    internal void ApplyUiSettings()
    {
        WithControlLoading(() =>
        {
            NotifyCheck.IsChecked = _ui.Notify;
            AutoParseCheck.IsChecked = _ui.AutoParse;
            Select(SourceLanguageBox, _ui.SourceLanguage);
            RecognizeCheck.IsChecked = _ui.Recognize;
            TranslateCheck.IsChecked = _ui.VisualNamesOnly ? _ui.TranslateVisualNames : _ui.Recognize && _ui.Translate;
            Select(SubtitleOutputBox, _ui.Burn ? "burn" : "softmux");
            BilingualCheck.IsChecked = _ui.Bilingual;
            PersonNotesCheck.IsChecked = _ui.PersonNotes;
            Select(FontNameBox, _ui.FontName);
            Select(FontSizeBox, _ui.FontSize.ToString(), "18");
            Select(SubtitlePositionBox, _ui.SubtitlePosition, "bottom");
            Select(SubtitleColorBox, _ui.SubtitleColor, "custom");
            SubtitleCustomColorBox.Text = _ui.SubtitleColor;
            VerticalOffsetSlider.Value = Math.Clamp(_ui.SubtitleVerticalOffset, -120, 120);
            OutlineSlider.Value = Math.Clamp(_ui.Outline, 0, 8);
            Select(ThemeBox, _ui.Theme, "auto");
            BackgroundOpacitySlider.Value = Math.Clamp(_ui.BackgroundOpacity, 40, 100);
            Select(WhisperParseBox, "small");
            Select(WhisperSettingsBox, "small");
            Select(TranslationParseBox, "null");
            Select(TranslationSettingsBox, "null");
            Select(LlmVendorParseBox, "deepseek");
            Select(LlmVendorSettingsBox, "deepseek");
            Select(AsrProviderParseBox, "faster-whisper");
            Select(AsrProviderSettingsBox, "faster-whisper");
            Select(AsrVendorParseBox, "siliconflow");
            Select(AsrVendorSettingsBox, "siliconflow");
            Select(ProxyModeBox, "system");
            ConcurrencySlider.Value = 2;
        });
        ApplyLlmPreset("deepseek");
        ApplyAsrPreset("siliconflow");
        UpdateOutputPathUi();
        SyncDependentControls();
        ApplyAppearance();
        UpdateSubmitHint();
    }

    internal void ApplyAppearance()
    {
        var accent = _ui.Accent switch
        {
            "jade" => ("#68D5B5", "#2468D5B5"),
            "amber" => ("#E4A451", "#24E4A451"),
            "iris" => ("#8CA8DC", "#248CA8DC"),
            _ => ("#3050F0", "#243050F0")
        };
        SetBrush("AccentBrush", accent.Item1);
        SetBrush("AccentSoftBrush", accent.Item2);
        SetBrush("PrimaryInkBrush", _ui.Accent is "jade" or "amber" or "iris" ? "#142152" : "#FFFFFF");
        var accentColor = ((SolidColorBrush)Resources["AccentBrush"]).Color;
        Resources["SwitchOnBrush"] = AlphaBrush(.32, accentColor.R, accentColor.G, accentColor.B);
        Resources["PrimaryGradient"] = new LinearGradientBrush(
            Color.FromRgb((byte)(accentColor.R * .88 + 255 * .12), (byte)(accentColor.G * .88 + 255 * .12), (byte)(accentColor.B * .88 + 255 * .12)),
            accentColor, 90);

        var light = _ui.Theme == "light" || (_ui.Theme == "auto" && SystemUsesLightTheme());
        ApplyNativeBackdrop();
        var t = (Math.Clamp(_ui.BackgroundOpacity, 40, 100) - 40d) / 60d;
        var strength = Math.Pow(t, 1.7);
        var mist = .15 + .85 * strength;
        Opacity = 1;
        MistLayer.Opacity = mist;
        Background = Brushes.Transparent;
        if (light)
        {
            Resources["WindowBrush"] = AlphaBrush(.12 * (.3 + .7 * strength), 252, 253, 254);
            SetBrush("RailBrush", "#091A202A");
            SetBrush("PanelBrush", "#0C1A202A");
            SetBrush("CardBrush", "#0C1A202A");
            SetBrush("InputBrush", "#18FFFFFF");
            SetBrush("DialogBrush", "#FFF7F8FA");
            SetBrush("TextBrush", "#303844");
            SetBrush("MutedBrush", "#687383");
            SetBrush("SecondaryTextBrush", "#586473");
            SetBrush("BorderBrush", "#171A202A");
            SetBrush("BorderStrongBrush", "#2B1A202A");
            SetBrush("ToastBrush", "#E0FAFBFD");
            BackgroundVeil.Fill = VerticalGradient("#F6F7F9", "#EEF0F3", "#E8EAEE");
            BackgroundVeil.Opacity = .18 + .42 * strength;
            BackgroundImage.Source = _lightArtwork ??= LoadBlendArtwork(true);
            BackgroundImage.Opacity = .15 * (.25 + .75 * strength) * .5;
            VignetteLayer.Opacity = 0;
        }
        else
        {
            Resources["WindowBrush"] = AlphaBrush(.10 * (.3 + .7 * strength), 13, 17, 24);
            SetBrush("RailBrush", "#09FFFFFF");
            SetBrush("PanelBrush", "#0CFFFFFF");
            SetBrush("CardBrush", "#0CFFFFFF");
            SetBrush("InputBrush", "#73090C12");
            SetBrush("DialogBrush", "#FF20252E");
            SetBrush("TextBrush", "#EAEFF4");
            SetBrush("MutedBrush", "#818A94");
            SetBrush("SecondaryTextBrush", "#ABB2BA");
            SetBrush("BorderBrush", "#17FFFFFF");
            SetBrush("BorderStrongBrush", "#2BFFFFFF");
            SetBrush("ToastBrush", "#C711161E");
            BackgroundVeil.Fill = VerticalGradient("#2C3033", "#292D30", "#25292C");
            BackgroundVeil.Opacity = .18 + .46 * strength;
            BackgroundImage.Source = _darkArtwork ??= LoadBlendArtwork(false);
            BackgroundImage.Opacity = .30 * (.25 + .75 * strength);
            VignetteLayer.Opacity = 1;
        }
        ModalOverlay.Resources["CardBrush"] = Resources["DialogBrush"];
        GrainLayer.Opacity = .045;

        foreach (var button in new[] { ThemeDarkButton, ThemeLightButton, ThemeAutoButton })
        {
            var selected = Equals(button.Tag, _ui.Theme);
            button.Background = selected ? (Brush)Resources["AccentSoftBrush"] : Brushes.Transparent;
            button.Foreground = selected ? (Brush)Resources["TextBrush"] : (Brush)Resources["MutedBrush"];
        }
        foreach (var button in new[] { AccentIconButton, AccentJadeButton, AccentAmberButton, AccentIrisButton })
        {
            button.BorderThickness = new Thickness(2);
            button.BorderBrush = Equals(button.Tag, _ui.Accent)
                ? (Brush)Resources["TextBrush"]
                : Brushes.Transparent;
        }
        ShowView(_currentView);
    }

    private static SolidColorBrush AlphaBrush(double opacity, byte red, byte green, byte blue) =>
        new(Color.FromArgb((byte)Math.Round(255 * opacity), red, green, blue));

    private static LinearGradientBrush VerticalGradient(string top, string middle, string bottom) => new(
        new GradientStopCollection { new(Brush(top).Color, 0), new(Brush(middle).Color, .52), new(Brush(bottom).Color, 1) },
        new Point(0, 0), new Point(0, 1));

    private void SetBrush(string key, string color) => Resources[key] = Brush(color);
    private static SolidColorBrush Brush(string value) => new((Color)ColorConverter.ConvertFromString(value));
    private static bool SystemUsesLightTheme() => Convert.ToInt32(Registry.GetValue(
        @"HKEY_CURRENT_USER\Software\Microsoft\Windows\CurrentVersion\Themes\Personalize", "AppsUseLightTheme", 0)) != 0;

    private void WindowControls_MouseEnter(object sender, MouseEventArgs e) => SetWindowGlyphs(.84);
    private void WindowControls_MouseLeave(object sender, MouseEventArgs e) => SetWindowGlyphs(0);

    private void SetWindowGlyphs(double opacity)
    {
        var duration = TimeSpan.FromMilliseconds(120);
        foreach (var glyph in new[] { WMinGlyph, WMaxGlyph, WCloseGlyph })
            glyph.BeginAnimation(OpacityProperty, new DoubleAnimation(opacity, duration));
    }

    private void WindowMin_Click(object sender, RoutedEventArgs e) => SystemCommands.MinimizeWindow(this);

    private void WindowMax_Click(object sender, RoutedEventArgs e)
    {
        if (WindowState == WindowState.Maximized) SystemCommands.RestoreWindow(this);
        else SystemCommands.MaximizeWindow(this);
    }

    private void WindowClose_Click(object sender, RoutedEventArgs e) =>
        _ = RunWindowOperationAsync("关闭窗口", CloseWindowAsync);

    private async Task CloseWindowAsync()
    {
        if (_windowClosing) return;
        if (!ConfirmDiscardSubtitleChanges()) return;
        _windowClosing = true;
        if (!SystemParameters.ClientAreaAnimation) { Close(); return; }
        RootGrid.RenderTransformOrigin = new Point(.5, .5);
        RootGrid.RenderTransform = new ScaleTransform(1, 1);
        var duration = TimeSpan.FromMilliseconds(280);
        RootGrid.BeginAnimation(OpacityProperty, new DoubleAnimation(0, duration) { EasingFunction = CssEase });
        ((ScaleTransform)RootGrid.RenderTransform).BeginAnimation(ScaleTransform.ScaleXProperty, new DoubleAnimation(.985, duration) { EasingFunction = CssEase });
        ((ScaleTransform)RootGrid.RenderTransform).BeginAnimation(ScaleTransform.ScaleYProperty, new DoubleAnimation(.985, duration) { EasingFunction = CssEase });
        await Task.Delay(duration);
        if (!_windowToken.IsCancellationRequested) Close();
    }

    private void Window_StateChanged(object? sender, EventArgs e)
    {
        if (WMaxGlyph is not null)
            WMaxGlyph.Text = WindowState == WindowState.Maximized ? "↙" : "↗";
        UpdateWindowClip();
    }

    private void Window_SizeChanged(object sender, SizeChangedEventArgs e)
    {
        if (!IsLoaded) return;
        UpdateWindowClip();
        UpdateResponsiveLayout();
        LayoutBackground();
    }

    internal void UpdateWindowClip()
    {
        UpdatePageRightEdge();
        if (RootGrid is null || RootGrid.ActualWidth <= 0 || RootGrid.ActualHeight <= 0) return;
        RootGrid.Clip = WindowState == WindowState.Maximized
            ? null
            : new RectangleGeometry(new Rect(0, 0, RootGrid.ActualWidth, RootGrid.ActualHeight), 12, 12);
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct MonitorRect { public int Left, Top, Right, Bottom; }

    [StructLayout(LayoutKind.Sequential)]
    private struct MonitorInfo
    {
        public int Size;
        public MonitorRect Bounds, WorkArea;
        public uint Flags;
    }

    [DllImport("user32.dll")]
    private static extern nint MonitorFromWindow(nint window, uint flags);

    [DllImport("user32.dll", ExactSpelling = true)]
    private static extern bool GetMonitorInfoW(nint monitor, ref MonitorInfo info);

    protected override void OnLocationChanged(EventArgs e)
    {
        base.OnLocationChanged(e);
        if (IsLoaded && WindowState == WindowState.Maximized) UpdatePageRightEdge();
    }

    private void UpdatePageRightEdge()
    {
        if (PageHost is null) return;
        double inset = 0;
        if (WindowState == WindowState.Maximized && PresentationSource.FromVisual(RootGrid) is HwndSource source)
        {
            // 自绘窗口最大化时，系统的缩放边框可能越过当前显示器工作区。
            var info = new MonitorInfo { Size = Marshal.SizeOf<MonitorInfo>() };
            if (GetMonitorInfoW(MonitorFromWindow(source.Handle, 2), ref info))
            {
                var edge = RootGrid.PointFromScreen(new Point(info.WorkArea.Right, info.WorkArea.Top));
                inset = Math.Clamp(RootGrid.ActualWidth - edge.X, 0, RootGrid.ActualWidth);
            }
        }
        PageHost.Margin = new Thickness(0, 0, inset, 0);
    }

    private void UpdateResponsiveLayout()
    {
        var width = RootGrid.ActualWidth;
        var compact = width <= 900;
        ParseHeading.FontSize = ProbePanel.Visibility == Visibility.Visible || ParseLoading.Visibility == Visibility.Visible
            ? 24 : Math.Clamp(width * .034, 28, 40);
        ParseHeading.LineHeight = ParseHeading.FontSize * 1.15;
        _compactLayout = compact;
        RailColumn.Width = new GridLength(compact ? 0 : 224);
        CompactNavRow.Height = new GridLength(compact ? 70 : 0);
        Grid.SetRow(Rail, compact ? 1 : 0);
        Grid.SetColumn(Rail, 0);
        Grid.SetColumnSpan(Rail, compact ? 2 : 1);
        Rail.BorderThickness = compact ? new Thickness(0, 1, 0, 0) : new Thickness(0);
        Rail.BorderBrush = Resources["BorderBrush"] as Brush;
        RailLayout.Margin = compact ? new Thickness(12, 8, 12, 8) : new Thickness(14, 20, 14, 16);
        RailLayout.RowDefinitions[0].Height = compact ? new GridLength(0) : GridLength.Auto;
        RailLayout.RowDefinitions[1].Height = compact ? new GridLength(1, GridUnitType.Star) : GridLength.Auto;
        RailLayout.RowDefinitions[2].Height = compact ? new GridLength(0) : new GridLength(1, GridUnitType.Star);
        RailLayout.RowDefinitions[3].Height = compact ? new GridLength(0) : GridLength.Auto;
        LogoPanel.Visibility = DiskPanel.Visibility = compact ? Visibility.Collapsed : Visibility.Visible;
        NavPanel.Orientation = compact ? Orientation.Horizontal : Orientation.Vertical;
        var buttons = new[] { ParseNav, QueueNav, DoneNav, SettingsNav };
        var compactWidth = Math.Max(1, (width - 24) / buttons.Length);
        foreach (var button in buttons)
        {
            button.Width = compact ? compactWidth : double.NaN;
            button.Height = compact ? 52 : 42;
            button.Margin = compact ? new Thickness(0) : new Thickness(0, 0, 0, 6);
            button.HorizontalContentAlignment = compact ? HorizontalAlignment.Center : HorizontalAlignment.Left;
            button.FontSize = compact ? 11 : 14;
            if (button.Content is DockPanel content && content.Children[0] is Viewbox icon)
            {
                // The original narrow layout puts the icon above its label.
                content.LastChildFill = false;
                DockPanel.SetDock(icon, compact ? Dock.Top : Dock.Left);
                icon.Margin = compact ? new Thickness(0, 0, 0, 3) : new Thickness(0, 0, 11, 0);
                icon.HorizontalAlignment = HorizontalAlignment.Center;
                foreach (var label in content.Children.OfType<TextBlock>())
                    label.HorizontalAlignment = compact ? HorizontalAlignment.Center : HorizontalAlignment.Left;
            }
        }
        NavThumb.Width = compact ? compactWidth : double.NaN;
        NavThumb.HorizontalAlignment = compact ? HorizontalAlignment.Left : HorizontalAlignment.Stretch;
        NavThumb.Height = compact ? 52 : 42;
        var padding = compact ? new Thickness(18, 54, 18, 30) : new Thickness(40, 60, 40, 56);
        QueueView.Margin = new Thickness(0, padding.Top, 0, padding.Bottom);
        QueueHeader.Margin = new Thickness(padding.Left, 0, padding.Right, 15);
        QueueList.Padding = DoneList.Padding = new Thickness(padding.Left, 0, padding.Right, 0);
        DoneHeader.Margin = new Thickness(0, padding.Top, 0, 15);
        DoneFooter.Height = padding.Bottom;
        ParseLayout.Margin = SettingsContent.Margin = padding;
        var viewportHeight = RootGrid.ActualHeight - CompactNavRow.Height.Value;
        ParseLayout.MinHeight = Math.Max(0, viewportHeight - padding.Top - padding.Bottom);
        var contentWidth = ParseLayout.ActualWidth > 1 ? ParseLayout.ActualWidth : Math.Max(1, width - (compact ? 36 : 80));
        var previewWidth = Math.Max(1, contentWidth - 36);
        SkeletonThumbColumn.Width = compact ? new GridLength(1, GridUnitType.Star) : new GridLength(264);
        SkeletonGapColumn.Width = compact ? new GridLength(0) : new GridLength(20);
        SkeletonLayout.ColumnDefinitions[2].Width = compact ? new GridLength(0) : new GridLength(1, GridUnitType.Star);
        Grid.SetColumn(SkeletonLines, compact ? 0 : 2);
        Grid.SetRow(SkeletonLines, compact ? 1 : 0);
        SkeletonLines.Margin = compact ? new Thickness(0, 20, 0, 0) : new Thickness(0, 8, 0, 0);
        SkeletonThumb.Height = compact ? previewWidth * 9 / 16 : 148;

        ProbeThumbColumn.Width = new GridLength(compact ? 140 : 286);
        ProbePreviewLayout.ColumnDefinitions[1].Width = new GridLength(1, GridUnitType.Star);
        Grid.SetColumn(ProbePreviewBody, 1);
        Grid.SetRow(ProbePreviewBody, 0);
        ProbePreviewBody.Margin = new Thickness(0);
        ProbePreviewThumb.Width = compact ? 120 : 264;
        ProbePreviewThumb.Height = compact ? 67.5 : 148;
        ProbePreviewThumb.HorizontalAlignment = HorizontalAlignment.Left;

        SubtitleDialog.Width = Math.Min(940, width * .94);
        DeleteDialog.Width = Math.Min(520, width * .92);
        ToastHost.Margin = compact ? new Thickness(0, 0, 14, 76) : new Thickness(0, 0, 22, 22);
        RestoreNavigationAfterResize();
    }
}
