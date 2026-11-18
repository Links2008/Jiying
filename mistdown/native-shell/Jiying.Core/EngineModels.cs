using System.ComponentModel;
using System.IO;
using System.Text.Json;
using System.Text.Json.Serialization;

namespace Jiying.Desktop;

// 会话传输类型与界面解耦，独立客户端回归无需启动 WebView2。
public sealed record WebsiteCookie(string Domain, string Name, string Value, string Path,
                                    bool Secure, bool HttpOnly, long Expires);

internal sealed class EngineConfig
{
    [JsonPropertyName("download")] public DownloadConfig Download { get; set; } = new();
    [JsonPropertyName("max_concurrent")] public int MaxConcurrent { get; set; } = 2;
    [JsonPropertyName("model")] public string Model { get; set; } = "small";
    [JsonPropertyName("translator_provider")] public string TranslatorProvider { get; set; } = "null";
    [JsonPropertyName("llm_vendor")] public string LlmVendor { get; set; } = "deepseek";
    [JsonPropertyName("llm_base_url")] public string LlmBaseUrl { get; set; } = "";
    [JsonPropertyName("llm_model")] public string LlmModel { get; set; } = "";
    [JsonPropertyName("llm_api_key_masked")] public string LlmApiKeyMasked { get; set; } = "";
    [JsonPropertyName("transcriber_provider")] public string TranscriberProvider { get; set; } = "faster-whisper";
    [JsonPropertyName("asr_vendor")] public string AsrVendor { get; set; } = "siliconflow";
    [JsonPropertyName("asr_base_url")] public string AsrBaseUrl { get; set; } = "";
    [JsonPropertyName("asr_model")] public string AsrModel { get; set; } = "";
    [JsonPropertyName("asr_api_key_masked")] public string AsrApiKeyMasked { get; set; } = "";
}

internal sealed class DownloadConfig
{
    [JsonPropertyName("proxy_mode")] public string ProxyMode { get; set; } = "system";
    [JsonPropertyName("custom_proxy")] public string CustomProxy { get; set; } = "";
    [JsonPropertyName("aria2c_enabled")] public bool Aria2cEnabled { get; set; } = true;
    [JsonPropertyName("fragments_concurrency")] public int FragmentsConcurrency { get; set; } = 16;
    [JsonPropertyName("cookies")] public string Cookies { get; set; } = "";
    [JsonPropertyName("cookies_from_browser")] public string CookiesFromBrowser { get; set; } = "";
}

internal sealed class EngineConfigUpdate
{
    [JsonPropertyName("proxy_mode")] public string ProxyMode { get; init; } = "system";
    [JsonPropertyName("custom_proxy")] public string CustomProxy { get; init; } = "";
    [JsonPropertyName("aria2c_enabled")] public bool Aria2cEnabled { get; init; }
    [JsonPropertyName("fragments_concurrency")] public int FragmentsConcurrency { get; init; }
    [JsonPropertyName("cookies"), JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)] public string? Cookies { get; init; }
    [JsonPropertyName("cookies_from_browser"), JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)] public string? CookiesFromBrowser { get; init; }
    [JsonPropertyName("max_concurrent")] public int MaxConcurrent { get; init; }
    [JsonPropertyName("model")] public string Model { get; init; } = "small";
    [JsonPropertyName("translator_provider")] public string TranslatorProvider { get; init; } = "null";
    [JsonPropertyName("llm_api_key")] public string LlmApiKey { get; init; } = "";
    [JsonPropertyName("llm_model")] public string LlmModel { get; init; } = "";
    [JsonPropertyName("llm_vendor")] public string LlmVendor { get; init; } = "deepseek";
    [JsonPropertyName("llm_base_url")] public string LlmBaseUrl { get; init; } = "";
    [JsonPropertyName("transcriber_provider")] public string TranscriberProvider { get; init; } = "faster-whisper";
    [JsonPropertyName("asr_vendor")] public string AsrVendor { get; init; } = "siliconflow";
    [JsonPropertyName("asr_base_url")] public string AsrBaseUrl { get; init; } = "";
    [JsonPropertyName("asr_api_key")] public string AsrApiKey { get; init; } = "";
    [JsonPropertyName("asr_model")] public string AsrModel { get; init; } = "";
}

