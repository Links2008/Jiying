// 使用隔离窗口与自生成历史记录测量导航，不读取安装中的配置或任务。
using System.Diagnostics;
using System.Reflection;
using System.Text.Json;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Media;
using Jiying.Desktop;

internal static class NavigationRegression
{
    internal static async Task Run(MainWindow window, Action<bool, string> check)
    {
        const BindingFlags flags = BindingFlags.Instance | BindingFlags.NonPublic;
        T Field<T>(string name) => (T)typeof(MainWindow).GetField(name, flags)!.GetValue(window)!;
        void Show(string name) => typeof(MainWindow).GetMethod("ShowView", flags)!.Invoke(window, [name]);
        FrameworkElement View(string name) => Field<FrameworkElement>(name switch
        { "settings" => "SettingsView", "done" => "DoneView", "queue" => "QueueView", _ => "ParseView" });
        window.Width = 1160;
        window.Height = 720;
        for (var i = 0; i < 1500; i++)
            window.DoneTasks.Add(new TaskItem { Id = "nav-" + i, Title = "导航测试视频 " + i,
                                               Status = "completed", CreatedAt = "2026-10-14T12:00:00" });
        Field<FrameworkElement>("DoneList").Visibility = Visibility.Visible;
        Field<FrameworkElement>("DoneEmpty").Visibility = Visibility.Collapsed;
        Show("parse");
        await Task.Delay(350);
        var results = new List<object>();
        foreach (var name in new[] { "settings", "done", "parse", "queue", "settings", "done", "parse" })
        {
            var timer = Stopwatch.StartNew();
            Show(name);
            var handlerMs = timer.Elapsed.TotalMilliseconds;
            var next = View(name);
            var immediate = next.Visibility == Visibility.Visible && next.IsHitTestVisible;
            check(immediate, "点击后目标页立即可操作：" + name);
            while (timer.ElapsedMilliseconds < 1500 &&
                   (next.Visibility != Visibility.Visible || !next.IsHitTestVisible || next.Opacity < .8))
                await Task.Delay(5);
            window.UpdateLayout();
            results.Add(new { view = name, immediate, handler_ms = handlerMs,
                              usable_ms = timer.Elapsed.TotalMilliseconds });
            await Task.Delay(320);
        }
        foreach (var name in new[] { "settings", "done", "parse", "queue", "done", "settings" })
        { Show(name); await Task.Delay(8); }
        check(View("settings").Visibility == Visibility.Visible && View("settings").IsHitTestVisible,
              "快速连续切换立即采用最后点击的页面");
        await Task.Delay(350);
        check(new[] { "settings", "done", "queue", "parse" }.Count(n => View(n).Visibility == Visibility.Visible) == 1,
              "过期动画不会恢复旧页或同时显示两页");
        Show("done");
        await Task.Delay(350);
        window.UpdateLayout();
        var list = Field<ListView>("DoneList");
        var rows = Descendants(list).OfType<ListViewItem>().Count();
        check(list.Items.Count == 1500 && rows > 0 && rows < 100,
              "1500条历史记录只创建当前可见的任务卡片");
        var path = Environment.GetEnvironmentVariable("JIYING_NAVIGATION_REPORT");
        if (!string.IsNullOrWhiteSpace(path))
            System.IO.File.WriteAllText(path, JsonSerializer.Serialize(new { results, realized_rows = rows,
                history_count = list.Items.Count }, new JsonSerializerOptions { WriteIndented = true }));
    }

    private static IEnumerable<DependencyObject> Descendants(DependencyObject parent)
    {
        for (var i = 0; i < VisualTreeHelper.GetChildrenCount(parent); i++)
        {
            var child = VisualTreeHelper.GetChild(parent, i);
            yield return child;
            foreach (var nested in Descendants(child)) yield return nested;
        }
    }
}
