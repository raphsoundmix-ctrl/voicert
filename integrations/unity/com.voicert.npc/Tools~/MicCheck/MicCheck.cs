using System;
using System.Linq;
using System.Runtime.InteropServices;

// "Does FMOD hear the microphone?" — answered by FMOD itself, outside Unity.
//
// This loads the real VoiceRT banks, records from a chosen input, plays that live
// recording into bus:/VoiceInput so the signal genuinely enters the FMOD Studio
// mixer, and then reads the level from FMOD's OWN metering DSP rather than from a
// hand-rolled scan of the samples. Both numbers are printed side by side, because
// the interesting question is whether FMOD's pre-fader metering still reads signal
// when the monitor volume is zero — which is what makes a silent, feedback-free
// signal check possible.
//
// Usage:
//   MicCheck --list
//   MicCheck [--device <index|substring>] [--seconds N] [--monitor 0..1]
//            [--bus bus:/VoiceInput] [--output <index|substring>]
static class MicCheck
{
    const string BankDir = @"D:\VOICE RT\FMOD VoiceRT\Fmod_VoiceRT_Prototype\Build\Desktop";

    // FMOD for Unity ships ONE Windows library, fmodstudio.dll, containing both the
    // core and the studio API. The C# binding still imports them under two different
    // names ("fmod" and "fmodstudio"), so satisfying each with its own copy of the
    // file loads the library TWICE: the Studio system is created inside one instance
    // and every core call goes to the other, which has never heard of that handle.
    // The symptom is a core system that reports hasHandle=true and then answers
    // ERR_INVALID_HANDLE to everything, with getVersion returning 0.
    static MicCheck()
    {
        System.Reflection.Assembly self = typeof(MicCheck).Assembly;
        NativeLibrary.SetDllImportResolver(self, (name, asm, path) =>
            name is "fmod" or "fmodstudio"
                ? NativeLibrary.Load(System.IO.Path.Combine(AppContext.BaseDirectory, "fmodstudio.dll"))
                : IntPtr.Zero);
    }

