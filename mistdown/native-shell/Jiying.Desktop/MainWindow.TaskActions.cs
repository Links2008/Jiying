using System.Windows;
using System.Windows.Controls;

namespace Jiying.Desktop;

public partial class MainWindow
{
    private int _deleteOperationSequence;
    private TaskItem[] _deleteTasks = [];
    private bool _deleteBatch;

    private void Cancel_Click(object sender, RoutedEventArgs e) =>
        _ = RunWindowOperationAsync("取消下载", () => RunTaskAction(sender, _engine.CancelAsync, "已发送取消请求"));

    private void Retry_Click(object sender, RoutedEventArgs e) =>
        _ = RunWindowOperationAsync("重试下载", () => RunTaskAction(sender, _engine.RetryAsync, "任务已重新开始"));

    private void Reveal_Click(object sender, RoutedEventArgs e) =>
        _ = RunWindowOperationAsync("打开视频目录", () => RunTaskAction(sender, _engine.RevealAsync, "已打开输出目录", false));

    private async Task RunTaskAction(object sender, Func<string, CancellationToken, Task> action, string success, bool refresh = true)
    {
        if (sender is not Button { Tag: string id }) return;
        if (!((Button)sender).IsEnabled) return;
        var button = (Button)sender;
        button.IsEnabled = false;
        ClearError();
        try
        {
            await action(id, _windowToken);
            ShowToast(success);
            if (refresh)
            {
                MarkTaskChanged(id);
                await FlushTaskChangesAsync();
            }
        }
        catch (OperationCanceledException) when (_stop.IsCancellationRequested) { }
        catch (Exception ex) { ShowError(ex.Message); }
        finally { button.IsEnabled = true; }
    }

    private void DeleteOne_Click(object sender, RoutedEventArgs e) =>
        _ = RunWindowOperationAsync("打开删除确认", () => OpenDeleteTaskAsync(sender));

    private async Task OpenDeleteTaskAsync(object sender)
    {
        if (sender is not Button { Tag: TaskItem task }) return;
        if (task.Artifacts.Count == 0 && !_snapshotHasArtifacts)
        {
            try { task = await _engine.GetTaskAsync(task.Id, _windowToken); }
            catch { }
        }
        if (_stop.IsCancellationRequested) return;
        ShowDeleteDialog([task], false);
    }

    private void ClearDone_Click(object sender, RoutedEventArgs e)
    {
        if (DoneTasks.Count == 0) { ShowToast("没有可清理的记录"); return; }
        ShowDeleteDialog(DoneTasks.ToArray(), true);
    }

    internal void ShowDeleteDialog(TaskItem[] tasks, bool batch)
    {
        _deleteTasks = tasks;
        _deleteBatch = batch;
        DeleteDialogTitle.Text = batch ? "清理已完成内容" : "删除下载内容";
        DeleteDescription.Text = batch
            ? $"选择清理范围 · 共 {tasks.Length} 条已完成记录"
            : $"“{tasks[0].DisplayTitle}”";
        DeleteAllChoice.IsChecked = true;
        DeleteChoice_Changed(DeleteAllChoice, new RoutedEventArgs());
        OpenModal(DeleteDialog);
        if (DeleteDialog.Visibility == Visibility.Visible) ConfirmDeleteButton.IsEnabled = true;
    }

    private void DeleteChoice_Changed(object sender, RoutedEventArgs e)
    {
        if (DeleteHint is null || ConfirmDeleteText is null) return;
        var scope = DeleteSubtitleChoice?.IsChecked == true ? "subtitle"
            : DeleteVideoChoice?.IsChecked == true ? "video" : "all";
        DeleteHint.Text = scope switch
        {
            "subtitle" => "保留视频和记录",
            "video" => "保留字幕和记录",
            _ => "同时删除文件和记录"
        };
        ConfirmDeleteText.Text = scope == "all" ? "删除全部" : "删除所选";
    }

    internal void ConfirmDelete_Click(object sender, RoutedEventArgs e) =>
        _ = RunWindowOperationAsync("删除任务", ConfirmDeleteAsync);

    private async Task ConfirmDeleteAsync()
    {
        if (!ConfirmDeleteButton.IsEnabled) return;
        var operation = ++_deleteOperationSequence;
        var tasks = _deleteTasks.ToArray();
        var batch = _deleteBatch;
        var modalSequence = _modalSequence;
        ConfirmDeleteButton.IsEnabled = false;
        try
        {
            var scope = DeleteSubtitleChoice.IsChecked == true ? "subtitle"
                : DeleteVideoChoice.IsChecked == true ? "video" : "all";
            DeleteResult result;
            if (batch) result = await _engine.ClearDoneAsync(scope, _windowToken, tasks.Select(task => task.Id).ToArray());
            else result = scope == "all"
                ? await _engine.DeleteTaskAsync(tasks[0].Id, _windowToken)
                : await _engine.DeleteTaskArtifactsAsync(tasks[0].Id, scope, _windowToken);
            foreach (var task in tasks) MarkTaskChanged(task.Id);
            // 删除进行中允许关闭弹窗，晚到结果不能关闭随后打开的另一份确认。
            if (modalSequence == _modalSequence && DeleteDialog.Visibility == Visibility.Visible) CloseModal();
            ShowToast("删除完成", $"已删除 {result.DeletedFiles.Count} 个文件" + (result.Skipped.Count > 0 ? $"，跳过 {result.Skipped.Count} 个" : ""));
            await FlushTaskChangesAsync();
        }
        catch (OperationCanceledException) when (_stop.IsCancellationRequested) { }
        catch (Exception ex) { ShowError(ex.Message); }
        finally
        {
            if (operation == _deleteOperationSequence && modalSequence == _modalSequence &&
                DeleteDialog.Visibility == Visibility.Visible) ConfirmDeleteButton.IsEnabled = true;
        }
    }

    private void Refresh_Click(object sender, RoutedEventArgs e) =>
        _ = RunWindowOperationAsync("手动刷新任务", async () =>
        {
            if (await RefreshTasksAsync()) ShowToast("已刷新任务列表");
            else if (!_stop.IsCancellationRequested) ShowError("下载列表暂时无法刷新，请稍后重试。");
        });
}
