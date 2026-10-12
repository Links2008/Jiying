using System.IO;
using System.Text.Json;

namespace Jiying.Desktop;

internal static class SelfCheck
{
    public static void Run()
    {
        var probe = JsonSerializer.Deserialize<ProbeResult>(
            """{"engine":"yt-dlp","title":"demo","duration":10,"formats":[{"format_id":"18","label":"360p","fps":30,"filesize":1048576}]}""",
            EngineClient.JsonOptions);
        if (probe?.Formats.Count != 1 || probe.Formats[0].FormatId != "18")
            throw new InvalidDataException("ProbeResult JSON mapping failed");

        var task = JsonSerializer.Deserialize<TaskItem>(
            """{"id":"abc","status":"running","progress":{"stage":"download","fraction":0.5,"speed_bps":1048576}}""",
            EngineClient.JsonOptions);
        if (task is null || task.IsTerminal || task.ProgressValue != 50 || task.SpeedText != "1.0 MB/s")
            throw new InvalidDataException("TaskItem JSON mapping failed");

        task.Status = "future-status";
        if (task.IsTerminal || task.StatusText != "future-status")
            throw new InvalidDataException("Unknown task statuses must remain visible");

        var staged = JsonSerializer.Deserialize<TaskItem>(
            """{"id":"weighted","status":"running","progress":{"stage":"transcribe","fraction":0.5},"stages":[{"stage":"download","status":"done"},{"stage":"extract","status":"done"},{"stage":"transcribe","status":"running"}]}""",
            EngineClient.JsonOptions);
        if (staged is null || Math.Abs(staged.ProgressValue - 62.5) > .01 || staged.StageViews.Count != 5)
            throw new InvalidDataException("Weighted task progress mapping failed");

        var request = new SubmitTaskRequest
        {
            Url = "https://example.test/video.mp4", Resolution = "1080p", Langs = ["zh", "en"],
            Burn = true, Bilingual = true, OutputDirectory = @"D:\Downloads", FontSize = 24,
            Position = "bottom", SourceLanguage = "en", Outline = 2
        };
        var json = JsonSerializer.Serialize(request, EngineClient.JsonOptions);
        if (!json.Contains("\"output_dir\"") || !json.Contains("\"source_lang\"") || !json.Contains("\"bilingual\": true"))
            throw new InvalidDataException("Full submit request JSON mapping failed");
    }
}