internal sealed class ProbeResult
{
    [JsonPropertyName("engine")] public string Engine { get; set; } = "";
    [JsonPropertyName("title")] public string Title { get; set; } = "";
    [JsonPropertyName("duration")] public double Duration { get; set; }
    [JsonPropertyName("thumbnail")] public string Thumbnail { get; set; } = "";
    [JsonPropertyName("quality_notice")] public string QualityNotice { get; set; } = "";
    [JsonPropertyName("auth_status")] public string AuthStatus { get; set; } = "none";
    [JsonPropertyName("formats")] public List<FormatInfo> Formats { get; set; } = [];
}

public sealed class FormatInfo
{
    [JsonPropertyName("format_id")] public string FormatId { get; set; } = "best";
    [JsonPropertyName("label")] public string Label { get; set; } = "源画质";
    [JsonPropertyName("height")] public int Height { get; set; }
    [JsonPropertyName("fps")] public double Fps { get; set; }
    [JsonPropertyName("filesize")] public long FileSize { get; set; }
    [JsonPropertyName("size_is_estimate")] public bool SizeIsEstimate { get; set; }
    [JsonPropertyName("protocol")] public string Protocol { get; set; } = "";
    [JsonPropertyName("vcodec")] public string VideoCodec { get; set; } = "";
    [JsonPropertyName("dynamic_range")] public string DynamicRange { get; set; } = "";
    public string DetailText => string.Join(" · ", new[]
    {
        Fps > 0 ? FpsText : "",
        VideoCodec.StartsWith("av01") ? "AV1" : VideoCodec.StartsWith("avc") ? "H.264" :
            VideoCodec.StartsWith("hev") || VideoCodec.StartsWith("hvc") ? "HEVC" : VideoCodec,
        DynamicRange.Equals("SDR", StringComparison.OrdinalIgnoreCase) ? "" : DynamicRange
    }.Where(value => !string.IsNullOrWhiteSpace(value) && value != "none"));
    public string FpsText => Fps > 0 ? $"{Fps:0.#} fps" : "-";
    public string SizeText => FileSize > 0 ? (SizeIsEstimate ? "约 " : "") +
        (FileSize >= 1073741824 ? $"{FileSize / 1073741824d:0.##} GiB" : $"{FileSize / 1048576d:0.#} MiB") : "大小未知";
    public string SelectionText => string.Join(" · ", new[] { Label, DetailText, SizeText }.Where(value => !string.IsNullOrWhiteSpace(value)));
    public string ProtocolText => string.IsNullOrWhiteSpace(Protocol) ? "-" : Protocol;
}

internal sealed class SubmitTaskRequest
{
    [JsonPropertyName("url")] public string Url { get; init; } = "";
    [JsonPropertyName("resolution")] public string Resolution { get; init; } = "best";
    [JsonPropertyName("langs")] public string[] Langs { get; init; } = [];
    [JsonPropertyName("recognize")] public bool Recognize { get; init; }
    [JsonPropertyName("translate")] public bool Translate { get; init; }
    [JsonPropertyName("burn")] public bool Burn { get; init; }
    [JsonPropertyName("bilingual")] public bool Bilingual { get; init; }
    [JsonPropertyName("person_notes")] public bool PersonNotes { get; init; }
    [JsonPropertyName("audio_subtitles"), JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public bool? AudioSubtitles { get; init; }
    [JsonPropertyName("output_dir")] public string OutputDirectory { get; init; } = "";
    [JsonPropertyName("font_name")] public string FontName { get; init; } = "";
    [JsonPropertyName("font_size")] public int FontSize { get; init; }
    [JsonPropertyName("position")] public string Position { get; init; } = "bottom";
    [JsonPropertyName("source_lang")] public string SourceLanguage { get; init; } = "";
    [JsonPropertyName("outline")] public int Outline { get; init; } = 1;
    [JsonPropertyName("color")] public string Color { get; init; } = "#FFFFFF";
    [JsonPropertyName("vertical_offset")] public int VerticalOffset { get; init; }
    [JsonPropertyName("horizontal_offset")] public int HorizontalOffset { get; init; }
}

internal sealed class BilibiliLoginStatus
{
    [JsonPropertyName("session_id")] public string SessionId { get; set; } = "";
    [JsonPropertyName("image_base64")] public string ImageBase64 { get; set; } = "";
    [JsonPropertyName("status")] public string Status { get; set; } = "";
    [JsonPropertyName("message")] public string Message { get; set; } = "";
}

internal sealed record TaskSnapshot(List<TaskItem> Items, long? Sequence);

internal sealed record TaskEventFrame(TaskEvent? Event, long? Head);

internal sealed class TaskEvent
{
    [JsonPropertyName("type")] public string Type { get; init; } = "";
    [JsonPropertyName("task_id")] public string TaskId { get; init; } = "";
    [JsonPropertyName("stage")] public string Stage { get; init; } = "";
    [JsonPropertyName("status")] public string Status { get; init; } = "";
    [JsonPropertyName("fraction")] public double Fraction { get; init; }
    [JsonPropertyName("speed_bps")] public double SpeedBps { get; init; }
    [JsonPropertyName("eta")] public double Eta { get; init; }
    [JsonPropertyName("note")] public string? Note { get; init; }
    [JsonPropertyName("sequence")] public long Sequence { get; init; }

