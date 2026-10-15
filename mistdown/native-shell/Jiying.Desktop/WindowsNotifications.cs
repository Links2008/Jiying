using System.IO;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Text;
using System.Xml.Linq;
using Microsoft.Win32;
using Windows.Data.Xml.Dom;
using Windows.UI.Notifications;

namespace Jiying.Desktop;

internal static class WindowsNotifications
{
    internal const string AppId = "Jiying.Desktop.Repaired";
    private static bool _registered;

    internal static void Register()
    {
        if (_registered) return;
        var executable = Environment.ProcessPath ?? throw new InvalidOperationException("无法确定即影安装位置");
        if (!Path.GetFileName(executable).Equals("Jiying.Native.exe", StringComparison.OrdinalIgnoreCase))
            throw new InvalidOperationException("隔离回归宿主不注册系统通知");
        Marshal.ThrowExceptionForHR(SetCurrentProcessExplicitAppUserModelID(AppId));
        var shortcutPath = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.Programs), "即影.lnk");
        dynamic shell = Activator.CreateInstance(Type.GetTypeFromProgID("WScript.Shell")!)!;
        try
        {
            dynamic shortcut = shell.CreateShortcut(shortcutPath);
            try
            {
                shortcut.TargetPath = executable;
                shortcut.WorkingDirectory = AppContext.BaseDirectory;
                shortcut.IconLocation = executable + ",0";
                shortcut.Save();
            }
            finally { Marshal.FinalReleaseComObject(shortcut); }
        }
        finally { Marshal.FinalReleaseComObject(shell); }
        var iid = typeof(IPropertyStore).GUID;
        Marshal.ThrowExceptionForHR(SHGetPropertyStoreFromParsingName(shortcutPath, 0, 2, ref iid, out var properties));
        var key = new PropertyKey { FormatId = new("9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3"), Id = 5 };
        var value = new PropVariant { Type = 31, Pointer = Marshal.StringToCoTaskMemUni(AppId) };
        try
        {
            Marshal.ThrowExceptionForHR(properties.SetValue(ref key, ref value));
            Marshal.ThrowExceptionForHR(properties.Commit());
        }
        finally
        {
            Marshal.FreeCoTaskMem(value.Pointer);
            Marshal.FinalReleaseComObject(properties);
        }
        using var protocol = Registry.CurrentUser.CreateSubKey(@"Software\Classes\jiying-repaired");
        protocol.SetValue("", "URL:即影完成通知");
        protocol.SetValue("URL Protocol", "");
        using (var command = protocol.CreateSubKey(@"shell\open\command"))
            command.SetValue("", $"\"{executable}\" --show-completed");
        using var identity = Registry.CurrentUser.CreateSubKey(@"Software\Classes\AppUserModelId\" + AppId);
        identity.SetValue("DisplayName", "即影");
        identity.SetValue("IconUri", executable);
        _registered = true;
    }

    internal static string Payload(string title, string detail) => new XElement("toast",
        new XAttribute("activationType", "protocol"), new XAttribute("launch", "jiying-repaired://completed"),
        new XElement("visual", new XElement("binding", new XAttribute("template", "ToastGeneric"),
            new XElement("text", title), new XElement("text", detail)))).ToString(SaveOptions.DisableFormatting);

    internal static NotificationSetting SendCompleted(string taskId, string title)
    {
        Register();
        var notifier = ToastNotificationManager.CreateToastNotifier(AppId);
        // 首次注册的桌面身份可能尚无通知设置记录；先发送，让 Windows 建立记录。
        try { if (notifier.Setting != NotificationSetting.Enabled) return notifier.Setting; }
        catch (COMException error) when (error.HResult == unchecked((int)0x80070490)) { }
        var document = new XmlDocument();
        document.LoadXml(Payload("下载已完成", title));
        notifier.Show(new ToastNotification(document)
        {
            Tag = Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(taskId)))[..16],
            Group = "completed", ExpirationTime = DateTimeOffset.Now.AddDays(2)
        });
        return notifier.Setting;
    }

    internal static void RecordStatus(string message)
    {
        try
        {
            Directory.CreateDirectory(Path.GetDirectoryName(UiSettings.NotificationStatusPath)!);
            File.WriteAllText(UiSettings.NotificationStatusPath,
                System.Text.Json.JsonSerializer.Serialize(new { message, time = DateTimeOffset.Now }));
        }
        catch (Exception) { }
    }

    [DllImport("shell32.dll", CharSet = CharSet.Unicode)]
    private static extern int SetCurrentProcessExplicitAppUserModelID(string appId);
    [DllImport("shell32.dll", CharSet = CharSet.Unicode)]
    private static extern int SHGetPropertyStoreFromParsingName(string path, nint context, uint flags, ref Guid iid,
        [MarshalAs(UnmanagedType.Interface)] out IPropertyStore properties);

    [StructLayout(LayoutKind.Sequential)]
    private struct PropertyKey { internal Guid FormatId; internal uint Id; }
    [StructLayout(LayoutKind.Explicit, Size = 24)]
    private struct PropVariant { [FieldOffset(0)] internal ushort Type; [FieldOffset(8)] internal nint Pointer; }
    [ComImport, Guid("886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    private interface IPropertyStore
    {
        [PreserveSig] int GetCount(out uint count);
        [PreserveSig] int GetAt(uint index, out PropertyKey key);
        [PreserveSig] int GetValue(ref PropertyKey key, out PropVariant value);
        [PreserveSig] int SetValue(ref PropertyKey key, ref PropVariant value);
        [PreserveSig] int Commit();
    }
}