    static int Main(string[] args)
    {
        string deviceArg = Arg(args, "--device");
        string outputArg = Arg(args, "--output");
        string busPath = Arg(args, "--bus") ?? "bus:/VoiceInput";
        float monitor = float.TryParse(Arg(args, "--monitor"), out var m) ? m : 0f;
        double seconds = double.TryParse(Arg(args, "--seconds"), out var s) ? s : 8.0;
        bool listOnly = args.Contains("--list");

        if (FMOD.Studio.System.create(out FMOD.Studio.System studio) != FMOD.RESULT.OK)
        { Console.WriteLine("Studio.System.create failed"); return 1; }
        studio.getCoreSystem(out FMOD.System core);

        // --- output device -------------------------------------------------
        // The record subsystem only comes up once an OUTPUT device initialises, so
        // a wedged or exclusively-held output takes the microphone down with it.
        // Walk the outputs rather than insisting on the system default.
        var numRes = core.getNumDrivers(out int outs);
        Console.WriteLine($"getNumDrivers -> {numRes}, {outs} output driver(s)");
        int chosenOut = -1;
        // With no enumerable outputs yet, still give the default one a chance: some
        // drivers only populate the list once the system has initialised.
        var order = outs > 0 ? Enumerable.Range(0, outs).ToList()
                             : new System.Collections.Generic.List<int> { -1 };
        if (outputArg != null)
        {
            int only = ResolveDriver(core, outs, outputArg, output: true);
            if (only >= 0) { order.Clear(); order.Add(only); }
        }
        foreach (int i in order)
        {
            string on = "(system default)";
            if (i >= 0) { core.setDriver(i); core.getDriverInfo(i, out on, 256, out _, out _, out _, out _); }
            var r = studio.initialize(256, FMOD.Studio.INITFLAGS.NORMAL, FMOD.INITFLAGS.NORMAL, IntPtr.Zero);
            Console.WriteLine($"output [{i}] \"{on}\" init -> {r}");
            if (r == FMOD.RESULT.OK)
            {
                chosenOut = i < 0 ? 0 : i;
                // The core system handle handed out before initialize() is not usable —
                // getNumDrivers on it returns ERR_INVALID_HANDLE. Re-fetch it now.
                var gcs = studio.getCoreSystem(out core);
                Console.WriteLine($"  getCoreSystem after init -> {gcs}, hasHandle={core.hasHandle()}");
                break;
            }
            studio.release();
            FMOD.Studio.System.create(out studio);
            studio.getCoreSystem(out core);
        }
        if (chosenOut < 0) { Console.WriteLine("NO OUTPUT DEVICE WOULD INITIALISE."); return 2; }
        Console.WriteLine();

        // --- record devices ------------------------------------------------
        var rnd = core.getRecordNumDrivers(out int num, out int connected);
        Console.WriteLine($"getRecordNumDrivers -> {rnd}: {num} driver(s), {connected} connected");
        core.getVersion(out uint ver, out _);
        Console.WriteLine($"core version 0x{ver:X8}");
        for (int i = 0; i < num; i++)
        {
            var ri = core.getRecordDriverInfo(i, out string nm, 256, out _, out int rt,
                out _, out int ch, out FMOD.DRIVER_STATE st);
            if (ri != FMOD.RESULT.OK) continue;
            bool loop = nm.IndexOf("[loopback]", StringComparison.OrdinalIgnoreCase) >= 0;
            Console.WriteLine($"  [{i}] {nm}   {rt} Hz, {ch} ch, {st}{(loop ? "   (loopback — not a microphone)" : "")}");
        }
        Console.WriteLine();
        if (listOnly) { studio.release(); return 0; }

        int driver = deviceArg != null
            ? ResolveDriver(core, num, deviceArg, output: false)
            : PickBestInput(core, num);
        if (driver < 0) { Console.WriteLine("no usable input device"); studio.release(); return 3; }

        core.getRecordDriverInfo(driver, out string name, 256, out _, out int rate,
            out _, out int channels, out _);
        if (channels <= 0) channels = 1;
        Console.WriteLine($"using input [{driver}] \"{name}\"  {rate} Hz, {channels} ch");

        // --- banks ----------------------------------------------------------
        // The strings bank must load first or bus paths cannot be resolved by name.
        foreach (string bank in new[] { "Master.strings.bank", "Master.bank", "Dialogue.bank" })
        {
            string path = System.IO.Path.Combine(BankDir, bank);
            if (!System.IO.File.Exists(path)) { Console.WriteLine($"bank missing: {path}"); continue; }
            var br = studio.loadBankFile(path, FMOD.Studio.LOAD_BANK_FLAGS.NORMAL, out _);
            Console.WriteLine($"load {bank} -> {br}");
        }
        studio.flushCommands();

        // --- the bus --------------------------------------------------------
        var busResult = studio.getBus(busPath, out FMOD.Studio.Bus bus);
        Console.WriteLine($"getBus(\"{busPath}\") -> {busResult}");
        FMOD.ChannelGroup group = default;
        if (busResult == FMOD.RESULT.OK)
        {
            bus.getVolume(out float busVol, out _);
            Console.WriteLine($"  bus volume = {busVol:0.####} linear ({Db(busVol):0.0} dBFS)");
            // The channel group behind a bus does not exist until the bus is locked
            // AND a Studio update has actually run; asking before that returns an
            // invalid handle, which looks exactly like "the bus is missing".
            Console.WriteLine("  lockChannelGroup -> " + bus.lockChannelGroup());
            studio.flushCommands();
            studio.update();
            studio.flushCommands();
            var gr = bus.getChannelGroup(out group);
            Console.WriteLine($"  getChannelGroup -> {gr}");
            if (gr != FMOD.RESULT.OK) group = default;
        }
        Console.WriteLine();

        // --- record ---------------------------------------------------------
        int loopFrames = rate * 2;                       // 2 s ring
        var ex = new FMOD.CREATESOUNDEXINFO
        {
            cbsize = Marshal.SizeOf(typeof(FMOD.CREATESOUNDEXINFO)),
            numchannels = channels,
            format = FMOD.SOUND_FORMAT.PCM16,
            defaultfrequency = rate,
            length = (uint)(loopFrames * channels * 2),
        };
        var cr = core.createSound(IntPtr.Zero, FMOD.MODE.OPENUSER | FMOD.MODE.LOOP_NORMAL, ref ex, out FMOD.Sound sound);
        if (cr != FMOD.RESULT.OK) { Console.WriteLine("createSound -> " + cr); studio.release(); return 4; }

        var rs = core.recordStart(driver, sound, true);
        Console.WriteLine("recordStart -> " + rs);
        if (rs != FMOD.RESULT.OK)
        {
            Console.WriteLine(rs == FMOD.RESULT.ERR_RECORD
                ? "  -> the device is held by another process (close FMOD Studio / other audio apps)."
                : "");
            sound.release(); studio.release(); return 5;
        }

        // --- play the live recording into the bus ---------------------------
        FMOD.Channel channel = default;
        FMOD.DSP fader = default;
        FMOD.DSP groupHead = default;
        bool monitoring = false;
        if (group.hasHandle())
        {
            // Wait until the recorder is comfortably ahead, or playback starts inside
            // the part of the ring that has not been written yet and stutters.
            System.Threading.Thread.Sleep(150);
            core.update();
            var pr = core.playSound(sound, group, false, out channel);
            Console.WriteLine($"playSound into {busPath} -> {pr}");
            if (pr == FMOD.RESULT.OK)
            {
                // Shipped design: the channel stays at unity so its fader DSP keeps
                // running and metering stays honest; silence is taken at the BUS.
                channel.setVolume(1f);
                channel.setPriority(0);
                bus.setVolume(monitor);
                var dr = channel.getDSP(FMOD.CHANNELCONTROL_DSP_INDEX.FADER, out fader);
                Console.WriteLine($"  getDSP(FADER) -> {dr}");
                // Meter the BUS as well: a Studio bus meters its own ChannelGroup, and
                // if the channel's own fader reads nothing the group tells us whether the
                // signal reached the bus at all.
                if (group.getDSP(FMOD.CHANNELCONTROL_DSP_INDEX.HEAD, out groupHead) == FMOD.RESULT.OK)
                    Console.WriteLine("  group HEAD metering -> " + groupHead.setMeteringEnabled(true, true));
                if (dr == FMOD.RESULT.OK)
                {
                    // INPUT metering is taken before the fader applies the volume, which
                    // is the whole trick: the level reads true at monitor volume 0.
                    Console.WriteLine("  setMeteringEnabled(input) -> " + fader.setMeteringEnabled(true, true));
                    monitoring = true;
                }
            }
        }
        else Console.WriteLine("no bus channel group — monitoring skipped, raw scan only");

        Console.WriteLine();
        Console.WriteLine($"listening for {seconds:0.#} s   (monitor volume {monitor:0.##})");
        Console.WriteLine("  raw   = peak of the PCM we captured ourselves");
        Console.WriteLine("  fmod  = FMOD's own metering DSP on the bus channel");
        Console.WriteLine();

        uint last = 0; int rawPeak = 0; float fmodInPeak = 0f, fmodOutPeak = 0f;
        var until = DateTime.UtcNow.AddSeconds(seconds);
        var nextPrint = DateTime.UtcNow;
        while (DateTime.UtcNow < until)
        {
            System.Threading.Thread.Sleep(20);
            studio.update();
            core.update();

            if (core.getRecordPosition(driver, out uint pos) == FMOD.RESULT.OK && pos != last)
            {
                uint count = pos >= last ? pos - last : (uint)(loopFrames - last) + pos;
                if (sound.@lock(last * (uint)channels * 2, count * (uint)channels * 2,
                        out IntPtr p1, out IntPtr p2, out uint l1, out uint l2) == FMOD.RESULT.OK)
                {
                    rawPeak = Math.Max(rawPeak, Math.Max(PeakOf(p1, l1), PeakOf(p2, l2)));
                    sound.unlock(p1, p2, l1, l2);
                }
                last = pos;
            }

            if (monitoring &&
                fader.getMeteringInfo(out FMOD.DSP_METERING_INFO inInfo, out FMOD.DSP_METERING_INFO outInfo) == FMOD.RESULT.OK)
            {
                for (int c = 0; c < inInfo.numchannels && c < 32; c++)
                    fmodInPeak = Math.Max(fmodInPeak, inInfo.peaklevel[c]);
                for (int c = 0; c < outInfo.numchannels && c < 32; c++)
                    fmodOutPeak = Math.Max(fmodOutPeak, outInfo.peaklevel[c]);
            }

            if (DateTime.UtcNow >= nextPrint)
            {
                nextPrint = DateTime.UtcNow.AddSeconds(1);
                string chState = "-";
                if (channel.hasHandle())
                {
                    channel.isPlaying(out bool playing);
                    channel.getAudibility(out float aud);
                    channel.getPosition(out uint cpos, FMOD.TIMEUNIT.PCM);
                    channel.getVolume(out float cvol);
                    chState = $"playing={playing} aud={aud:0.000} vol={cvol:0.00} pos={cpos}";
                }
                float gPeak = 0f;
                if (groupHead.hasHandle() &&
                    groupHead.getMeteringInfo(out FMOD.DSP_METERING_INFO gi, IntPtr.Zero) == FMOD.RESULT.OK)
                    for (int c = 0; c < gi.numchannels && c < 32; c++) gPeak = Math.Max(gPeak, gi.peaklevel[c]);
                Console.WriteLine($"  raw {Db(rawPeak / 32768f),7:0.0} " +
                                  $"| ch-fader {Db(fmodInPeak),7:0.0} " +
                                  $"| bus {Db(gPeak),7:0.0} dBFS   {chState}");
            }
        }

        Console.WriteLine();
        Console.WriteLine("=== verdict ===");
        Console.WriteLine($"raw capture      : {Db(rawPeak / 32768f):0.0} dBFS  {(rawPeak > 32 ? "SIGNAL" : "silent")}");
        Console.WriteLine($"FMOD input meter : {Db(fmodInPeak):0.0} dBFS  {(fmodInPeak > 0.001f ? "SIGNAL" : "silent")}");
        Console.WriteLine($"FMOD output meter: {Db(fmodOutPeak):0.0} dBFS  (after volume {monitor:0.##})");
        Console.WriteLine(monitoring
            ? "monitoring path: microphone -> FMOD Core record -> " + busPath + " (in the Studio mixer)"
            : "monitoring path: NOT established");

        if (channel.hasHandle()) channel.stop();
        core.recordStop(driver);
        sound.release();
        if (busResult == FMOD.RESULT.OK) bus.unlockChannelGroup();   // or the strip leaks
        studio.release();
        return 0;
    }

