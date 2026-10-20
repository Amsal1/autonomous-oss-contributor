// ossbot shutdown blocker (CR-001 6a, DESIGN 13.5, ADR-0021 and its post-v1 addendum). C# 5 (the
// compiler Add-Type uses in Windows PowerShell 5.1). Compiled at runtime from this file by
// ossbot-shutdown-blocker.ps1; no binary is checked in or copied anywhere.
//
// One off-screen, visible, top-level tool window (not in the taskbar or Alt+Tab). Windows terminates
// console processes and processes without a visible window that do not answer WM_QUERYENDSESSION
// within 5 s, so the window must stay visible (WS_VISIBLE), only placed off-screen.
//
// Windows refuses to initialise a new process in a session that is ending (a child started then dies
// with 0xC0000142 or 0xC000026B), so nothing is started at shutdown time. At start the blocker opens
// a long-lived drain channel instead: `wsl.exe -d <distro> -u root --exec <ossctl> drain-listen`,
// hidden, with its stdin/stdout kept as pipes. A 60 s timer restarts the channel when it died while
// the distro runs (it never starts the distro itself). On WM_QUERYENDSESSION (shutdown, restart,
// sign-out) it registers the block reason with ShutdownBlockReasonCreate (Windows shows it in the
// "apps preventing shutdown" screen, where the user can still pick "Shut down anyway"), writes
// "down <timeout> blocker" to the channel, pumps messages until the listener answers "drained", then
// destroys the reason and returns TRUE so the session ends. Process creation is refused for the whole
// query (spawnForbidden).
using System;
using System.Collections.Concurrent;
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
        public const string LifecycleMutexName = "Local\\ossbot-lifecycle";
        const int WM_QUERYENDSESSION = 0x0011;
        const int WM_ENDSESSION = 0x0016;
        const int WS_EX_TOOLWINDOW = 0x00000080;
        const int WS_EX_APPWINDOW = 0x00040000;
        const int WS_EX_NOACTIVATE = 0x08000000;
        const long ENDSESSION_CLOSEAPP = 0x00000001;
        const long ENDSESSION_CRITICAL = 0x40000000;
        const long ENDSESSION_LOGOFF = 0x80000000;
        // The listener's drain is bounded by its own timeout; this is the outer bound of the wait.
        const int CapSlackSeconds = 30;
        const int TickMs = 1000;
        const int ChannelCheckTicks = 60;
        const int LockWaitMs = 5000;

        [DllImport("user32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        static extern bool ShutdownBlockReasonCreate(IntPtr hWnd, string pwszReason);

        [DllImport("user32.dll", SetLastError = true)]
        static extern bool ShutdownBlockReasonDestroy(IntPtr hWnd);

        [DllImport("user32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        static extern bool ShutdownBlockReasonQuery(IntPtr hWnd, StringBuilder pwszBuff, ref uint pcchBuff);

        [DllImport("user32.dll")]
        static extern bool IsWindowVisible(IntPtr hWnd);

        readonly string distro;
        readonly string listener;
        readonly int downTimeout;
        readonly string logPath;
        readonly string statePath;
        readonly string lifecycleLogPath;
        readonly System.Windows.Forms.Timer timer = new System.Windows.Forms.Timer();
        bool busy;
        bool spawnForbidden;
        int lastExit = -1;
        DateTime lastDone = DateTime.MinValue;

        // The drain channel. Only the window thread touches these, except `lines` (filled by the
        // stdout reader thread).
        Process channel;
        StreamWriter channelIn;
        readonly ConcurrentQueue<string> lines = new ConcurrentQueue<string>();
        string channelState = "none";   // none | up | down | skipped
        string lastPong = "";
        int pingsUnanswered;
        int ticks;
        bool checkRunning;

        public ShutdownBlockerWindow(string distro, string listener, int downTimeout, string logPath,
            string statePath, string lifecycleLogPath)
        {
            this.distro = distro;
            this.listener = listener;
            this.downTimeout = downTimeout;
            this.logPath = logPath;
            this.statePath = statePath;
            this.lifecycleLogPath = lifecycleLogPath;
            Text = WindowTitle;
            FormBorderStyle = FormBorderStyle.None;
            StartPosition = FormStartPosition.Manual;
            Location = new Point(-32000, -32000);
            Size = new Size(1, 1);
            MinimumSize = new Size(1, 1);
            timer.Interval = TickMs;
            timer.Tick += delegate { Tick(); };
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
            CheckChannel();
            timer.Start();
        }

        protected override void OnFormClosed(FormClosedEventArgs e)
        {
            timer.Stop();
            CloseChannel("blocker closing");
            base.OnFormClosed(e);
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

        // ------------------------------------------------------------------ process creation
        // The one place that starts a process. Refused while a WM_QUERYENDSESSION is handled: a
        // process created in an ending session fails to initialise (ADR-0021 addendum post-v1).
        Process Spawn(ProcessStartInfo psi, string what)
        {
            if (spawnForbidden)
            {
                Log("spawn refused during WM_QUERYENDSESSION: " + what);
                throw new InvalidOperationException("no process creation during WM_QUERYENDSESSION");
            }
            return Process.Start(psi);
        }

        static string WslExe()
        {
            return Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.System), "wsl.exe");
        }

        // ------------------------------------------------------------------ the drain channel
        bool ChannelAlive()
        {
            if (channel == null) return false;
            try { return !channel.HasExited; } catch (Exception) { return false; }
        }

        // Every second: pongs, and a dead channel noticed at once. Every 60 s: ping, or restart.
        void Tick()
        {
            if (busy) return;
            DrainIdleLines();
            if (channel != null && !ChannelAlive())
            {
                ticks = 0;
                CheckChannel();
                return;
            }
            if (++ticks < ChannelCheckTicks) return;
            ticks = 0;
            CheckChannel();
        }

        void CheckChannel()
        {
            if (busy) return;
            if (ChannelAlive())
            {
                if (pingsUnanswered >= 2)
                {
                    Log("channel not answering (" + pingsUnanswered + " pings); restarting it");
                    CloseChannel("unanswered");
                }
                else
                {
                    pingsUnanswered++;
                    Send("ping");
                    return;
                }
            }
            if (channel != null)
            {
                int code = -1;
                try { code = channel.ExitCode; } catch (Exception) { }
                Log("channel died exit=" + code);
                DisposeChannel();
                SetChannelState("down");
            }
            StartDistroCheck();
        }

        // `wsl.exe -l --running -q` without blocking the window thread; the result comes back through
        // BeginInvoke. Never starts the distro.
        void StartDistroCheck()
        {
            if (checkRunning) return;
            ProcessStartInfo psi = new ProcessStartInfo(WslExe(), "-l --running -q");
            psi.UseShellExecute = false;
            psi.CreateNoWindow = true;
            psi.RedirectStandardOutput = true;
            psi.StandardOutputEncoding = new UTF8Encoding(false);
            psi.EnvironmentVariables["WSL_UTF8"] = "1";
            Process p;
            try { p = Spawn(psi, "distro check"); }
            catch (Exception ex) { Log("distro check failed to start: " + ex.Message); return; }
            checkRunning = true;
            ThreadPool.QueueUserWorkItem(delegate
            {
                bool running = false;
                try
                {
                    string output = p.StandardOutput.ReadToEnd();
                    p.WaitForExit(15000);
                    foreach (string raw in output.Split('\n'))
                    {
                        if (raw.Replace("\0", "").Trim() == distro) running = true;
                    }
                }
                catch (Exception) { }
                finally { p.Dispose(); }
                try { BeginInvoke(new Action<bool>(DistroChecked), running); } catch (Exception) { }
            });
        }

        void DistroChecked(bool running)
        {
            checkRunning = false;
            if (busy || ChannelAlive()) return;
            if (!running)
            {
                if (channelState != "skipped") Log("channel skipped: distro stopped");
                SetChannelState("skipped");
                return;
            }
            StartChannel();
        }

        void StartChannel()
        {
            bool restart = channelState != "none";
            ProcessStartInfo psi = new ProcessStartInfo(WslExe(),
                "-d " + distro + " -u root --exec " + listener + " drain-listen");
            psi.UseShellExecute = false;
            psi.CreateNoWindow = true;
            psi.RedirectStandardInput = true;
            psi.RedirectStandardOutput = true;
            psi.RedirectStandardError = true;
            psi.StandardOutputEncoding = new UTF8Encoding(false);
            psi.StandardErrorEncoding = new UTF8Encoding(false);
            Process p;
            try { p = Spawn(psi, "drain channel"); }
            catch (Exception ex) { Log("channel failed to start: " + ex.Message); return; }
            p.OutputDataReceived += delegate(object s, DataReceivedEventArgs a) { if (a.Data != null) lines.Enqueue(a.Data.Trim()); };
            p.ErrorDataReceived += delegate(object s, DataReceivedEventArgs a)
            {
                if (a.Data != null && a.Data.Trim().Length > 0) ShutdownBlocker.Append(logPath, "channel: " + Clip(a.Data.Trim(), 300));
            };
            p.BeginOutputReadLine();
            p.BeginErrorReadLine();
            channel = p;
            // Our own writer without a preamble over the pipe (the default one uses the console encoding).
            channelIn = new StreamWriter(p.StandardInput.BaseStream, new UTF8Encoding(false));
            channelIn.NewLine = "\n";
            channelIn.AutoFlush = true;
            pingsUnanswered = 0;
            lastPong = "";
            Log("channel " + (restart ? "restarted" : "started") + " pid=" + p.Id);
            SetChannelState("up");
            pingsUnanswered = 1;
            Send("ping");
        }

        bool Send(string line)
        {
            try
            {
                channelIn.WriteLine(line);
                return true;
            }
            catch (Exception ex)
            {
                Log("channel write failed (" + line.Split(' ')[0] + "): " + ex.GetType().Name);
                return false;
            }
        }

        // Pongs that arrive between queries; anything else outside a query is logged.
        void DrainIdleLines()
        {
            string l;
            bool changed = false;
            while (lines.TryDequeue(out l))
            {
                if (l.StartsWith("pong "))
                {
                    pingsUnanswered = 0;
                    string pong = l.Substring(5);
                    if (lastPong == "") Log("channel up pid=" + (ChannelAlive() ? channel.Id : 0) + " pong=" + pong);
                    changed = changed || pong != lastPong;
                    lastPong = pong;
                }
                else if (l.Length > 0) Log("channel said: " + Clip(l, 120));
            }
            if (changed) WriteState("idle", false);
        }

        void CloseChannel(string why)
        {
            if (channel == null) return;
            try { if (channelIn != null) channelIn.Close(); } catch (Exception) { }
            try { if (!channel.WaitForExit(2000)) channel.Kill(); } catch (Exception) { }
            Log("channel closed (" + why + ")");
            DisposeChannel();
            SetChannelState("down");
        }

        void DisposeChannel()
        {
            try { if (channel != null) channel.Dispose(); } catch (Exception) { }
            channel = null;
            channelIn = null;
            string ignored;
            while (lines.TryDequeue(out ignored)) { }
        }

        void SetChannelState(string s)
        {
            channelState = s;
            WriteState(busy ? "stopping" : "idle", false);
        }

        // ------------------------------------------------------------------ WM_QUERYENDSESSION
        bool QueryEndSession(long lp)
        {
            string kind = Describe(lp);
            if (busy)
            {
                Log("WM_QUERYENDSESSION (" + kind + ") while ossbot is stopping: still blocking");
                return false;
            }
            busy = true;
            spawnForbidden = true;
            try
            {
                bool created = ShutdownBlockReasonCreate(Handle, BlockReason);
                int err = created ? 0 : Marshal.GetLastWin32Error();
                string q = QueryReason();
                Log("WM_QUERYENDSESSION (" + kind + "): block reason created=" + created
                    + (err != 0 ? " error=" + err : "") + " query=" + Quote(q));
                WriteState("stopping", q != null);
                lastExit = DrainViaChannel();
                bool destroyed = ShutdownBlockReasonDestroy(Handle);
                string q2 = QueryReason();
                Log("drain exit=" + lastExit + "; block reason destroyed=" + destroyed + " query=" + Quote(q2)
                    + "; the session may end");
                lastDone = DateTime.UtcNow;
                WriteState("released", q2 != null);
                return true;
            }
            finally
            {
                spawnForbidden = false;
                busy = false;
            }
        }

        int DrainViaChannel()
        {
            if (!ChannelAlive())
            {
                Log("no channel (distro stopped or listener dead): nothing to drain");
                return 0;
            }
            string tag = "down kind=graceful caller=blocker pid=" + Process.GetCurrentProcess().Id;
            Mutex mutex = new Mutex(false, LifecycleMutexName);
            bool held = false;
            Stopwatch sw = Stopwatch.StartNew();
            while (!held && sw.ElapsedMilliseconds < LockWaitMs)
            {
                try { held = mutex.WaitOne(200); }
                catch (AbandonedMutexException) { held = true; }
                Application.DoEvents();
            }
            if (!held) Log("lifecycle lock busy after " + (LockWaitMs / 1000) + " s (a manual down/up runs); draining anyway");
            try
            {
                LifecycleLog(tag + ": start timeout=" + downTimeout + " (channel)");
                string stale;
                while (lines.TryDequeue(out stale)) { }
                int exit = 4;
                if (!Send("down " + downTimeout + " blocker"))
                {
                    exit = 5;
                }
                else
                {
                    Log("channel: sent down " + downTimeout + " blocker");
                    int capMs = (downTimeout + CapSlackSeconds) * 1000;
                    sw = Stopwatch.StartNew();
                    bool done = false;
                    while (!done)
                    {
                        Application.DoEvents();
                        string l;
                        while (!done && lines.TryDequeue(out l))
                        {
                            if (l == "ack") Log("channel: ack");
                            else if (l.StartsWith("drained "))
                            {
                                Log("channel: " + Clip(l, 80) + " (" + (sw.ElapsedMilliseconds / 1000.0).ToString("0.0") + " s)");
                                exit = ParseExit(l);
                                done = true;
                            }
                            else if (l.StartsWith("error"))
                            {
                                Log("channel refused the down line: " + Clip(l, 80));
                                exit = 2;
                                done = true;
                            }
                        }
                        if (done) break;
                        if (!ChannelAlive())
                        {
                            Log("channel ended during the drain");
                            exit = 5;
                            break;
                        }
                        if (sw.ElapsedMilliseconds > capMs)
                        {
                            Log("no drained answer after " + (capMs / 1000) + " s; letting the session end (crash safety covers the rest)");
                            exit = 4;
                            break;
                        }
                        Thread.Sleep(100);
                    }
                }
                LifecycleLog(tag + ": ossctl-exit=" + exit);
                LifecycleLog(tag + ": end exit=" + exit);
                return exit;
            }
            finally
            {
                if (held) { try { mutex.ReleaseMutex(); } catch (Exception) { } }
                mutex.Dispose();
            }
        }

        static int ParseExit(string drained)
        {
            // "drained exit=<n> elapsed=<s>"
            foreach (string part in drained.Split(' '))
            {
                int n;
                if (part.StartsWith("exit=") && int.TryParse(part.Substring(5), out n)) return n;
            }
            return 3;
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

        static string Clip(string s, int n)
        {
            return s.Length <= n ? s : s.Substring(0, n) + "...";
        }

        void WriteState(string phase, bool reasonActive)
        {
            int cpid = 0;
            try { if (ChannelAlive()) cpid = channel.Id; } catch (Exception) { }
            string json = "{\"pid\": " + Process.GetCurrentProcess().Id
                + ", \"hwnd\": \"0x" + Handle.ToInt64().ToString("x") + "\""
                + ", \"phase\": \"" + phase + "\""
                + ", \"reason_active\": " + (reasonActive ? "true" : "false")
                + ", \"down_exit\": " + lastExit
                + ", \"channel\": \"" + channelState + "\""
                + ", \"channel_pid\": " + cpid
                + ", \"channel_pong\": \"" + lastPong.Replace("\"", "").Replace("\\", "") + "\""
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

        // Same line format as Write-OssLifecycleLog (scripts\lib\common.ps1).
        void LifecycleLog(string text)
        {
            string line = DateTime.UtcNow.ToString("o") + " " + text + "\r\n";
            for (int i = 0; i < 5; i++)
            {
                try
                {
                    File.AppendAllText(lifecycleLogPath, line, new UTF8Encoding(false));
                    return;
                }
                catch (IOException)
                {
                    Thread.Sleep(100);
                }
            }
        }
    }

    public static class ShutdownBlocker
    {
        public const string MutexName = "Local\\ossbot-shutdown-blocker";
        static readonly object appendLock = new object();

        [DllImport("kernel32.dll")]
        static extern bool SetProcessShutdownParameters(uint dwLevel, uint dwFlags);

        [DllImport("kernel32.dll")]
        static extern bool FreeConsole();

        public static void Append(string logPath, string text)
        {
            string line = DateTime.UtcNow.ToString("yyyy-MM-ddTHH:mm:ss.fffZ") + " pid="
                + Process.GetCurrentProcess().Id + " " + text + "\r\n";
            lock (appendLock)
            {
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
        }

        // Single instance per session (the mutex), then the window's message loop until WM_CLOSE or the
        // end of the session. Returns 0, or 1 when the window could not be created.
        public static int Run(string distro, string listener, int downTimeout, string logPath, string statePath,
            string lifecycleLogPath, bool keepConsole)
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
                    // (0x280) processes, among them the channel's wsl.exe.
                    bool level = SetProcessShutdownParameters(0x3FF, 0);
                    Append(logPath, "blocker starting (channel " + distro + ":" + listener
                        + " drain-listen, shutdown level 0x3ff set=" + level + ")");
                    using (ShutdownBlockerWindow w = new ShutdownBlockerWindow(distro, listener, downTimeout,
                        logPath, statePath, lifecycleLogPath))
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
