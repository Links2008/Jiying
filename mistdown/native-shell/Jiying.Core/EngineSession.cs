using System.Diagnostics;
using System.IO;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;

namespace Jiying.Desktop;

internal sealed class EngineSession(int processId, int port, string token)
{
    internal int ProcessId { get; } = processId;
    internal int Port { get; } = port;
    internal string Token { get; } = token;

    internal static EngineSession? Read(string executable)
    {
        if (!OperatingSystem.IsWindows()) return null;
        var root = Path.GetDirectoryName(Path.GetFullPath(executable))!;
        var profile = Environment.GetEnvironmentVariable("LOCALAPPDATA");
        if (string.IsNullOrWhiteSpace(profile)) return null;
        var hash = Convert.ToHexStringLower(SHA256.HashData(Encoding.UTF8.GetBytes(root.ToLowerInvariant())));
        var descriptor = Path.Combine(profile, "VideoSubtitler", "runtime", hash + ".json");
        try
        {
            using var file = new FileStream(descriptor, FileMode.Open, FileAccess.Read,
                                            FileShare.ReadWrite | FileShare.Delete);
            if (file.Length is < 1 or > 8192) return null;
            using var doc = JsonDocument.Parse(file);
            var json = doc.RootElement;
            if (json.ValueKind != JsonValueKind.Object || !json.TryGetProperty("pid", out var pid) ||
                pid.ValueKind != JsonValueKind.Number ||
                !pid.TryGetInt32(out var id) || id <= 0 ||
                !json.TryGetProperty("port", out var port) || port.ValueKind != JsonValueKind.Number ||
                !port.TryGetInt32(out var value) || value is < 1 or > 65535 ||
                !json.TryGetProperty("token_dpapi", out var token) || token.ValueKind != JsonValueKind.String)
                return null;
            using var process = Process.GetProcessById(id);
            var launched = process.MainModule?.FileName;
            if (!string.Equals(launched, Path.GetFullPath(executable), StringComparison.OrdinalIgnoreCase))
                return null;
            var clear = Unprotect(Convert.FromBase64String(token.GetString()!));
            try
            {
                if (clear.Length != 64 || clear.Any(b => !(b is >= (byte)'0' and <= (byte)'9' or >= (byte)'a' and <= (byte)'f')))
                    return null;
                return new EngineSession(id, value, Encoding.ASCII.GetString(clear));
            }
            finally { CryptographicOperations.ZeroMemory(clear); }
        }
        catch (Exception error) when (error is IOException or UnauthorizedAccessException or ArgumentException or FormatException
                                       or InvalidOperationException or JsonException or CryptographicException or System.ComponentModel.Win32Exception)
        {
            return null;
        }
    }

    private static byte[] Unprotect(byte[] data)
    {
        var pinned = GCHandle.Alloc(data, GCHandleType.Pinned);
        DataBlob output = default;
        try
        {
            var input = new DataBlob { Size = data.Length, Data = pinned.AddrOfPinnedObject() };
            if (!CryptUnprotectData(ref input, 0, 0, 0, 0, 1, out output))
                throw new System.ComponentModel.Win32Exception(Marshal.GetLastWin32Error());
            if (output.Size != 64 || output.Data == 0) throw new CryptographicException("引擎凭据格式无效");
            var clear = new byte[output.Size];
            Marshal.Copy(output.Data, clear, 0, clear.Length);
            Marshal.Copy(new byte[output.Size], 0, output.Data, output.Size);
            return clear;
        }
        finally
        {
            pinned.Free();
            if (output.Data != 0) _ = LocalFree(output.Data);
        }
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct DataBlob
    {
        public int Size;
        public nint Data;
    }

    [DllImport("crypt32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool CryptUnprotectData(ref DataBlob data, nint description, nint entropy,
        nint reserved, nint prompt, uint flags, out DataBlob output);

    [DllImport("kernel32.dll")]
    private static extern nint LocalFree(nint pointer);
}
