using System.IO;
using System.Windows;
using Microsoft.Win32;

namespace Jiying.Desktop;

public partial class MainWindow
{
    private void BrowseOutput_Click(object sender, RoutedEventArgs e) =>
        PickOutputDirectory(OutputPathBox.Text);

    private void BrowseSettingsOutput_Click(object sender, RoutedEventArgs e) =>
        PickOutputDirectory(_ui.OutputDirectory);

    private void PickOutputDirectory(string start)
    {
        try
        {
            var dialog = new OpenFolderDialog
            {
                Title = "选择输出目录",
                InitialDirectory = OutputPickerStart(start),
                AddToRecent = false
            };
            if (dialog.ShowDialog(this) == true) SetOutputDirectory(dialog.FolderName);
        }
        catch (Exception ex)
        {
            DesktopDiagnostics.Write("选择输出目录", ex);
            ShowError(ex.Message);
        }
    }

    internal static string OutputPickerStart(string start)
    {
        var candidate = Path.IsPathFullyQualified(start.Trim()) ? start.Trim() : null;
        while (candidate is not null)
        {
            if (Directory.Exists(candidate)) return candidate;
            candidate = Path.GetDirectoryName(candidate);
        }
        return Environment.GetFolderPath(Environment.SpecialFolder.MyDocuments);
    }

    private void SetOutputDirectory(string path)
    {
        _ui.OutputDirectory = path;
        _ui.Save();
        UpdateOutputPathUi();
    }

    private void OutputPathBox_LostFocus(object sender, RoutedEventArgs e)
    {
        var path = OutputPathBox.Text.Trim();
        if (string.IsNullOrWhiteSpace(path)) path = UiSettings.DefaultOutputDirectory;
        SetOutputDirectory(path);
    }

    private void UpdateOutputPathUi()
    {
        OutputPathBox.Text = _ui.OutputDirectory;
        SettingsPathText.Text = _ui.OutputDirectory;
        OutputPathSideText.Text = _ui.OutputDirectory;
        try
        {
            var root = Path.GetPathRoot(_ui.OutputDirectory);
            var drive = string.IsNullOrWhiteSpace(root) ? null : new DriveInfo(root);
            DiskFreeText.Text = drive is { IsReady: true } ? $"剩余 {drive.AvailableFreeSpace / 1073741824d:0.#} GB" : "保存到本地磁盘";
        }
        catch { DiskFreeText.Text = "保存到本地磁盘"; }
    }
}
