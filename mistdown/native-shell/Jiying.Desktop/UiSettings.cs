using System.IO;
using System.Text.Json;

namespace Jiying.Desktop;

internal sealed class UiSettings
{
    private static string VideosOutputDirectory => Path.Combine(
        Environment.GetFolderPath(Environment.SpecialFolder.MyVideos) is { Length: > 0 } videos
            ? videos : Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.UserProfile), "Videos"), "即影");
    internal static string DefaultOutputDirectory => ResolveDefaultOutputDirectory(Directory.Exists(@"D:\"));

    internal static string ResolveDefaultOutputDirectory(bool dDriveAvailable) =>
        dDriveAvailable ? @"D:\即影\Downloads" : VideosOutputDirectory;

    internal static string NormalizeOutputDirectory(string? path, bool dDriveAvailable)
    {
        if (string.IsNullOrWhiteSpace(path) ||
            dDriveAvailable && path.Equals(VideosOutputDirectory, StringComparison.OrdinalIgnoreCase) ||
            !dDriveAvailable && path.Equals(@"D:\即影\Downloads", StringComparison.OrdinalIgnoreCase))
            return ResolveDefaultOutputDirectory(dDriveAvailable);
        return path;
    }

    public string Accent { get; set; } = "icon";
    public string Theme { get; set; } = "auto";
    public int BackgroundOpacity { get; set; } = 100;
    public bool Notify { get; set; } = true;
    public bool AutoParse { get; set; } = true;
    public string OutputDirectory { get; set; } = DefaultOutputDirectory;
    public string SourceLanguage { get; set; } = "";
    public bool Burn { get; set; }
    public bool Recognize { get; set; }
    public bool Translate { get; set; }
    public bool Bilingual { get; set; }
    public bool PersonNotes { get; set; }
    public bool VisualNamesOnly { get; set; }
    public bool TranslateVisualNames { get; set; }
    public string FontName { get; set; } = "Noto Sans CJK SC";
    public int FontSize { get; set; } = 18;
    public string SubtitlePosition { get; set; } = "bottom";
    public int Outline { get; set; } = 1;
    public string SubtitleColor { get; set; } = "#FFFFFF";
    public int SubtitleVerticalOffset { get; set; }
    public int SubtitleHorizontalOffset { get; set; }

    internal static string SettingsPath => Environment.GetEnvironmentVariable("JIYING_UI_SETTINGS_PATH") is { Length: > 0 } path
        ? path : Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "Jiying", "native-ui.json");

    internal static string NotificationStatusPath => Path.Combine(Path.GetDirectoryName(SettingsPath)!, "native-notification-status.json");

    internal static bool NotificationsEnabled()
    {
        try { return !File.Exists(SettingsPath) || JsonSerializer.Deserialize<UiSettings>(File.ReadAllText(SettingsPath), EngineClient.JsonOptions)?.Notify != false; }
        catch (Exception) { return true; }
    }

    public static UiSettings Load()
    {
        try
        {
            if (!File.Exists(SettingsPath)) return new();
            var settings = JsonSerializer.Deserialize<UiSettings>(File.ReadAllText(SettingsPath), EngineClient.JsonOptions) ?? new();
            var normalized = NormalizeOutputDirectory(settings.OutputDirectory, Directory.Exists(@"D:\"));
            if (!string.Equals(settings.OutputDirectory, normalized, StringComparison.Ordinal))
            {
                settings.OutputDirectory = normalized;
                settings.Save();
            }
            return settings;
        }
        catch (Exception) { return new(); }
    }

    public void Save()
    {
        try
        {
            Directory.CreateDirectory(Path.GetDirectoryName(SettingsPath)!);
            File.WriteAllText(SettingsPath, JsonSerializer.Serialize(this, EngineClient.JsonOptions));
        }
        catch (Exception) { }
    }
}
