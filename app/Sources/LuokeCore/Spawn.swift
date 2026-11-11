// 本体起子进程只走这里（安全审查 H3）：除了这个文件，代码里不直接用 Process() 和 posix_spawn（SpawnTests.testSpawnRules 拦着）。
//
// 默认 disclaim：子进程自己当「负责的进程」，不再借螺壳的授权（完全磁盘访问等）。只有两类不 disclaim，写 `.luoke`：
// - 要用螺壳授权的：螺壳搜索、螺壳扫描（含 --run-tool 的空闲任务和替扫描弹密码框的那一步）。
// - 要改 .app 的：换包、装配件、升级。系统只让同一个开发者的应用改它（App Management），disclaim 了的 sh、ditto 改不了螺壳.app。
//
// 系统怎么认（11-09 在正式签名、给了完全磁盘访问的螺壳上实测，读的是自己建的测试文件）：
// - 可执行文件在螺壳.app 里的，一律算螺壳：disclaim 了照样有螺壳的授权，谁起的都一样。自带的 Python、Helpers 里的程序都是这样。
//   所以给主控、agent 核心、邮件助手 disclaim 挡不住它们自己，挡的是包外的程序借它们的名义。
// - 包外的程序（引擎、系统命令、输入法、各应用的启动器）：起它的算螺壳就跟着有授权，disclaim 了就没有。
//   主控起引擎、打分进程也要 disclaim，那一边在 luoke_core/spawn.py，经这里的 --disclaim-exec。
//
// Process 带不了 disclaim：.disclaimed 的 Process 起的是螺壳自己的 `--disclaim-exec <程序> [参数…]`，它带着 disclaim 原地换成那个
// 程序（POSIX_SPAWN_SETEXEC，pid 不变），Process 的管道、等待、结束照常用。换不成就退出（124），不退回普通方式。
import Darwin
import Foundation

enum Spawn {
    enum Access {
        /// 子进程自己负责，不借螺壳的授权
        case disclaimed
        /// 照旧算螺壳：只给要螺壳授权的（搜索、扫描）和要改 .app 的（换包、安装、升级）
        case luoke
    }

    static let disclaimFlag = "--disclaim-exec"
    static let disclaimFailed: Int32 = 124

    /// 配好可执行文件和参数的 Process，别的（环境、标准输入输出、结束回调）调用方照常设。不要再改 executableURL 和 arguments
    static func process(_ path: String, _ args: [String], access: Access = .disclaimed, trampoline: URL? = Spawn.trampoline) -> Process {
        let p = Process()
        if access == .disclaimed, let me = trampoline {
            p.executableURL = me
            p.arguments = [disclaimFlag, path] + args
        } else {
            p.executableURL = URL(fileURLWithPath: path)
            p.arguments = args
        }
        return p
    }

    /// 螺壳自己的可执行文件，--disclaim-exec 用。没打包成 .app（swift run、swift test）时是 nil，直接起：那时也没有螺壳的授权可借
    static let trampoline: URL? = Bundle.main.bundleURL.pathExtension == "app" ? Bundle.main.executableURL : nil

    private typealias SetDisclaim = @convention(c) (UnsafeMutablePointer<posix_spawnattr_t?>, Int32) -> Int32
    /// 私有接口，libSystem 里有（macOS 10.14 起）。找不到就是 nil，要 disclaim 的一律起不来
    private static let setDisclaim: SetDisclaim? = dlsym(UnsafeMutableRawPointer(bitPattern: -2), "responsibility_spawnattrs_setdisclaim")   // -2：RTLD_DEFAULT
        .map { unsafeBitCast($0, to: SetDisclaim.self) }

    private static func disclaim(_ attr: inout posix_spawnattr_t?) -> Bool {
        guard let setDisclaim else { return false }
        return setDisclaim(&attr, 1) == 0
    }