    public TaskProgress ToProgress() => new()
    {
        Stage = Stage, Fraction = Math.Clamp(Fraction, 0, 1),
        SpeedBps = Math.Max(0, SpeedBps), Eta = Math.Max(0, Eta), Note = Note ?? ""
    };
}

public sealed class TaskProgress
{
    [JsonPropertyName("stage")] public string Stage { get; set; } = "";
    [JsonPropertyName("fraction")] public double Fraction { get; set; }
    [JsonPropertyName("speed_bps")] public double SpeedBps { get; set; }
    [JsonPropertyName("eta")] public double Eta { get; set; }
    [JsonPropertyName("note")] public string Note { get; set; } = "";
}

public sealed class StageInfo
{
    [JsonPropertyName("stage")] public string Stage { get; set; } = "";
    [JsonPropertyName("status")] public string Status { get; set; } = "";
}

public sealed class StageView
{
    public string Name { get; init; } = "";
    public string Status { get; init; } = "";
}

public sealed class ArtifactItem
{
    [JsonPropertyName("id")] public int Id { get; set; }
    [JsonPropertyName("kind")] public string Kind { get; set; } = "";
    [JsonPropertyName("path")] public string Path { get; set; } = "";
    [JsonPropertyName("lang")] public string Language { get; set; } = "";
    [JsonPropertyName("size")] public long Size { get; set; }
    [JsonIgnore] public string TaskId { get; set; } = "";
    public string FileName => System.IO.Path.GetFileName(Path);
    public string SizeText => Size > 0 ? $"{Size / 1048576d:0.#} MB" : "";
    public bool IsSubtitle => (Kind is "subtitle" or "bilingual") && Path.EndsWith(".srt", StringComparison.OrdinalIgnoreCase)
        || Kind == "person_notes" && Path.EndsWith(".ass", StringComparison.OrdinalIgnoreCase);
    public string EditText => Kind == "bilingual" ? "修改字幕 · 双语" : $"修改字幕 · {LanguageName(Language)}";
    public string DisplayText => Kind == "person_notes" ? "人名注释 .ass" : Kind == "bilingual" ? "双语上下 .srt" : IsSubtitle
        ? $"{LanguageName(Language)} .srt"
        : $"{FileName}{(Size > 0 ? $" · {SizeText}" : "")}";

    private static string LanguageName(string code) => code switch
    {
        "zh" => "中文", "en" => "英语", "ja" => "日语", "ko" => "韩语",
        "es" => "西语", "fr" => "法语", "de" => "德语", "ru" => "俄语",
        "pt" => "葡语", "th" => "泰语", "vi" => "越南语",
        _ => string.IsNullOrWhiteSpace(code) ? "源语言" : code
    };
}

public sealed class TaskItem : INotifyPropertyChanged
{
    public event PropertyChangedEventHandler? PropertyChanged;
    private static readonly string[] ProgressProperties =
        [nameof(Progress), nameof(ProgressValue), nameof(ProgressPercent), nameof(DetailText), nameof(SpeedText)];

