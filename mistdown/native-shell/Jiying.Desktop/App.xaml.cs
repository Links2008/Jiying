using System.Threading;
using System.Windows;
using System.IO;
using System.Text.Json;
using Windows.UI.Notifications;

namespace Jiying.Desktop;

public partial class App : Application
{
    private const string MutexName = @"Local\Jiying.NativeShell.Mutex";
    private const string ActivateName = @"Local\Jiying.NativeShell.Activate";
    private const string CompletedActivateName = @"Local\Jiying.NativeShell.Completed";
    private Mutex? _mutex;
    private EventWaitHandle? _activate;
    private EventWaitHandle? _activateCompleted;
    private CancellationTokenSource? _listenerStop;

    protected override void OnStartup(StartupEventArgs e)
    {
        base.OnStartup(e);
        if (e.Args.Contains("--self-test", StringComparer.OrdinalIgnoreCase))
        {
            try { SelfCheck.Run(); Shutdown(0); }
            catch { Shutdown(1); }
            return;
        }
        if (e.Args.Length == 3 && e.Args[0] == "--completion-notify")
        {
            if (!UiSettings.NotificationsEnabled()) { Shutdown(0); return; }
            try
            {
                var setting = WindowsNotifications.SendCompleted(e.Args[1], e.Args[2]);
                WindowsNotifications.RecordStatus(setting == NotificationSetting.Enabled
                    ? "软件内提示与 Windows 系统通知" : "Windows 已关闭此应用的系统通知；软件内提醒仍可用");
                Shutdown(0);
            }
            catch (Exception error)
            {
                WindowsNotifications.RecordStatus("系统通知未送达：" + error.Message);
                Shutdown(1);
            }
            return;
        }
        if (e.Args.Length == 2 && e.Args[0] == "--notification-test")
        {
            try
            {
                var setting = WindowsNotifications.SendCompleted("jiying-notification-probe", "即影通知功能测试（测试消息）");
                var recorded = false;
                for (var attempt = 0; attempt < 20 && !recorded && setting == NotificationSetting.Enabled; attempt++)
                {
                    recorded = ToastNotificationManager.History.GetHistory(WindowsNotifications.AppId)
                        .Any(toast => toast.Group == "completed" && toast.Content.GetXml().Contains("即影通知功能测试"));
                    if (!recorded) Thread.Sleep(200);
                }
                File.WriteAllText(e.Args[1], JsonSerializer.Serialize(new { setting = setting.ToString(), recorded }));
                Shutdown(recorded ? 0 : 1);
            }
            catch (Exception error)
            {
                File.WriteAllText(e.Args[1], JsonSerializer.Serialize(new { error = error.ToString(), hresult = error.HResult, type = error.GetType().FullName }));
                Shutdown(1);
            }
            return;
        }
        var showCompleted = e.Args.Contains("--show-completed", StringComparer.OrdinalIgnoreCase);

        _mutex = new Mutex(true, MutexName, out var firstInstance);
        if (!firstInstance)
        {
            SignalExistingInstance(showCompleted);
            Shutdown();
            return;
        }

        _activate = new EventWaitHandle(false, EventResetMode.AutoReset, ActivateName);
        _activateCompleted = new EventWaitHandle(false, EventResetMode.AutoReset, CompletedActivateName);
        _listenerStop = new CancellationTokenSource();
        _ = Task.Run(() => ListenForActivation(_listenerStop.Token));

        MainWindow = new MainWindow();
        MainWindow.Show();
        if (showCompleted) ((MainWindow)MainWindow).ShowCompleted();
    }

    private static void SignalExistingInstance(bool showCompleted)
    {
        for (var i = 0; i < 10; i++)
        {
            try
            {
                using var signal = EventWaitHandle.OpenExisting(showCompleted ? CompletedActivateName : ActivateName);
                signal.Set();
                return;
            }
            catch (WaitHandleCannotBeOpenedException) { Thread.Sleep(100); }
        }
    }

    private void ListenForActivation(CancellationToken token)
    {
        while (!token.IsCancellationRequested && _activate is not null && _activateCompleted is not null)
        {
            var signal = WaitHandle.WaitAny([_activate, _activateCompleted], 500);
            if (signal == WaitHandle.WaitTimeout) continue;
            Dispatcher.Invoke(() =>
            {
                if (MainWindow is null) return;
                if (MainWindow.WindowState == WindowState.Minimized)
                    SystemCommands.RestoreWindow(MainWindow);
                MainWindow.Show();
                if (signal == 1) ((MainWindow)MainWindow).ShowCompleted();
                MainWindow.Activate();
                MainWindow.Topmost = true;
                MainWindow.Topmost = false;
            });
        }
    }

    protected override void OnExit(ExitEventArgs e)
    {
        _listenerStop?.Cancel();
        _activate?.Dispose();
        _activateCompleted?.Dispose();
        _mutex?.Dispose();
        base.OnExit(e);
    }
}