    /// 螺壳 --disclaim-exec <程序> [参数…]：带 disclaim 原地换成那个程序。只留 0–2，环境照旧，信号恢复默认。程序要给绝对路径
    static func execDisclaimed(_ argv: [String]) -> Never {
        func fail(_ why: String) -> Never {
            FileHandle.standardError.write(Data("螺壳 \(disclaimFlag)：\(why)\n".utf8))
            exit(disclaimFailed)
        }
        guard let path = argv.first, path.hasPrefix("/") else { fail("要给程序的绝对路径") }
        var actions: posix_spawn_file_actions_t?
        posix_spawn_file_actions_init(&actions)
        for fd: Int32 in 0...2 where fcntl(fd, F_GETFD) != -1 { posix_spawn_file_actions_addinherit_np(&actions, fd) }
        var attr: posix_spawnattr_t?
        posix_spawnattr_init(&attr)
        posix_spawnattr_setflags(&attr, Int16(POSIX_SPAWN_SETEXEC | POSIX_SPAWN_CLOEXEC_DEFAULT | POSIX_SPAWN_SETSIGDEF | POSIX_SPAWN_SETSIGMASK))
        var all = sigset_t(UInt32.max)
        posix_spawnattr_setsigdefault(&attr, &all)
        var none = sigset_t(0)
        posix_spawnattr_setsigmask(&attr, &none)
        guard disclaim(&attr) else { fail("系统不让 disclaim，不起 \(path)") }
        let cargs: [UnsafeMutablePointer<CChar>?] = argv.map { strdup($0) } + [nil]
        let rc = posix_spawn(nil, path, &actions, &attr, cargs, environ)    // 成功就不回来了
        fail("起不了 \(path)：\(String(cString: strerror(rc)))")
    }

    /// posix_spawn：只留 `fds` 里映射的描述符（POSIX_SPAWN_CLOEXEC_DEFAULT，其余一律不漏），工作目录、环境照给的，信号恢复默认。
    /// `suspended`：起来先停着（POSIX_SPAWN_START_SUSPENDED），核过再 SIGCONT 放行。`.disclaimed` 带不上 disclaim 就不起
    static func run(_ path: String, _ args: [String], env: [String], cwd: String, suspended: Bool = false,
                    fds: [(Int32, Int32)], access: Access = .disclaimed) throws -> pid_t {
        var actions: posix_spawn_file_actions_t?
        posix_spawn_file_actions_init(&actions)
        defer { posix_spawn_file_actions_destroy(&actions) }
        for (from, to) in fds { posix_spawn_file_actions_adddup2(&actions, from, to) }
        posix_spawn_file_actions_addchdir_np(&actions, cwd)
        var attr: posix_spawnattr_t?
        posix_spawnattr_init(&attr)
        defer { posix_spawnattr_destroy(&attr) }
        var flags = POSIX_SPAWN_CLOEXEC_DEFAULT | POSIX_SPAWN_SETSIGDEF | POSIX_SPAWN_SETSIGMASK
        if suspended { flags |= POSIX_SPAWN_START_SUSPENDED }
        posix_spawnattr_setflags(&attr, Int16(flags))
        var all = sigset_t(UInt32.max)
        posix_spawnattr_setsigdefault(&attr, &all)
        var none = sigset_t(0)
        posix_spawnattr_setsigmask(&attr, &none)
        if access == .disclaimed, !disclaim(&attr) { throw POSIXError(.ENOTSUP) }
        let argv: [UnsafeMutablePointer<CChar>?] = ([path] + args).map { strdup($0) } + [nil]
        let envp: [UnsafeMutablePointer<CChar>?] = env.map { strdup($0) } + [nil]
        defer { (argv + envp).forEach { free($0) } }
        var pid: pid_t = 0
        let rc = posix_spawn(&pid, path, &actions, &attr, argv, envp)
        guard rc == 0 else { throw POSIXError(POSIXErrorCode(rawValue: rc) ?? .EIO) }
        return pid
    }

    /// 起一次、等它退出：停着起，check 过了才放行，标准输入和没要的那一路接 /dev/null，`capture` 那一路（1 或 2）读回来。
    /// check 没过时抛 Refused，程序一行也没跑。status 是 waitpid 的状态
    struct Output { let status: Int32; let data: Data }
    struct Refused: Error { let why: String }

