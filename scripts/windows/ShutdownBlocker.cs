// ossbot shutdown blocker (CR-001 6a, DESIGN 13.5, ADR-0021). C# 5 (the compiler Add-Type uses in
// Windows PowerShell 5.1). Compiled at runtime from this file by ossbot-shutdown-blocker.ps1; no binary
// is checked in or copied anywhere.
//
// One off-screen, visible, top-level tool window (not in the taskbar or Alt+Tab). Windows terminates
// console processes and processes without a visible window that do not answer WM_QUERYENDSESSION
// within 5 s, so the window must stay visible (WS_VISIBLE), only placed off-screen. On
// WM_QUERYENDSESSION (shutdown, restart, sign-out) it registers the block reason with
// ShutdownBlockReasonCreate (Windows shows it in the "apps preventing shutdown" screen, where the
// user can still pick "Shut down anyway"), runs scripts\down.ps1 -Caller blocker while pumping
// messages, destroys the reason and returns TRUE so the session ends.
using System;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;
using System.Windows.Forms;

namespace Ossbot
{
    public sealed class ShutdownBlockerWindow : Form
    {
        public const string WindowTitle = "ossbot shutdown blocker";
        public const string BlockReason = "ossbot: stopping safely\u2026";
        const int WM_QUERYENDSESSION = 0x0011;
        const int WM_ENDSESSION = 0x0016;
        const int WS_EX_TOOLWINDOW = 0x00000080;
        const int WS_EX_APPWINDOW = 0x00040000;
        const int WS_EX_NOACTIVATE = 0x08000000;
        const long ENDSESSION_CLOSEAPP = 0x00000001;
        const long ENDSESSION_CRITICAL = 0x40000000;
        const long ENDSESSION_LOGOFF = 0x80000000;
        // down.ps1 cuts its own in-distro step at Timeout + 90 s; this is the outer bound.
        const int CapSlackSeconds = 90 + 30;

