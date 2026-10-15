using System.Windows;

namespace Jiying.Desktop;

public partial class MainWindow
{
    private SubtitleDocument? _subtitleDocument;

    private void LoadSubtitleEditor(ArtifactContent content)
    {
        _subtitleDocument = null;
        SubtitleTextBox.Text = content.Text;
        SubtitleRows.ItemsSource = null;
        SubtitleRows.Visibility = Visibility.Collapsed;
        SubtitleTextBox.Visibility = Visibility.Visible;
        SubtitleEditorDescription.Text = "此文件格式需要修复，原稿已完整保留；请修正序号和时间轴后保存。";
        try
        {
            var document = SubtitleDocument.Parse(content.Text);
            try { document.SetReference(content.SourceText); }
            catch (FormatException) { }
            _subtitleDocument = document;
            foreach (var cue in document.Cues)
                cue.Changed = () =>
                {
                    if (ReferenceEquals(_subtitleDocument, document)) SubtitleTextBox.Text = document.Serialize();
                };
            SubtitleReferenceColumn.Visibility = content.IsSource ? Visibility.Collapsed : Visibility.Visible;
            SubtitleEditColumn.Header = content.IsSource ? "原文 · 点击修改" : "当前字幕 · 点击修改";
            SubtitleRows.ItemsSource = document.Cues;
            SubtitleRows.Visibility = Visibility.Visible;
            SubtitleTextBox.Visibility = Visibility.Collapsed;
            SubtitleEditorDescription.Text = $"共 {document.Cues.Count} 条 · 直接点击文字修改，时间轴自动保留。" +
                (content.IsSource ? "当前编辑原文。" : "左侧原文供对照；修改其他语言可打开对应字幕。");
        }
        catch (FormatException) { }
    }
}