    /// `channel`：描述符 3 给一对 socketpair 的一端（不设 LUOKE_HOST_FD），本体这头对每个请求答 failed：
    /// 只给工具认「是螺壳起的」（它在上面来回一行，看答话的是不是父进程）
    static func capture(_ path: String, _ args: [String], env: [String], capture fd: Int32, access: Access = .disclaimed,
                        channel: Bool = false, check: (pid_t) -> String?) throws -> Output {
        let out = Pipe()
        let null = open("/dev/null", O_RDWR | O_CLOEXEC)
        guard null >= 0 else { throw POSIXError(.EIO) }
        defer { close(null) }
        var ends: (host: Int32, tool: Int32)?
        if channel {
            let (h, t) = try FD.pair()
            FD.closeOnExec(h)
            FD.closeOnExec(t)       // 只经 fds 交给这一次起的程序（dup2 到 3 会去掉这一标记）；别处同时起的进程拿不到
            ends = (h, t)
        }
        var answered: DispatchSemaphore?
        defer {
            if let ends {
                shutdown(ends.host, SHUT_RDWR)      // 它起的进程还拿着另一头也不等
                answered?.wait()
                close(ends.host)
            }
        }
        let write = out.fileHandleForWriting.fileDescriptor
        var fds = [(null, Int32(0)), (fd == 1 ? write : null, Int32(1)), (fd == 2 ? write : null, Int32(2))]
        if let ends { fds.append((ends.tool, 3)) }
        let pid: pid_t
        do {
            defer { if let ends { close(ends.tool) } }
            pid = try run(path, args, env: env, cwd: "/", suspended: true, fds: fds, access: access)
        }
        try? out.fileHandleForWriting.close()
        if let ends { answered = FD.answerAll(ends.host, name: "capture-fd3") { _ in ["status": "failed", "why": "这一次不代办"] } }
        if let why = release(pid, check: { check(pid) }) { throw Refused(why: why) }
        let data = out.fileHandleForReading.readDataToEndOfFile()
        var status: Int32 = 0
        while waitpid(pid, &status, 0) < 0 && errno == EINTR {}
        return Output(status: status, data: data)
    }

    /// 停着起的进程：check 过了、而且核完它还停着、一点用户态时间都没用过，才 SIGCONT 放行。同用户的别的进程能对它发 SIGCONT，
    /// 让它在核完以前先跑起来；那样就结束它。不放行时 SIGKILL、收掉，返回原因
    static func release(_ pid: pid_t, check: () -> String?) -> String? {
        var why = check()
        if why == nil, !untouched(pid) {
            why = "它在核完以前被别的进程放出来跑过，不起它"
            NSLog("pid %d 在核签名以前被放出来跑过（不是本体发的 SIGCONT），结束它", pid)
        }
        if let why {
            kill(pid, SIGKILL)
            var status: Int32 = 0
            while waitpid(pid, &status, 0) < 0 && errno == EINTR {}
            return why
        }
        kill(pid, SIGCONT)
        return nil
    }

    /// 还停着（SSTOP，posix_spawn 的 START_SUSPENDED 就是这个状态）、用户态时间是 0（一条指令都没跑过）
    static func untouched(_ pid: pid_t) -> Bool {
        var bsd = proc_bsdinfo()
        let bsdSize = Int32(MemoryLayout<proc_bsdinfo>.size)
        guard proc_pidinfo(pid, PROC_PIDTBSDINFO, 0, &bsd, bsdSize) == bsdSize, bsd.pbi_status == 4 else { return false }   // 4：sys/proc.h 的 SSTOP
        var task = proc_taskinfo()
        let taskSize = Int32(MemoryLayout<proc_taskinfo>.size)
        guard proc_pidinfo(pid, PROC_PIDTASKINFO, 0, &task, taskSize) == taskSize else { return false }
        return task.pti_total_user == 0
    }
}
