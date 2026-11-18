using Jiying.Desktop;
using Xunit;

namespace Jiying.Tests;

public sealed class SubtitleDocumentTests
{
    private const string Original =
        "7\r\n00:00:00,000 --> 00:00:01,000\r\n原文\r\n第二行\r\n\r\n9\r\n00:00:01,000 --> 00:00:02,000\r\n下一句\r\n";

    [Fact]
    public void EditingReplacesOnlyCueText()
    {
        var document = SubtitleDocument.Parse(Original);
        Assert.Equal(Original, document.Serialize());
        document.Cues[0].Text = "修正后\n第二行";
        Assert.Equal(Original.Replace("原文", "修正后"), document.Serialize(true));
    }

    [Theory]
    [InlineData("")]
    [InlineData("一行\n\n另一行")]
    public void SaveRejectsEmptyOrSplitCue(string text)
    {
        var document = SubtitleDocument.Parse(Original);
        document.Cues[0].Text = text;
        Assert.Throws<FormatException>(() => document.Serialize(true));
    }

    [Theory]
    [InlineData("1\n00:00:00,000 --> 00:00:01,000\n有效\n\n损坏块")]
    [InlineData("1\n00:00:01,000 --> 00:00:00,000\n倒序")]
    public void ParserRejectsMalformedCue(string text) =>
        Assert.Throws<FormatException>(() => SubtitleDocument.Parse(text));

    [Fact]
    public void ReferenceUsesTimingAndDoesNotBorrowUnrelatedCue()
    {
        var document = SubtitleDocument.Parse("1\n00:00:00,000 --> 00:00:01,000\n译文一\n\n2\n00:00:01,000 --> 00:00:02,000\n译文二\n");
        document.SetReference("1\n00:00:00,000 --> 00:00:02,000\n完整原句\n");
        Assert.All(document.Cues, cue => Assert.Equal("完整原句", cue.ReferenceText));
        document.SetReference("1\n00:00:10,000 --> 00:00:11,000\n无关原句\n");
        Assert.All(document.Cues, cue => Assert.Equal("未找到对应原文", cue.ReferenceText));
    }
}
