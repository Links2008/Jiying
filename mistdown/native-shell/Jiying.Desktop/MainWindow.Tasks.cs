using System.Collections.ObjectModel;
using System.Windows;
using System.Windows.Threading;

namespace Jiying.Desktop;

public partial class MainWindow
{
    internal TaskListViewModel TaskList { get; } = new();
    public ObservableCollection<TaskItem> QueueTasks => TaskList.QueueTasks;
    public ObservableCollection<TaskItem> DoneTasks => TaskList.DoneTasks;

    private const int TaskBatchLimit = 60;
    private readonly SemaphoreSlim _refreshLock = new(1, 1);
    internal bool RefreshingTasks => _refreshLock.CurrentCount == 0;
    private readonly HashSet<string> _changedTasks = new(StringComparer.Ordinal);
    private readonly Dictionary<string, TaskEvent> _progressUpdates = new(StringComparer.Ordinal);
    private DispatcherTimer? _taskUpdateTimer;
    private bool _fullTaskRefresh;
    private bool HasLoadedTasks => TaskList.HasLoaded;
    private long _taskConnection = -1;
    private long _snapshotConnection = -1;
    private long _streamConnection = -1;
    private long _eventSequence = -1;
    private bool _snapshotHasArtifacts;
    private bool _streamSequenced;
    private long _taskRetryAfter;

    internal async Task EventLoopAsync(CancellationToken token)
    {
        while (!token.IsCancellationRequested)
        {
            if (_engine.IsConnected)
            {
                var version = _engine.ConnectionVersion;
                try
                {
                    await _engine.ListenTaskEventsAsync(cursor =>
                    {
                        if (token.IsCancellationRequested || _engine.ConnectionVersion != version)
                            return Task.CompletedTask;
                        StartTaskConnection(version);
                        _streamConnection = version;
                        _streamSequenced = cursor.HasValue;
                        if (_snapshotConnection != version || cursor > _eventSequence) QueueRefresh();
                        if (cursor.HasValue) _eventSequence = Math.Max(_eventSequence, cursor.Value);
                        return Task.CompletedTask;
                    }, frame =>
                    {
                        if (!token.IsCancellationRequested && _engine.ConnectionVersion == version)
                            ReceiveTaskEvent(frame);
                        return Task.CompletedTask;
                    }, token);
                }
                catch (OperationCanceledException) when (token.IsCancellationRequested) { return; }
                catch (Exception error)
                {
                    DesktopDiagnostics.Write("任务事件连接", error);
                    if (_engine.ConnectionVersion == version && _streamConnection == version)
                    {
                        _streamConnection = -1;
                        QueueRefresh();
                    }
                }
            }
            try { await Task.Delay(1000, token); }
            catch (OperationCanceledException) when (token.IsCancellationRequested) { return; }
        }
    }

    private void StartTaskConnection(long version)
    {
        if (_taskConnection == version) return;
        _taskConnection = version;
        _eventSequence = -1;
        _snapshotHasArtifacts = false;
        _changedTasks.Clear();
        _progressUpdates.Clear();
        _taskRetryAfter = 0;
        _fullTaskRefresh = true;
    }

    internal void ReceiveTaskEvent(TaskEventFrame frame)
    {
        if (frame.Event is not { } item)
        {
            if (frame.Head is not { } head || head > _eventSequence) QueueRefresh();
            return;
        }
        if (item.Sequence > 0)
        {
            if (item.Sequence <= _eventSequence) return;
            if (_eventSequence >= 0 && item.Sequence != _eventSequence + 1) QueueRefresh();
            _eventSequence = item.Sequence;
        }
        if (string.IsNullOrWhiteSpace(item.TaskId)) return;
        if (item.Type is "task" or "stage")
        {
            // 阶段或任务切换后，旧阶段的待应用进度即告失效。
            _progressUpdates.Remove(item.TaskId);
            MarkTaskChanged(item.TaskId);
        }
        else if (item.Type == "progress")
        {
            if (!TaskList.Contains(item.TaskId)) _changedTasks.Add(item.TaskId);
            _progressUpdates[item.TaskId] = item;
            ScheduleTaskUpdate();
        }
    }

    private void QueueRefresh()
    {
        _fullTaskRefresh = true;
        ScheduleTaskUpdate();
    }

    private void MarkTaskChanged(string id)
    {
        if (!string.IsNullOrWhiteSpace(id)) _changedTasks.Add(id);
        ScheduleTaskUpdate();
    }

