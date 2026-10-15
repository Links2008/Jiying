using System.ComponentModel;
using System.Text;
using System.Text.RegularExpressions;

namespace Jiying.Desktop;

public sealed class SubtitleCue : INotifyPropertyChanged
{
    public event PropertyChangedEventHandler? PropertyChanged;
    internal Action? Changed { get; set; }
    internal int Offset, Length;
    internal long Start, End;
    private string _text = "";
    public string Number { get; internal set; } = "";
    public string Timing { get; internal set; } = "";
    public string TimingDisplay => Timing.Replace(" --> ", "\n");
    public string ReferenceText { get; internal set; } = "未找到对应原文";
    public string Text
    {
        get => _text;
        set
        {
            if (_text == value) return;
            _text = value;
            PropertyChanged?.Invoke(this, new(nameof(Text)));
            Changed?.Invoke();
        }
    }
}

internal sealed class SubtitleDocument
{
    private readonly string _original;
    private readonly string _newline;
    public List<SubtitleCue> Cues { get; } = [];
    private const string Timestamp = @"\d{2,}:[0-5]\d:[0-5]\d,\d{3}";
    private static readonly Regex Blocks = new(
        @"^\ufeff?(?<number>\d+)[ \t]*\r?\n(?<time>(?<start>" + Timestamp + @")[ \t]+-->[ \t]+(?<end>" + Timestamp + @"))[ \t]*\r?\n(?<body>.*?)(?=\r?\n[ \t]*\r?\n|\z)",
        RegexOptions.Multiline | RegexOptions.Singleline);

    private SubtitleDocument(string text)
    {
        _original = text;
        _newline = text.Contains("\r\n") ? "\r\n" : "\n";
    }

    public static SubtitleDocument Parse(string text)
    {
        var document = new SubtitleDocument(text);
        var cursor = 0;
        foreach (Match match in Blocks.Matches(text))
        {
            if (text[cursor..match.Index].Trim('\ufeff', ' ', '\r', '\n', '\t').Length > 0)
                throw new FormatException("字幕包含无法读取的条目，原稿已保留。");
            var body = match.Groups["body"];
            var content = body.Value.TrimEnd('\r', '\n');
            var start = Milliseconds(match.Groups["start"].Value);
            var end = Milliseconds(match.Groups["end"].Value);
            if (end <= start || string.IsNullOrWhiteSpace(content))
                throw new FormatException("字幕文字为空或时间段无效，原稿已保留。");
            document.Cues.Add(new SubtitleCue { Number = match.Groups["number"].Value,
                Timing = match.Groups["time"].Value, Offset = body.Index, Length = content.Length,
                Start = start, End = end, Text = content });
            cursor = match.Index + match.Length;
        }
        if (document.Cues.Count == 0 || text[cursor..].Trim().Length > 0)
            throw new FormatException("字幕格式无法逐条读取，原稿已保留。");
        return document;
    }

    private static long Milliseconds(string timestamp)
    {
        var parts = timestamp.Split(':', ',');
        try { return checked(((long.Parse(parts[0]) * 60 + int.Parse(parts[1])) * 60 + int.Parse(parts[2])) * 1000 + int.Parse(parts[3])); }
        catch (Exception error) when (error is OverflowException or FormatException)
        { throw new FormatException("字幕时间轴超出范围。", error); }
    }

    public string Serialize(bool validate = false)
    {
        var result = new StringBuilder(_original.Length);
        var cursor = 0;
        foreach (var cue in Cues)
        {
            if (validate && (string.IsNullOrWhiteSpace(cue.Text) || Regex.IsMatch(cue.Text, @"\r?\n\s*\r?\n")))
                throw new FormatException($"第 {cue.Number} 条字幕不能为空或包含空白分段。");
            result.Append(_original, cursor, cue.Offset - cursor);
            result.Append(cue.Text == _original.Substring(cue.Offset, cue.Length) ? cue.Text :
                cue.Text.Replace("\r\n", "\n").Replace('\r', '\n').Replace("\n", _newline));
            cursor = cue.Offset + cue.Length;
        }
        result.Append(_original, cursor, _original.Length - cursor);
        return result.ToString();
    }

    public void SetReference(string source)
    {
        if (string.IsNullOrWhiteSpace(source)) return;
        var references = Parse(source).Cues.OrderBy(c => c.Start).ToArray();
        var first = 0;
        foreach (var cue in Cues.OrderBy(c => c.Start))
        {
            while (first < references.Length && references[first].End <= cue.Start) first++;
            var matching = new List<string>();
            for (var i = first; i < references.Length && references[i].Start < cue.End; i++)
                if (references[i].End > cue.Start) matching.Add(references[i].Text);
            cue.ReferenceText = matching.Count == 0 ? "未找到对应原文" : string.Join("\n", matching);
        }
    }
}
