using System.Diagnostics;
using System.Windows.Threading;

internal static class UiTiming
{
    internal static async Task<bool> Until(Func<bool> condition, int milliseconds = 1200)
    {
        var elapsed = Stopwatch.StartNew();
        do
        {
            // 在布局和渲染优先级之后采样，由具体状态决定完成。
            await Dispatcher.Yield(DispatcherPriority.Background);
            if (condition()) return true;
            await Task.Delay(16);
        } while (elapsed.ElapsedMilliseconds < milliseconds);
        return false;
    }
}