    private void ScheduleTaskUpdate()
    {
        if (_stop.IsCancellationRequested || !_engine.IsConnected ||
            _refreshLock.CurrentCount == 0 || Environment.TickCount64 < _taskRetryAfter) return;
        if (_taskUpdateTimer is null)
        {
            _taskUpdateTimer = new DispatcherTimer(DispatcherPriority.Background, Dispatcher)
            {
                Interval = TimeSpan.FromMilliseconds(110)
            };
            _taskUpdateTimer.Tick += TaskUpdateTick;
        }
        if (!_taskUpdateTimer.IsEnabled) _taskUpdateTimer.Start();
    }

    private void TaskUpdateTick(object? sender, EventArgs e)
    {
        _taskUpdateTimer?.Stop();
        _ = RunWindowOperationAsync("刷新下载列表", async () => { await FlushTaskChangesAsync(); });
    }

    internal Task<bool> RefreshTasksAsync()
    {
        _fullTaskRefresh = true;
        return FlushTaskChangesAsync();
    }

    private async Task<bool> FlushTaskChangesAsync()
    {
        if (!_engine.IsConnected || _stop.IsCancellationRequested) return false;
        try { await _refreshLock.WaitAsync(_windowToken); }
        catch (OperationCanceledException) when (_stop.IsCancellationRequested) { return false; }
        try
        {
            if (!_engine.IsConnected || _stop.IsCancellationRequested) return false;
            var version = _engine.ConnectionVersion;
            StartTaskConnection(version);
            var full = _fullTaskRefresh || _changedTasks.Count > TaskBatchLimit;
            var ids = _changedTasks.ToArray();
            if (full || ids.Length > 0)
            {
                _fullTaskRefresh = false;
                _changedTasks.Clear();
                var snapshot = await _engine.GetTaskSnapshotAsync(full ? null : ids, _windowToken);
                if (_stop.IsCancellationRequested || _engine.ConnectionVersion != version) return false;
                ApplyTaskSnapshot(snapshot, full ? null : ids);
                if (full)
                {
                    _snapshotConnection = version;
                    _snapshotHasArtifacts = snapshot.Sequence.HasValue;
                    if (snapshot.Sequence is long sequence) _eventSequence = Math.Max(_eventSequence, sequence);
                }
            }
            foreach (var (id, update) in _progressUpdates.ToArray())
            {
                if (_changedTasks.Contains(id)) continue;
                _progressUpdates.Remove(id);
                TaskList.ApplyProgress(id, update.ToProgress());
            }
            _taskRetryAfter = 0;
            UpdateTaskIndicators();
            return true;
        }
        catch (OperationCanceledException) when (_stop.IsCancellationRequested) { return false; }
        catch (Exception error)
        {
            DesktopDiagnostics.Write("读取下载列表", error);
            _fullTaskRefresh = true;
            _taskRetryAfter = Environment.TickCount64 + 4000;
            return false;
        }
        finally
        {
            _refreshLock.Release();
            if (_fullTaskRefresh || _changedTasks.Count > 0 || _progressUpdates.Count > 0)
                ScheduleTaskUpdate();
        }
    }

    private void ApplyTaskSnapshot(TaskSnapshot snapshot, string[]? ids)
    {
        if (snapshot.Sequence is long sequence)
            foreach (var id in (ids ?? _progressUpdates.Keys.ToArray()))
                if (_progressUpdates.TryGetValue(id, out var progress) && progress.Sequence <= sequence)
                    _progressUpdates.Remove(id);
        TaskList.Apply(snapshot, ids, _changedTasks, task => { if (_ui.Notify) NotifyCompleted(task); });
    }

    private void UpdateTaskIndicators()
    {
        var queued = QueueTasks.Count;
        var done = DoneTasks.Count;
        QueueEmpty.Visibility = queued == 0 ? Visibility.Visible : Visibility.Collapsed;
        QueueList.Visibility = queued == 0 ? Visibility.Collapsed : Visibility.Visible;
        DoneEmpty.Visibility = done == 0 ? Visibility.Visible : Visibility.Collapsed;
        QueueBadge.Visibility = queued == 0 ? Visibility.Collapsed : Visibility.Visible;
        DoneBadge.Visibility = done == 0 ? Visibility.Collapsed : Visibility.Visible;
        QueueBadgeText.Text = queued.ToString();
        DoneBadgeText.Text = done.ToString();
        QueueCountText.Text = $"进行中 {TaskList.RunningCount} · 共 {queued} 个任务";
        DoneCountText.Text = $"共 {done} 条记录";
        var speed = TaskList.DownloadSpeed;
        NetSpeedText.Text = speed >= 1048576 ? $"{speed / 1048576d:0.0} MB/s" : $"{speed / 1024d:0} KB/s";
    }

}