    public void RefreshFrom(TaskItem other)
    {
        var changed = Url != other.Url || Title != other.Title || Status != other.Status ||
            Resolution != other.Resolution || Error != other.Error || CreatedAt != other.CreatedAt ||
            OutputDirectory != other.OutputDirectory || SubtitleMode != other.SubtitleMode || Translate != other.Translate ||
            AudioSubtitles != other.AudioSubtitles || NameAnnotations != other.NameAnnotations ||
            Enhance != other.Enhance || EnhanceScale != other.EnhanceScale ||
            Progress.Stage != other.Progress.Stage ||
            !Artifacts.Select(a => (a.Id, a.Kind, a.Path, a.Size, a.Language))
                .SequenceEqual(other.Artifacts.Select(a => (a.Id, a.Kind, a.Path, a.Size, a.Language))) ||
            !Stages.Select(stage => (stage.Stage, stage.Status))
                .SequenceEqual(other.Stages.Select(stage => (stage.Stage, stage.Status)));
        var progressChanged = Progress.Fraction != other.Progress.Fraction ||
            Progress.SpeedBps != other.Progress.SpeedBps || Progress.Eta != other.Progress.Eta || Progress.Note != other.Progress.Note;
        Url = other.Url;
        Title = other.Title;
        Status = other.Status;
        Resolution = other.Resolution;
        Error = other.Error;
        CreatedAt = other.CreatedAt;
        FinishedAt = other.FinishedAt;
        OutputDirectory = other.OutputDirectory;
        SubtitleMode = other.SubtitleMode;
        Translate = other.Translate;
        AudioSubtitles = other.AudioSubtitles;
        NameAnnotations = other.NameAnnotations;
        Enhance = other.Enhance;
        EnhanceScale = other.EnhanceScale;
        if (changed)
        {
            Progress = other.Progress;
            Stages = other.Stages;
            Artifacts = other.Artifacts;
            PropertyChanged?.Invoke(this, new PropertyChangedEventArgs(string.Empty));
        }
        else if (progressChanged) SetProgress(other.Progress);
    }

    internal void ApplyProgress(TaskProgress update)
    {
        if (Status != "running" || string.IsNullOrEmpty(update.Stage) ||
            Progress.Stage != update.Stage && (Progress.Stage.Length != 0 ||
                !Stages.Any(stage => stage.Stage == update.Stage && stage.Status == "running")))
            return;
        if (update.Fraction < Progress.Fraction)
            update.Fraction = Progress.Fraction;
        if (Progress.Fraction != update.Fraction || Progress.SpeedBps != update.SpeedBps ||
            Progress.Eta != update.Eta || Progress.Note != update.Note || Progress.Stage != update.Stage)
            SetProgress(update);
    }

    private void SetProgress(TaskProgress update)
    {
        var stageChanged = Progress.Stage != update.Stage;
        Progress = update;
        foreach (var name in ProgressProperties) PropertyChanged?.Invoke(this, new PropertyChangedEventArgs(name));
        if (stageChanged) PropertyChanged?.Invoke(this, new PropertyChangedEventArgs(nameof(StageText)));
    }

    private static readonly (string Id, string Name, double Weight)[] StageOrder =
    [
        ("download", "下载", .40), ("visual_names", "画面人名", .10), ("extract", "提取音频", .05),
        ("transcribe", "语音识别", .35), ("translate", "翻译字幕", .10),
        ("render", "封装输出", .10)
    ];

