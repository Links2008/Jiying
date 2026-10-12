using System.Threading;
using System.Windows;

namespace Jiying.Desktop;

public partial class App : Application
{
    private const string MutexName = @"Local\Jiying.NativeShell.Mutex";
    private const string ActivateName = @"Local\Jiying.NativeShell.Activate";
    private Mutex? _mutex;
    private EventWaitHandle? _activate;
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

        _mutex = new Mutex(true, MutexName, out var firstInstance);
        if (!firstInstance)
        {
            SignalExistingInstance();
            Shutdown();
            return;
        }

        _activate = new EventWaitHandle(false, EventResetMode.AutoReset, ActivateName);
        _listenerStop = new CancellationTokenSource();
        _ = Task.Run(() => ListenForActivation(_listenerStop.Token));

        MainWindow = new MainWindow();
        MainWindow.Show();
    }

    private static void SignalExistingInstance()
    {
        for (var i = 0; i < 10; i++)
        {
            try
            {
                EventWaitHandle.OpenExisting(ActivateName).Set();
                return;
            }
            catch (WaitHandleCannotBeOpenedException) { Thread.Sleep(100); }
        }
    }

    private void ListenForActivation(CancellationToken token)
    {
        while (!token.IsCancellationRequested && _activate is not null)
        {
            if (!_activate.WaitOne(500)) continue;
            Dispatcher.Invoke(() =>
            {
                if (MainWindow is null) return;
                if (MainWindow.WindowState == WindowState.Minimized)
                    SystemCommands.RestoreWindow(MainWindow);
                MainWindow.Show();
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
        _mutex?.Dispose();
        base.OnExit(e);
    }
}
