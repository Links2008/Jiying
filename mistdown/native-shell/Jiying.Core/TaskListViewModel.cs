using System.Collections.ObjectModel;

namespace Jiying.Desktop;

internal sealed class TaskListViewModel
{
    private readonly Dictionary<string, TaskItem> _taskCards = new(StringComparer.Ordinal);
    public ObservableCollection<TaskItem> QueueTasks { get; } = [];
    public ObservableCollection<TaskItem> DoneTasks { get; } = [];
    public bool HasLoaded { get; private set; }
    public bool Contains(string id) => _taskCards.ContainsKey(id);
    public void Clear()
    {
        HasLoaded = false;
        _taskCards.Clear();
        QueueTasks.Clear();
        DoneTasks.Clear();
    }
    public int RunningCount => QueueTasks.Count(task => task.Status == "running");
    public double DownloadSpeed => QueueTasks.Where(task => task.Status == "running" && task.Progress.Stage == "download" &&
            (task.Stages.Count == 0 || task.Stages.Any(stage => stage.Stage == "download" && stage.Status == "running")))
        .Sum(task => task.Progress.SpeedBps);
    public void ApplyProgress(string id, TaskProgress progress)
    {
        if (_taskCards.TryGetValue(id, out var task)) task.ApplyProgress(progress);
    }

    public void Apply(TaskSnapshot snapshot, string[]? ids, IReadOnlySet<string> changed, Action<TaskItem> completed)
    {
        var incoming = snapshot.Items.ToDictionary(item => item.Id, StringComparer.Ordinal);
        if (ids is null)
        {
            var visible = new List<TaskItem>(incoming.Count);
            foreach (var item in snapshot.Items)
            {
                if (!changed.Contains(item.Id)) visible.Add(UpdateTaskCard(item, completed));
                else if (_taskCards.TryGetValue(item.Id, out var current)) visible.Add(current);
            }
            foreach (var id in _taskCards.Keys.Where(id => !incoming.ContainsKey(id)).ToArray())
            {
                if (changed.Contains(id)) visible.Add(_taskCards[id]);
                else _taskCards.Remove(id);
            }
            var ordered = visible.OrderByDescending(item => item.CreatedAt, StringComparer.Ordinal)
                .ThenBy(item => item.Id, StringComparer.Ordinal).ToArray();
            SyncTasks(QueueTasks, ordered.Where(item => !item.IsTerminal).ToArray());
            SyncTasks(DoneTasks, ordered.Where(item => item.IsTerminal).ToArray());
            HasLoaded = true;
            return;
        }

        foreach (var id in ids)
        {
            if (changed.Contains(id)) continue;
            _taskCards.TryGetValue(id, out var previous);
            var oldList = previous?.IsTerminal == true ? DoneTasks : QueueTasks;
            if (!incoming.TryGetValue(id, out var item))
            {
                if (previous is not null) oldList.Remove(previous);
                _taskCards.Remove(id);
                continue;
            }
            var task = UpdateTaskCard(item, completed);
            var list = task.IsTerminal ? DoneTasks : QueueTasks;
            if (previous is null || oldList != list)
            {
                if (previous is not null) oldList.Remove(previous);
                InsertTask(list, task);
            }
        }
    }

    private TaskItem UpdateTaskCard(TaskItem item, Action<TaskItem> completed)
    {
        foreach (var artifact in item.Artifacts) artifact.TaskId = item.Id;
        _taskCards.TryGetValue(item.Id, out var task);
        var notify = HasLoaded && item.Status == "completed" && task?.Status != "completed";
        if (task is null) _taskCards[item.Id] = task = item;
        else task.RefreshFrom(item);
        if (notify) completed(task);
        return task;
    }

    private static void InsertTask(ObservableCollection<TaskItem> items, TaskItem task)
    {
        var first = 0;
        var last = items.Count;
        while (first < last)
        {
            var middle = (first + last) / 2;
            var comparison = string.CompareOrdinal(task.CreatedAt, items[middle].CreatedAt);
            if (comparison > 0 || comparison == 0 && string.CompareOrdinal(task.Id, items[middle].Id) < 0)
                last = middle;
            else
                first = middle + 1;
        }
        items.Insert(first, task);
    }

    internal static void SyncTasks(ObservableCollection<TaskItem> target, IReadOnlyList<TaskItem> items)
    {
        var ids = items.Select(item => item.Id).ToHashSet(StringComparer.Ordinal);
        for (var index = target.Count - 1; index >= 0; index--)
            if (!ids.Contains(target[index].Id)) target.RemoveAt(index);

        var existing = target.ToDictionary(item => item.Id, StringComparer.Ordinal);
        for (var index = 0; index < items.Count; index++)
        {
            var item = items[index];
            if (!existing.TryGetValue(item.Id, out var current)) target.Insert(index, item);
            else
            {
                if (!ReferenceEquals(target[index], current))
                    target.Move(target.IndexOf(current), index);
                if (!ReferenceEquals(current, item)) current.RefreshFrom(item);
            }
        }
    }
}
