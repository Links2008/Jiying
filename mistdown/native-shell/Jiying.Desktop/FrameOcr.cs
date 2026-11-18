using System.Buffers.Binary;
using System.IO;
using System.IO.Pipes;
using System.Text;
using System.Text.Json;
using Windows.Globalization;
using Windows.Graphics.Imaging;
using Windows.Media.Ocr;
using Windows.Storage;
using Windows.Storage.Streams;

namespace Jiying.Desktop;

internal static class FrameOcr
{
    private const int Protocol = 1;
    private const int MaxPayload = 8 * 1024 * 1024;
    private static readonly JsonSerializerOptions PipeJson = new(EngineClient.JsonOptions) { WriteIndented = false };

    public static async Task<int> ServeAsync(string name)
    {
        const string prefix = "jiying-ocr-";
        if (!name.StartsWith(prefix, StringComparison.Ordinal) ||
            !Guid.TryParseExact(name.AsSpan(prefix.Length), "N", out _)) return 2;
        try
        {
            await using var pipe = new NamedPipeServerStream(name, PipeDirection.InOut, 1, PipeTransmissionMode.Byte,
                PipeOptions.Asynchronous | PipeOptions.CurrentUserOnly);
            using var connectTimeout = new CancellationTokenSource(TimeSpan.FromSeconds(30));
            await pipe.WaitForConnectionAsync(connectTimeout.Token);
            await using var writer = new StreamWriter(pipe, new UTF8Encoding(false), leaveOpen: true) { AutoFlush = true };
            try
            {
                var engine = CreateEngine();
                await SendAsync(writer, new { protocol = Protocol, ok = true, max_dimension = OcrEngine.MaxImageDimension });
                var header = new byte[24];
                byte[] pixels = [];
                while (true)
                {
                    await pipe.ReadExactlyAsync(header);
                    var id = BinaryPrimitives.ReadUInt32LittleEndian(header);
                    var pts = BinaryPrimitives.ReadInt64LittleEndian(header.AsSpan(4));
                    var width = BinaryPrimitives.ReadInt32LittleEndian(header.AsSpan(12));
                    var height = BinaryPrimitives.ReadInt32LittleEndian(header.AsSpan(16));
                    var size = BinaryPrimitives.ReadInt32LittleEndian(header.AsSpan(20));
                    if (width == 0 && height == 0 && size == 0) return 0;
                    if (pts < 0 || width <= 0 || height <= 0 || width > OcrEngine.MaxImageDimension ||
                        height > OcrEngine.MaxImageDimension || size > MaxPayload || size != (long)width * height * 4)
                        throw new InvalidDataException("无效的 OCR 帧尺寸或时间戳。");
                    if (pixels.Length != size) pixels = new byte[size];
                    await pipe.ReadExactlyAsync(pixels);
                    using var buffer = new DataWriter();
                    buffer.WriteBytes(pixels);
                    using var bitmap = SoftwareBitmap.CreateCopyFromBuffer(buffer.DetachBuffer(),
                        BitmapPixelFormat.Bgra8, width, height, BitmapAlphaMode.Ignore);
                    var lines = await ReadLinesAsync(engine, bitmap);
                    await SendAsync(writer, new { ok = true, frame_id = id, pts_ms = pts, lines });
                }
            }
            catch (Exception error)
            {
                if (pipe.IsConnected) await SendAsync(writer, Failure(error));
                return 1;
            }
        }
        catch (Exception error) when (error is IOException or OperationCanceledException or UnauthorizedAccessException)
        {
            return 1;
        }
    }

    public static Task<int> ProbeAsync(string output) => WriteResponseAsync(output, () =>
    {
        var (language, english) = ProbeLanguage();
        return Task.FromResult<object>(new { protocol = Protocol, ok = true,
            language, english_available = english,
            max_dimension = OcrEngine.MaxImageDimension });
    });

    private static (string Language, bool English) ProbeLanguage()
    {
        var language = CreateEngine().RecognizerLanguage.LanguageTag;
        return (language, language.StartsWith("en", StringComparison.OrdinalIgnoreCase));
    }