    [JsonPropertyName("id")] public string Id { get; set; } = "";
    [JsonPropertyName("url")] public string Url { get; set; } = "";
    [JsonPropertyName("title")] public string Title { get; set; } = "";
    [JsonPropertyName("status")] public string Status { get; set; } = "";
    [JsonPropertyName("resolution")] public string Resolution { get; set; } = "best";
    [JsonPropertyName("error")] public string Error { get; set; } = "";
    [JsonPropertyName("created_at")] public string CreatedAt { get; set; } = "";
    [JsonPropertyName("finished_at")] public string FinishedAt { get; set; } = "";
    [JsonPropertyName("output_dir")] public string OutputDirectory { get; set; } = "";
    [JsonPropertyName("subtitle_mode")] public string SubtitleMode { get; set; } = "";
    [JsonPropertyName("translate")] public int Translate { get; set; } = 1;
    [JsonPropertyName("audio_subtitles")] public int AudioSubtitles { get; set; } = 1;
    [JsonPropertyName("person_notes")] public int NameAnnotations { get; set; }
    [JsonPropertyName("enhance")] public int Enhance { get; set; }
    [JsonPropertyName("enhance_scale")] public int EnhanceScale { get; set; } = 2;
    [JsonPropertyName("progress")] public TaskProgress Progress { get; set; } = new();
    [JsonPropertyName("stages")] public List<StageInfo> Stages { get; set; } = [];
    [JsonPropertyName("artifacts")] public List<ArtifactItem> Artifacts { get; set; } = [];
    [JsonIgnore] public List<ArtifactItem> EditableSubtitles => Artifacts.Where(a => a.IsSubtitle && a.Kind != "person_notes")
        .OrderByDescending(a => a.Kind == "bilingual").ToList();
    [JsonIgnore] public bool HasEditableSubtitles => EditableSubtitles.Count > 0;
    [JsonIgnore] public ArtifactItem? PersonNotes => Artifacts.Where(a => a.Kind == "person_notes")
        .MaxBy(a => a.Id);
    [JsonIgnore] public bool HasPersonNotes => PersonNotes is not null;

    public string DisplayTitle => string.IsNullOrWhiteSpace(Title) ? Url : Title;
    public bool IsTerminal => Status is "completed" or "failed" or "cancelled" or "interrupted";
    public bool CanRetry => Status is "failed" or "cancelled" or "interrupted"
        || Status == "completed" && !string.IsNullOrWhiteSpace(Error);
    public bool CanReveal => Status == "completed";
    public double ProgressValue => Status == "completed" ? 100 : Math.Clamp(CalculateProgress() * 100, 0, 99.9);
    public string ProgressPercent => $"{(Status == "completed" ? 100 : Math.Floor(ProgressValue)):0}%";
    public string StatusText => Status switch
    {
        "created" => "已创建", "pending" => "等待中", "running" => "进行中",
        "completed" when !string.IsNullOrWhiteSpace(Error) => "视频已下载",
        "completed" => "下载完成", "failed" => "失败", "cancelled" => "已取消",
        "interrupted" => "已中断", _ => string.IsNullOrWhiteSpace(Status) ? "未知状态" : Status
    };
    public string StageText => StageName(Progress.Stage);
    public string DetailText
    {
        get
        {
            var bits = new List<string>();
            if (Status is "pending" or "created") bits.Add("排队等待引擎调度");
            else if (string.IsNullOrWhiteSpace(Progress.Note) && !string.IsNullOrWhiteSpace(StageText))
                bits.Add($"{StageText} {Math.Clamp(Progress.Fraction * 100, 0, 100):0}%");
            if (!string.IsNullOrWhiteSpace(Progress.Note)) bits.Add(Progress.Note);
            if (Progress.Stage == "download" && Progress.SpeedBps > 0) bits.Add(SpeedText);
            if (Progress.Eta is > 0 and < 7200) bits.Add(Progress.Eta >= 90 ? $"剩余 {Math.Round(Progress.Eta / 60)} 分钟" : $"剩余 {Math.Round(Progress.Eta)} 秒");
            return string.Join("，", bits);
        }
    }
    public string SpeedText => Progress.SpeedBps <= 0 ? "" : Progress.SpeedBps >= 1048576
        ? $"{Progress.SpeedBps / 1048576d:0.0} MB/s" : $"{Progress.SpeedBps / 1024d:0} KB/s";
    public string ErrorText => ReadError(Error);
    public string CreatedText => DateTime.TryParse(CreatedAt, out var value) ? value.ToString("MM-dd HH:mm") : CreatedAt;
    public string OutputMeta => $"{CreatedText} · {(string.IsNullOrWhiteSpace(OutputDirectory) ? "默认目录" : OutputDirectory)}";
    public string SubtitleModeText => SubtitleMode switch { "none" => "仅视频", "burn" => "硬字幕已烧录", _ => "软字幕轨" };
    private IEnumerable<(string Id, string Name, double Weight)> ActiveStages =>
        StageOrder.Where(item => (SubtitleMode != "none" || item.Id == "download")
            && (Translate != 0 || item.Id != "translate")
            && (AudioSubtitles != 0 || item.Id is not ("extract" or "transcribe"))
            && (NameAnnotations != 0 || item.Id != "visual_names"));
    public List<StageView> StageViews => ActiveStages.Select(item => new StageView
    {
        Name = item.Name,
        Status = Stages.FirstOrDefault(stage => stage.Stage == item.Id)?.Status
            ?? (Progress.Stage == item.Id && Status == "running" ? "running" : "")
    }).ToList();