        [DllImport("user32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        static extern bool ShutdownBlockReasonCreate(IntPtr hWnd, string pwszReason);

        [DllImport("user32.dll", SetLastError = true)]
        static extern bool ShutdownBlockReasonDestroy(IntPtr hWnd);

        [DllImport("user32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        static extern bool ShutdownBlockReasonQuery(IntPtr hWnd, StringBuilder pwszBuff, ref uint pcchBuff);

        [DllImport("user32.dll")]
        static extern bool IsWindowVisible(IntPtr hWnd);

        readonly string downScript;
        readonly int downTimeout;
        readonly string logPath;
        readonly string statePath;
        bool busy;
        int lastExit = -1;
        DateTime lastDone = DateTime.MinValue;

        public ShutdownBlockerWindow(string downScript, int downTimeout, string logPath, string statePath)
        {
            this.downScript = downScript;
            this.downTimeout = downTimeout;
            this.logPath = logPath;
            this.statePath = statePath;
            Text = WindowTitle;
            FormBorderStyle = FormBorderStyle.None;
            StartPosition = FormStartPosition.Manual;
            Location = new Point(-32000, -32000);
            Size = new Size(1, 1);
            MinimumSize = new Size(1, 1);
        }

        protected override bool ShowWithoutActivation { get { return true; } }

        protected override CreateParams CreateParams
        {
            get
            {
                CreateParams cp = base.CreateParams;
                cp.ExStyle = (cp.ExStyle | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE) & ~WS_EX_APPWINDOW;
                return cp;
            }
        }

        protected override void OnShown(EventArgs e)
        {
            base.OnShown(e);
            Log("window up hwnd=0x" + Handle.ToInt64().ToString("x") + " visible=" + IsWindowVisible(Handle)
                + " down timeout=" + downTimeout + " s");
            WriteState("idle", false);
        }

        protected override void WndProc(ref Message m)
        {
            if (m.Msg == WM_QUERYENDSESSION)
            {
                m.Result = QueryEndSession(m.LParam.ToInt64() & 0xFFFFFFFFL) ? new IntPtr(1) : IntPtr.Zero;
                return;
            }
            if (m.Msg == WM_ENDSESSION)
            {
                EndSession(m.WParam != IntPtr.Zero, m.LParam.ToInt64() & 0xFFFFFFFFL);
                m.Result = IntPtr.Zero;
                return;
            }
            base.WndProc(ref m);
        }

        static string Describe(long lp)
        {
            if (lp == 0) return "shutdown or restart";
            string s = "";
            if ((lp & ENDSESSION_LOGOFF) != 0) s += "sign-out ";
            if ((lp & ENDSESSION_CRITICAL) != 0) s += "critical ";
            if ((lp & ENDSESSION_CLOSEAPP) != 0) s += "close-app ";
            return s.Trim() + " (lParam 0x" + lp.ToString("x") + ")";
        }

        bool QueryEndSession(long lp)
        {
            string kind = Describe(lp);
            if (busy)
            {
                Log("WM_QUERYENDSESSION (" + kind + ") while ossbot is stopping: still blocking");
                return false;
            }
            busy = true;
            try
            {
                bool created = ShutdownBlockReasonCreate(Handle, BlockReason);
                int err = created ? 0 : Marshal.GetLastWin32Error();
                string q = QueryReason();
                Log("WM_QUERYENDSESSION (" + kind + "): block reason created=" + created
                    + (err != 0 ? " error=" + err : "") + " query=" + Quote(q));
                WriteState("stopping", q != null);
                lastExit = RunDown();
                bool destroyed = ShutdownBlockReasonDestroy(Handle);
                string q2 = QueryReason();
                Log("down.ps1 exit=" + lastExit + "; block reason destroyed=" + destroyed + " query=" + Quote(q2)
                    + "; the session may end");
                lastDone = DateTime.UtcNow;
                WriteState("released", q2 != null);
                return true;
            }
            finally
            {
                busy = false;
            }
        }

        void EndSession(bool ending, long lp)
        {
            if (ending)
            {
                Log("WM_ENDSESSION: the session ends (" + Describe(lp) + ")");
                WriteState("ending", false);
                return;
            }
            if (lastDone != DateTime.MinValue && (DateTime.UtcNow - lastDone).TotalMinutes < 30)
            {
                Log("WM_ENDSESSION: the session did not end (cancelled); ossbot stays down until scripts\\up.ps1 or the next logon");
            }
            else
            {
                Log("WM_ENDSESSION: the session did not end");
            }
            WriteState("idle", false);
        }

        string QueryReason()
        {
            uint cch = 0;
            if (!ShutdownBlockReasonQuery(Handle, null, ref cch)) return null;
            StringBuilder sb = new StringBuilder((int)cch + 1);
            cch = (uint)sb.Capacity;
            if (!ShutdownBlockReasonQuery(Handle, sb, ref cch)) return null;
            return sb.ToString();
        }

        static string Quote(string s)
        {
            return s == null ? "none" : "'" + s + "'";
        }

        static string PowerShellExe()
        {
            return Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.System),
                "WindowsPowerShell\\v1.0\\powershell.exe");
        }

        int RunDown()
        {
            ProcessStartInfo psi = new ProcessStartInfo(PowerShellExe(),
                "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File \"" + downScript + "\" -Timeout "
                + downTimeout + " -Caller blocker");
            psi.UseShellExecute = false;
            psi.CreateNoWindow = true;
            int capMs = (downTimeout + CapSlackSeconds) * 1000;
            Stopwatch sw = Stopwatch.StartNew();
            Process p;
            try
            {
                p = Process.Start(psi);
            }
            catch (Exception ex)
            {
                Log("could not start down.ps1: " + ex.Message);
                return -1;
            }
            Log("down.ps1 started pid=" + p.Id + " (cap " + (capMs / 1000) + " s)");
            // Keep pumping messages so the window stays responsive while down.ps1 runs.
            while (!p.WaitForExit(200))
            {
                Application.DoEvents();
                if (sw.ElapsedMilliseconds > capMs)
                {
                    Log("down.ps1 still running after " + (capMs / 1000) + " s; killing it (crash safety covers the rest)");
                    try { p.Kill(); } catch (Exception) { }
                    p.WaitForExit(5000);
                    return 4;
                }
            }
            p.WaitForExit();
            Log("down.ps1 finished in " + (sw.ElapsedMilliseconds / 1000.0).ToString("0.0") + " s");
            return p.ExitCode;
        }

        void WriteState(string phase, bool reasonActive)
        {
            string json = "{\"pid\": " + Process.GetCurrentProcess().Id
                + ", \"hwnd\": \"0x" + Handle.ToInt64().ToString("x") + "\""
                + ", \"phase\": \"" + phase + "\""
                + ", \"reason_active\": " + (reasonActive ? "true" : "false")
                + ", \"down_exit\": " + lastExit
                + ", \"ts\": \"" + DateTime.UtcNow.ToString("yyyy-MM-ddTHH:mm:ss.fffZ") + "\"}";
            try
            {
                string tmp = statePath + ".tmp";
                File.WriteAllText(tmp, json + "\r\n", new UTF8Encoding(false));
                if (File.Exists(statePath)) File.Delete(statePath);
                File.Move(tmp, statePath);
            }
            catch (Exception)
            {
            }
        }

        void Log(string text)
        {
            ShutdownBlocker.Append(logPath, text);
        }
    }

    public static class ShutdownBlocker
    {
        public const string MutexName = "Local\\ossbot-shutdown-blocker";

        [DllImport("kernel32.dll")]
        static extern bool SetProcessShutdownParameters(uint dwLevel, uint dwFlags);

        [DllImport("kernel32.dll")]
        static extern bool FreeConsole();

        public static void Append(string logPath, string text)
        {
            string line = DateTime.UtcNow.ToString("yyyy-MM-ddTHH:mm:ss.fffZ") + " pid="
                + Process.GetCurrentProcess().Id + " " + text + "\r\n";
            for (int i = 0; i < 5; i++)
            {
                try
                {
                    File.AppendAllText(logPath, line, new UTF8Encoding(false));
                    return;
                }
                catch (IOException)
                {
                    Thread.Sleep(100);
                }
            }
        }

        // Single instance per session (the mutex), then the window's message loop until WM_CLOSE or the
        // end of the session. Returns 0, or 1 when the window could not be created.
        public static int Run(string downScript, int downTimeout, string logPath, string statePath, bool keepConsole)
        {
            bool createdNew;
            using (Mutex mutex = new Mutex(true, MutexName, out createdNew))
            {
                if (!createdNew)
                {
                    Append(logPath, "another blocker already runs in this session; exiting");
                    return 0;
                }
                try
                {
                    // Detached from the console: closing the (headless) console host can no longer
                    // end this process with CTRL_CLOSE_EVENT before WM_QUERYENDSESSION arrives.
                    if (!keepConsole) FreeConsole();
                    // 0x3FF: the first application range, so this process is asked before the default
                    // (0x280) processes, among them wsl.exe and the down.ps1 it starts.
                    bool level = SetProcessShutdownParameters(0x3FF, 0);
                    Append(logPath, "blocker starting (down " + downScript + ", shutdown level 0x3ff set=" + level + ")");
                    using (ShutdownBlockerWindow w = new ShutdownBlockerWindow(downScript, downTimeout, logPath, statePath))
                    {
                        Application.Run(w);
                    }
                    Append(logPath, "blocker window closed; exiting");
                    return 0;
                }
                catch (Exception ex)
                {
                    Append(logPath, "blocker failed: " + ex.GetType().Name + ": " + ex.Message);
                    return 1;
                }
                finally
                {
                    mutex.ReleaseMutex();
                }
            }
        }
    }
}