    internal static string LanguageHint()
    {
        try
        {
            var (language, english) = ProbeLanguage();
            return english ? "" : $"英语 OCR 组件不可用，当前使用 {language}。可在 Windows 语言设置中安装。";
        }
        catch (Exception error) when (error is InvalidOperationException or System.Runtime.InteropServices.COMException)
        {
            return "Windows 文字识别不可用。请检查系统语言组件。";
        }
    }

    public static Task<int> ScanFileBatchAsync(string directory, string output) =>
        WriteResponseAsync(output, async () => await ScanAsync(directory));

    private static async Task<int> WriteResponseAsync(string output, Func<Task<object>> create)
    {
        try
        {
            await File.WriteAllTextAsync(output, JsonSerializer.Serialize(await create(), EngineClient.JsonOptions));
            return 0;
        }
        catch (Exception error)
        {
            try { await File.WriteAllTextAsync(output, JsonSerializer.Serialize(Failure(error), EngineClient.JsonOptions)); }
            catch (Exception writeError) when (writeError is IOException or UnauthorizedAccessException or ArgumentException) { }
            return 1;
        }
    }

    private static object Failure(Exception error) =>
        new { protocol = Protocol, ok = false, error = error.Message, code = error.GetType().Name };

    private static Task SendAsync(StreamWriter writer, object payload) =>
        writer.WriteLineAsync(JsonSerializer.Serialize(payload, PipeJson));

    private static OcrEngine CreateEngine()
    {
        var language = OcrEngine.AvailableRecognizerLanguages.FirstOrDefault(
            lang => lang.LanguageTag.StartsWith("en", StringComparison.OrdinalIgnoreCase));
        var engine = (language is null ? null : OcrEngine.TryCreateFromLanguage(new Language(language.LanguageTag)))
            ?? OcrEngine.TryCreateFromUserProfileLanguages();
        return engine ?? throw new InvalidOperationException("请在 Windows 语言设置中安装文字识别组件。");
    }

    private static async Task<SortedDictionary<int, List<TextLine>>> ScanAsync(string directory)
    {
        var engine = CreateEngine();
        var results = new SortedDictionary<int, List<TextLine>>();
        foreach (var file in Directory.EnumerateFiles(directory, "frame-*.png"))
        {
            if (!int.TryParse(Path.GetFileNameWithoutExtension(file).AsSpan(6), out var number)) continue;
            var source = await StorageFile.GetFileFromPathAsync(Path.GetFullPath(file));
            using var stream = await source.OpenReadAsync();
            var decoder = await BitmapDecoder.CreateAsync(stream);
            using var bitmap = await decoder.GetSoftwareBitmapAsync(BitmapPixelFormat.Bgra8, BitmapAlphaMode.Premultiplied);
            var rows = await ReadLinesAsync(engine, bitmap);
            if (rows.Count > 0) results[number] = rows;
        }
        return results;
    }

    private static async Task<List<TextLine>> ReadLinesAsync(OcrEngine engine, SoftwareBitmap bitmap)
    {
        var text = await engine.RecognizeAsync(bitmap);
        var rows = new List<TextLine>();
        foreach (var line in text.Lines)
        {
            if (line.Words.Count == 0 || string.IsNullOrWhiteSpace(line.Text)) continue;
            var top = line.Words.Min(word => word.BoundingRect.Top);
            var left = line.Words.Min(word => word.BoundingRect.Left);
            var right = line.Words.Max(word => word.BoundingRect.Right);
            var bottom = line.Words.Max(word => word.BoundingRect.Bottom);
            rows.Add(new TextLine(line.Text, left / bitmap.PixelWidth, top / bitmap.PixelHeight,
                (right - left) / bitmap.PixelWidth, (bottom - top) / bitmap.PixelHeight));
        }
        return rows;
    }

    private sealed record TextLine(string Text, double X, double Y, double Width, double Height);
}
