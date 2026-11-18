using Microsoft.Win32;
using System.Windows.Controls;
using System.IO;
using System.Windows;

namespace Jiying.Desktop;

public partial class MainWindow
{
    private SubtitleDocument? _subtitleDocument;

    internal void LoadSubtitleEditor(ArtifactContent content)
    {
        _subtitleDocument = null;
        SubtitleTextBox.Text = content.Text;
        SubtitleRows.ItemsSource = null;
        SubtitleRows.Visibility = Visibility.Collapsed;
        SubtitleTextBox.Visibility = Visibility.Visible;
        if (_editingArtifact?.Kind == "person_notes")
        {
            SubtitleEditorDescription.Text = "编辑 ASS 可调整人名、中英文及时间轴。保存后可重新生成视频。";
            return;
        }
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

    private ArtifactItem? _editingArtifact;
    private string _subtitleSavedText = "";
    private bool _subtitleBusy;
    internal bool SubtitleBusy => _subtitleBusy;
    internal ArtifactItem? EditingSubtitle { get => _editingArtifact; set => _editingArtifact = value; }
    internal string SavedSubtitleText { get => _subtitleSavedText; set => _subtitleSavedText = value; }
    private int _subtitleOperationSequence;
    private CancellationTokenSource? _subtitleLoadStop;
    private int _subtitleLoadSequence;

    internal void Artifact_Click(object sender, RoutedEventArgs e) =>
        _ = RunWindowOperationAsync("打开下载产物", () => OpenArtifactAsync(sender));

    private async Task OpenArtifactAsync(object sender)
    {
        if (sender is not Button { Tag: ArtifactItem artifact }) return;
        if (_subtitleBusy) return;
        if (!ConfirmDiscardSubtitleChanges()) return;
        CancelSubtitleLoad();
        if (artifact.IsSubtitle)
        {
            var sequence = _subtitleLoadSequence;
            using var loadStop = CancellationTokenSource.CreateLinkedTokenSource(_windowToken);
            _subtitleLoadStop = loadStop;
            try
            {
                var content = await _engine.GetArtifactContentAsync(artifact.Id, loadStop.Token);
                // 连续选择字幕时，已取消或较早的响应不能替换当前编辑对象与文本。
                if (sequence != _subtitleLoadSequence || loadStop.IsCancellationRequested || _subtitleBusy) return;
                _editingArtifact = artifact;
                SubtitleDialogTitle.Text = $"修改字幕 · {artifact.DisplayText}";
                LoadSubtitleEditor(content);
                _subtitleSavedText = content.Text;
                SetSubtitleBusy(false, "修改后选择“保存并重新生成视频”，进度会显示在传输队列中。");
                OpenModal(SubtitleDialog);
                if (_subtitleDocument is null) SubtitleTextBox.Focus();
                else SubtitleRows.Focus();
            }
            catch (OperationCanceledException) when (loadStop.IsCancellationRequested) { }
            catch (Exception ex) { if (sequence == _subtitleLoadSequence) ShowError(ex.Message); }
            finally { if (ReferenceEquals(_subtitleLoadStop, loadStop)) _subtitleLoadStop = null; }
            return;
        }

        var dialog = new SaveFileDialog
        {
            FileName = string.IsNullOrWhiteSpace(artifact.FileName) ? $"artifact-{artifact.Id}" : artifact.FileName,
            Filter = "所有文件|*.*"
        };
        if (dialog.ShowDialog(this) != true) return;
        try
        {
            await _engine.SaveArtifactAsync(artifact.Id, dialog.FileName, _windowToken);
            ShowToast("文件已保存", dialog.FileName);
        }
        catch (OperationCanceledException) when (_stop.IsCancellationRequested) { }
        catch (Exception ex) { ShowError(ex.Message); }
    }

    private void SaveSubtitle_Click(object sender, RoutedEventArgs e) =>
        _ = RunWindowOperationAsync("保存字幕", SaveSubtitleFromEditorAsync);

    private async Task SaveSubtitleFromEditorAsync()
    {
        if (_subtitleBusy || _editingArtifact is null) return;
        var operation = ++_subtitleOperationSequence;
        SetSubtitleBusy(true, "正在保存字幕…");
        try
        {
            if (await SaveSubtitleAsync()) ShowToast("字幕已保存", "可继续修改，或重新生成带字幕视频");
        }
        finally { if (operation == _subtitleOperationSequence) SetSubtitleBusy(false); }
    }

    internal void SetSubtitleBusy(bool busy, string? message = null)
    {
        _subtitleBusy = busy;
        SubtitleTextBox.IsReadOnly = busy;
        SubtitleRows.IsEnabled = !busy;
        SubtitleSaveButton.IsEnabled = SubtitleRenderButton.IsEnabled = SubtitleCloseButton.IsEnabled = !busy;
        if (message is not null) SubtitleHint.Text = message;
    }

    internal async Task<bool> SaveSubtitleAsync()
    {
        var artifact = _editingArtifact;
        if (artifact is null) return false;
        var text = SubtitleTextBox.Text;
        try
        {
            if (_subtitleDocument is not null) _subtitleDocument.Serialize(true);
            await _engine.SaveArtifactContentAsync(artifact.Id, text, _windowToken);
            _subtitleSavedText = text;
            MarkTaskChanged(artifact.TaskId);
            SubtitleHint.Text = "字幕已保存；可继续修改，或重新生成视频。";
            return true;
        }
        catch (OperationCanceledException) when (_stop.IsCancellationRequested) { return false; }
        catch (Exception ex)
        {
            SubtitleHint.Text = $"保存失败：{ex.Message}";
            ShowError(ex.Message);
            return false;
        }
    }

    internal void RerenderSubtitle_Click(object sender, RoutedEventArgs e) =>
        _ = RunWindowOperationAsync("重新生成视频", RerenderSubtitleAsync);

    private async Task RerenderSubtitleAsync()
    {
        if (_editingArtifact is null || _subtitleBusy) return;
        var operation = ++_subtitleOperationSequence;
        var artifact = _editingArtifact;
        var submitted = false;
        SetSubtitleBusy(true, "正在保存字幕…");
        try
        {
            if (!await SaveSubtitleAsync()) return;
            SubtitleHint.Text = "字幕已保存，正在提交视频重生成…";
            await _engine.RerenderAsync(artifact.TaskId, artifact.Id, artifact.Language, _windowToken);
            submitted = true;
            MarkTaskChanged(artifact.TaskId);
            SetSubtitleBusy(false);
            CloseModal();
            ShowToast("已开始重新生成视频", "使用已保存的字幕，进度会在传输队列中更新");
            ShowView("queue");
            await FlushTaskChangesAsync();
        }
        catch (OperationCanceledException) when (_stop.IsCancellationRequested) { }
        catch (Exception ex)
        {
            if (operation == _subtitleOperationSequence && _editingArtifact?.Id == artifact.Id &&
                _editingArtifact.TaskId == artifact.TaskId)
                SubtitleHint.Text = submitted ? "视频已提交，进度刷新失败。" : $"字幕已保存，视频重生成未启动：{ex.Message}";
            ShowError(ex.Message);
        }
        finally { if (operation == _subtitleOperationSequence) SetSubtitleBusy(false); }
    }

    private bool ConfirmDiscardSubtitleChanges()
    {
        if (SubtitleDialog.Visibility != Visibility.Visible || _editingArtifact is null ||
            SubtitleTextBox.Text == _subtitleSavedText) return true;
        var choice = MessageBox.Show(this, "字幕还有未保存的修改。要丢弃修改并关闭吗？", "未保存的字幕",
            MessageBoxButton.YesNo, MessageBoxImage.Warning, MessageBoxResult.No);
        if (choice != MessageBoxResult.Yes) return false;
        SubtitleTextBox.Text = _subtitleSavedText;
        _subtitleDocument = null;
        return true;
    }

    private void CancelSubtitleLoad()
    {
        _subtitleLoadSequence++;
        _subtitleLoadStop?.Cancel();
    }
}