    // Best input = a real capture endpoint, never a loopback (that records the game),
    // and hardware before anything calling itself a virtual mixer.
    static int PickBestInput(FMOD.System core, int num)
    {
        int best = -1, bestRank = int.MaxValue;
        for (int i = 0; i < num; i++)
        {
            if (core.getRecordDriverInfo(i, out string nm, 256, out _, out _, out _, out _,
                    out FMOD.DRIVER_STATE st) != FMOD.RESULT.OK) continue;
            if ((st & FMOD.DRIVER_STATE.CONNECTED) == 0) continue;
            string low = nm.ToLowerInvariant();
            if (low.Contains("[loopback]") || low.Contains("stereo mix")) continue;
            int rank = low.Contains("mixing driver") || low.Contains("virtual") ? 1 : 0;
            if (rank < bestRank) { bestRank = rank; best = i; }
        }
        return best;
    }

    static int ResolveDriver(FMOD.System core, int count, string arg, bool output)
    {
        if (int.TryParse(arg, out int idx) && idx >= 0 && idx < count) return idx;
        for (int i = 0; i < count; i++)
        {
            string nm;
            if (output) core.getDriverInfo(i, out nm, 256, out _, out _, out _, out _);
            else core.getRecordDriverInfo(i, out nm, 256, out _, out _, out _, out _, out _);
            if (nm == null) continue;
            // A name match must never silently land on a loopback: "Realtek USB Audio"
            // appears in both the microphone and the digital-output tap, and recording
            // the tap captures whatever the game is playing instead of the player.
            if (!output && nm.IndexOf("[loopback]", StringComparison.OrdinalIgnoreCase) >= 0
                        && arg.IndexOf("loopback", StringComparison.OrdinalIgnoreCase) < 0) continue;
            if (nm.IndexOf(arg, StringComparison.OrdinalIgnoreCase) >= 0) return i;
        }
        return -1;
    }

    static string Arg(string[] a, string key)
    {
        int i = Array.IndexOf(a, key);
        return i >= 0 && i + 1 < a.Length ? a[i + 1] : null;
    }

    static double Db(double linear) => linear > 1e-6 ? 20.0 * Math.Log10(linear) : -120.0;

    static int PeakOf(IntPtr p, uint bytes)
    {
        if (p == IntPtr.Zero || bytes < 2) return 0;
        int peak = 0, n = (int)(bytes / 2);
        for (int i = 0; i < n; i++)
        {
            short v = Marshal.ReadInt16(p, i * 2);
            int a = v < 0 ? -v : v;
            if (a > peak) peak = a;
        }
        return peak;
    }
}