    private double CalculateProgress()
    {
        if (Stages.Count == 0) return Progress.Fraction;
        var total = 0d;
        var stages = ActiveStages.ToArray();
        foreach (var item in stages)
        {
            var status = Stages.FirstOrDefault(stage => stage.Stage == item.Id)?.Status;
            if (status == "done" || item.Id == "visual_names" && status == "failed") { total += item.Weight; continue; }
            if (status == "running" && Progress.Stage == item.Id)
                total += item.Weight * Math.Clamp(Progress.Fraction, 0, 1);
            break;
        }
        return total / stages.Sum(item => item.Weight);
    }

    private static string StageName(string stage) => stage switch
    {
        "download" => "下载", "enhance" => "旧版处理", "extract" => "提取音频", "transcribe" => "语音识别",
        "translate" => "翻译字幕", "visual_names" => "画面人名", "render" => "封装输出", _ => ""
    };

    private static string ReadError(string raw)
    {
        if (string.IsNullOrWhiteSpace(raw)) return "";
        try
        {
            using var doc = JsonDocument.Parse(raw);
            if (!doc.RootElement.TryGetProperty("message", out var message)) return raw;
            return (message.GetString() ?? raw) +
                (doc.RootElement.TryGetProperty("suggestion", out var suggestion) && !string.IsNullOrWhiteSpace(suggestion.GetString())
                    ? "\n" + suggestion.GetString() : "");
        }
        catch (JsonException) { return raw; }
    }
}

internal sealed class ArtifactContent
{
    [JsonPropertyName("text")] public string Text { get; set; } = "";
    [JsonPropertyName("source_text")] public string SourceText { get; set; } = "";
    [JsonPropertyName("source_language")] public string SourceLanguage { get; set; } = "";
    [JsonPropertyName("is_source")] public bool IsSource { get; set; }
}

internal sealed class DeleteResult
{
    [JsonPropertyName("deleted_files")] public List<string> DeletedFiles { get; set; } = [];
    [JsonPropertyName("skipped")] public List<string> Skipped { get; set; } = [];
}

internal sealed class WhisperStatus
{
    [JsonPropertyName("model")] public string Model { get; set; } = "";
    [JsonPropertyName("status")] public string Status { get; set; } = "";
    [JsonPropertyName("downloaded")] public bool Downloaded { get; set; }
    [JsonPropertyName("fraction")] public double Fraction { get; set; }
    [JsonPropertyName("error")] public string Error { get; set; } = "";
    [JsonPropertyName("message")] public string Message { get; set; } = "";
}

internal sealed class GpuStatus
{
    [JsonPropertyName("supported")] public bool Supported { get; set; }
    [JsonPropertyName("downloaded")] public bool Downloaded { get; set; }
    [JsonPropertyName("status")] public string Status { get; set; } = "";
    [JsonPropertyName("fraction")] public double Fraction { get; set; }
    [JsonPropertyName("size_bytes")] public long SizeBytes { get; set; }
    [JsonPropertyName("error")] public string Error { get; set; } = "";
    [JsonPropertyName("message")] public string Message { get; set; } = "";
}

internal sealed class YtDlpCheckResult
{
    [JsonPropertyName("ok")] public bool Ok { get; set; }
    [JsonPropertyName("installed")] public string Installed { get; set; } = "";
    [JsonPropertyName("pending")] public string Pending { get; set; } = "";
    [JsonPropertyName("latest_seen")] public string LatestSeen { get; set; } = "";
    [JsonPropertyName("error")] public string Error { get; set; } = "";
}

internal sealed class YtDlpStatus
{
    [JsonPropertyName("active")] public string Active { get; set; } = "";
    [JsonPropertyName("vendored")] public string Vendored { get; set; } = "";
    [JsonPropertyName("pending")] public string Pending { get; set; } = "";
    [JsonPropertyName("latest_seen")] public string LatestSeen { get; set; } = "";
}
