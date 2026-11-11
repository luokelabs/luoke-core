// 由本体当子进程起的程序：Contents/Helpers 里的裸程序，或者自带 Python 跑的应用。经 Spawn（Spawn.swift）直接起，不走 open、NSWorkspace，也不经主控。
// 默认 disclaim；要用螺壳授权的（螺壳搜索）设 access = .luoke。这里只管起、停、看住；起哪个程序、什么时候该起由调用方给（SearchHelper.swift、MailHelper.swift）。
// 给了 check 就停着起，核过才放行（Spawn.run、Spawn.release）；Process 做不到停着起，这时自己收进程（SpawnedChild）。
import Darwin
import Foundation

/// 在跑的子进程：Process，或者 SpawnedChild
private protocol HelperChild: AnyObject {
    var isRunning: Bool { get }
    func terminate()
    func forceKill()
}

extension Process: HelperChild {
    fileprivate func forceKill() { kill(processIdentifier, SIGKILL) }
}

/// posix_spawn 起的子进程，这里收。先等它退出但不收（WNOWAIT），在锁里记下「退了」再收：收掉以前 pid 不会被重用，发信号的在同一把锁里看，不会杀错进程
private final class SpawnedChild: HelperChild {
    private let pid: pid_t
    private let lock = NSLock()
    private var done = false

    init(pid: pid_t, exited: @escaping (SpawnedChild, Process.TerminationReason, Int32) -> Void) {
        self.pid = pid
        let t = Thread { [self] in
            var info = siginfo_t()
            while waitid(P_PID, id_t(pid), &info, WEXITED | WNOWAIT) < 0 && errno == EINTR {}
            lock.lock()
            done = true
            var status: Int32 = 0
            while waitpid(pid, &status, 0) < 0 && errno == EINTR {}
            lock.unlock()
            let signal = status & 0x7f
            exited(self, signal == 0 ? .exit : .uncaughtSignal, signal == 0 ? (status >> 8) & 0xff : signal)
        }
        t.name = "helper-\(pid)"
        t.start()
    }

    var isRunning: Bool { lock.lock(); defer { lock.unlock() }; return !done }
    func terminate() { send(SIGTERM) }
    func forceKill() { send(SIGKILL) }

    private func send(_ signal: Int32) {
        lock.lock(); defer { lock.unlock() }
        if !done { kill(pid, signal) }
    }
}

/// 只在主线程上用。stop 会等它退出，最多 graceSeconds 秒
final class HelperProcess {
    /// 这个数据目录已经有一份在跑（它退出码 3）：不算出错，过一会儿再看
    static let alreadyRunning: Int32 = 3

    struct Options {
        var graceSeconds: TimeInterval = 5
        var restartDelay: TimeInterval = 2
        var alreadyRunningDelay: TimeInterval = 15
        var restartWindow: TimeInterval = 600
        var maxRestarts = 3
    }

    let program: URL
    let arguments: [String]
    /// 每次起的时候现取：用户可能刚改了权限
    let environment: () -> [String: String]
    let logFile: URL
    var options = Options()
    /// 现在该不该跑（用户停用了、系统版本不够就不该）；每次起之前问
    var wanted: () -> Bool = { true }
    /// 停着起的程序经描述符 3 交给本体的话（LUOKE_HOST_FD），一行一个 JSON，在主线程上调。第二个参数是起它时的 round。
    /// 描述符 3 是一对 socketpair：带 id 的行是请求，不交到这里，在读的线程上答（hello 答 ok：它据此认出是螺壳起的）
    var onHostMessage: (([String: Any], Int) -> Void)?
    /// 描述符 3 上的请求（带 id 的行）怎么答，在读的线程上调，第二个参数是起它时的 round。没给就用 `answer`
    var onHostRequest: (([String: Any], Int) -> [String: Any])?
    /// 程序不是它独用的（自带的 Python，主控和 agent 核心也在用）：命令行里有这一项才算它的进程
    var marker: String?
    /// 给了就开一条管道：它的 stdout 上一行一个 JSON，在主线程上交到这里；要说的话经 send 写到它的 stdin
    var onMessage: (([String: Any]) -> Void)?
    /// 它不在了（退了、被停了）。主线程上
    var onExit: (() -> Void)?
    /// 给了就停着起，核过才放行；答的是不放行的原因。不在主线程上调。不和 onMessage 一起用
    var check: ((pid_t) -> String?)?
    /// 默认 disclaim，不借螺壳的授权；要用螺壳授权的（螺壳搜索）写 .luoke（Spawn.swift）
    var access: Spawn.Access = .disclaimed

    /// 环境改过几次。起进程时记下，分得清说话的是新进程，还是取了旧环境、要被换掉的
    private(set) var round = 0
    private var spawnRound = 0

    private var process: HelperChild?
    private var pipe: HelperPipe?
    private var starting = false
    private var stopped = false
    private var restartWhenSpawned = false
    private var restarts: [Date] = []
    private var retry: DispatchWorkItem?

    init(program: URL, arguments: [String], environment: @autoclosure @escaping () -> [String: String], logFile: URL) {
        self.program = program
        self.arguments = arguments
        self.environment = environment
        self.logFile = logFile
    }

    var isRunning: Bool { process?.isRunning ?? false }

    /// 重复调不要紧。也是用户重新启用、或重起次数用完以后再试一次的入口
    func start() {
        stopped = false
        restarts = []
        launch()
    }

    /// 环境变了：这以后取环境的进程才算新的。返回新的 round。换进程是 restart，可以晚一点
    func environmentChanged() -> Int {
        round += 1
        return round
    }

    /// 要换一个进程，先调 environmentChanged。起到一半的拿了旧环境就等它起来再换；拿的已经是新环境就不用换
    func restart() {
        if starting { restartWhenSpawned = spawnRound != round; return }
        stop()
        start()
    }

    private func launch() {
        guard process == nil, !starting, !stopped, wanted() else { return }
        starting = true
        // 内核给的是真实路径（/tmp 是 /private/tmp）
        let path = realpath(program.path, nil).map { p in defer { free(p) }; return String(cString: p) } ?? program.path
        let name = (path as NSString).lastPathComponent
        // 上一次留下的先清掉：要等它退出，不放在主线程上
        DispatchQueue.global().async { [grace = options.graceSeconds, marker] in
            let cleared = Self.clearStale(path: path, name: name, marker: marker, grace: grace)
            if cleared > 0 { NSLog("%@：结束了上一次留下的 %d 个进程", name, cleared) }
            DispatchQueue.main.async { [weak self] in self?.run() }
        }
    }

    private func run() {
        starting = false
        guard process == nil, !stopped, wanted() else { return }
        if let check {
            starting = true
            restartWhenSpawned = false      // 这一下取的就是现在的环境
            spawn(check)
            return
        }
        let p = Spawn.process(program.path, arguments, access: access)
        p.environment = environment()
        p.currentDirectoryURL = URL(fileURLWithPath: "/")
        p.standardInput = FileHandle.nullDevice
        p.standardOutput = FileHandle.nullDevice
        p.standardError = Self.log(logFile)
        var opened: HelperPipe?
        if let onMessage {
            let toChild = Pipe(), fromChild = Pipe()
            p.standardInput = toChild
            p.standardOutput = fromChild
            opened = HelperPipe(read: fromChild.fileHandleForReading, write: toChild.fileHandleForWriting, onMessage: onMessage)
        }
        p.terminationHandler = { [weak self] proc in
            let (reason, status) = (proc.terminationReason, proc.terminationStatus)
            DispatchQueue.main.async { self?.exited(proc, reason, status) }
        }
        do {
            try p.run()
            process = p
            pipe = opened
        } catch {
            opened?.close()
            NSLog("%@ 起不来：%@", program.lastPathComponent, error.localizedDescription)
            scheduleRestart()
        }
    }

    /// 停着起、核过放行。核要读整个程序，不放在主线程上。不放行的原因不会自己好，不重起，等下一次 start
    private func spawn(_ check: @escaping (pid_t) -> String?) {
        var env = environment().map { "\($0.key)=\($0.value)" }
        spawnRound = round
        let path = program.path, args = arguments, log = logFile, round = round, access = access
        let messages = onHostMessage.map { tell in { (m: [String: Any]) in tell(m, round) } }
        let requests: ([String: Any]) -> [String: Any] = onHostRequest.map { ask in { ask($0, round) } } ?? Self.answer
        if messages != nil { env.append("LUOKE_HOST_FD=3") }
        DispatchQueue.global().async { [weak self] in
            let result = Result { () throws -> pid_t in
                let null = open("/dev/null", O_RDWR | O_CLOEXEC)
                guard null >= 0 else { throw POSIXError(.EIO) }
                let out = FD.appendLog(log)
                defer { close(null); if out >= 0 { close(out) } }
                var ends: [Int32] = [-1, -1]
                if messages != nil {
                    let (host, tool) = try FD.pair()
                    ends = [host, tool]
                    ends.forEach(FD.closeOnExec)
                }
                defer { if ends[1] >= 0 { close(ends[1]) } }
                var fds = [(null, Int32(0)), (null, Int32(1)), (out >= 0 ? out : null, Int32(2))]
                if ends[1] >= 0 { fds.append((ends[1], 3)) }
                let pid: pid_t
                do {
                    pid = try Spawn.run(path, args, env: env, cwd: "/", suspended: true, fds: fds, access: access)
                    if let why = Spawn.release(pid, check: { check(pid) }) { throw Spawn.Refused(why: why) }
                } catch {
                    if ends[0] >= 0 { close(ends[0]) }
                    throw error
                }
                if let messages, ends[0] >= 0 { Self.read(ends[0], messages, requests) }
                return pid
            }
            DispatchQueue.main.async { self?.spawned(result) }
        }
    }

    /// 读到程序退出（另一头都关了）。带 id 的是请求，就地答。答话也在这个线程上写：工具发了请求就要把答复读走，
    /// 不读的话写满缓冲区，这个线程就卡住，后面的 play、up 也转不过来（现在只有起来时一来一回的 hello）
    private static func read(_ fd: Int32, _ messages: @escaping ([String: Any]) -> Void,
                             _ requests: @escaping ([String: Any]) -> [String: Any]) {
        let t = Thread {
            let lines = FD.Lines(fd, limit: 1 << 16)
            while let line = lines.next() {
                guard let m = (try? JSONSerialization.jsonObject(with: line)) as? [String: Any] else { continue }
                if let id = m["id"] {
                    _ = FD.writeLine(fd, ["id": id, "result": requests(m)])
                    continue
                }
                DispatchQueue.main.async { messages(m) }
            }
            close(fd)
        }
        t.name = "helper-host"
        t.start()
    }

    /// 描述符 3 上的请求怎么答：hello 答 ok，别的不认
    static func answer(_ request: [String: Any]) -> [String: Any] {
        request["method"] as? String == "hello" ? ["status": "ok"] : ["status": "failed", "why": "不认识的请求"]
    }

    private func spawned(_ result: Result<pid_t, Error>) {
        starting = false
        switch result {
        case .success(let pid):
            let child = SpawnedChild(pid: pid) { [weak self] c, reason, status in
                DispatchQueue.main.async { self?.exited(c, reason, status) }
            }
            process = child
            if stopped { halt(child) }           // 核的时候用户停用了
            else if restartWhenSpawned { restartWhenSpawned = false; restart() }
        case .failure(let e as Spawn.Refused):
            NSLog("%@ 没起：%@", program.lastPathComponent, e.why)
        case .failure(let e):
            NSLog("%@ 起不来：%@", program.lastPathComponent, e.localizedDescription)
            scheduleRestart()
        }
    }

    /// 往它的 stdin 写一行。还没起、已经退了就丢掉
    func send(_ message: [String: Any]) {
        pipe?.send(message)
    }

    private func gone() {
        process = nil
        pipe?.close()
        pipe = nil
        onExit?()
    }

    private func exited(_ p: HelperChild, _ reason: Process.TerminationReason, _ status: Int32) {
        guard process === p else { return }      // 已经 stop 过的上一个
        gone()
        if stopped { return }
        switch (reason, status) {
        case (.exit, 0):                         // 有人请它退出的（SIGTERM），不是出了事：下次启动螺壳时才起
            NSLog("%@ 自己退出了", program.lastPathComponent)
        case (.exit, Self.alreadyRunning):
            retryLater(options.alreadyRunningDelay)
        default:                                 // 崩了、被杀了，或者读文件卡住退出（75）
            NSLog("%@ 退出了（%d），重起", program.lastPathComponent, status)
            scheduleRestart()
        }
    }

    /// 十分钟内最多重起 3 次，免得循环（V1 §1 常驻配件的规矩）
    private func scheduleRestart() {
        restarts = restarts.filter { Date().timeIntervalSince($0) < options.restartWindow }
        if restarts.count >= options.maxRestarts {
            NSLog("%@ 十分钟内退出 %d 次，不再重起", program.lastPathComponent, restarts.count)
            return
        }
        restarts.append(Date())
        retryLater(options.restartDelay)
    }

    private func retryLater(_ delay: TimeInterval) {
        retry?.cancel()
        let work = DispatchWorkItem { [weak self] in self?.launch() }
        retry = work
        DispatchQueue.main.asyncAfter(deadline: .now() + delay, execute: work)
    }

    /// 先 SIGTERM，等它自己收尾，还不退就 SIGKILL。之后不再自动起，等下一次 start
    func stop() {
        stopped = true
        retry?.cancel()
        guard let p = process else { return }
        halt(p)
    }

    private func halt(_ p: HelperChild) {
        if p.isRunning {
            p.terminate()
            let deadline = Date().addingTimeInterval(options.graceSeconds)
            while p.isRunning && Date() < deadline { usleep(50_000) }
            if p.isRunning {
                p.forceKill()
                let gone = Date().addingTimeInterval(2)       // 卡在挂死的网络卷上时 SIGKILL 也不马上生效，不能一直占着主线程
                while p.isRunning && Date() < gone { usleep(50_000) }
            }
        }
        self.gone()
    }

    /// 占着位置的旧进程：路径就是这一份的；或者同名、已经没有父进程的（包被换走、挪走以后路径变了）。只看本账户的。
    /// 别的 .app 里同名、父进程还在的不是我们的（开发版和正式版可以同时跑）
    static func stale(path: String, name: String, marker: String? = nil, me: pid_t = getpid()) -> [pid_t] {
        let uid = getuid()
        let orphan = marker == nil ? "/Contents/Helpers/" + name : "/Contents/Resources/python/bin/" + name
        return Procs.pids().filter { pid in
            guard pid != me, let info = Procs.info(pid), info.uid == uid, let exe = Procs.exePath(pid) else { return false }
            if info.ppid == me { return false }
            guard exe == path || (info.ppid == 1 && exe.hasSuffix(orphan)) else { return false }
            let argv = Procs.argv(pid)
            return argv.contains("--parent-pid") && (marker.map(argv.contains) ?? true)    // 本体起的才带这个；用户自己跑的命令行不算
        }
    }

    static func clearStale(path: String, name: String, marker: String? = nil, grace: TimeInterval) -> Int {
        let pids = stale(path: path, name: name, marker: marker)
        end(pids, grace: grace)
        return pids.count
    }

    /// SIGTERM，等 grace 秒，还在就 SIGKILL
    static func end(_ pids: [pid_t], grace: TimeInterval) {
        for p in pids { kill(p, SIGTERM) }
        let deadline = Date().addingTimeInterval(grace)
        var alive = pids
        while !alive.isEmpty && Date() < deadline {
            usleep(100_000)
            alive = alive.filter { kill($0, 0) == 0 }
        }
        for p in alive { kill(p, SIGKILL) }
    }

    private static func log(_ url: URL) -> FileHandle {
        try? FileManager.default.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
        if !FileManager.default.fileExists(atPath: url.path) {
            FileManager.default.createFile(atPath: url.path, contents: nil, attributes: [.posixPermissions: 0o600])
        }
        guard let h = FileHandle(forWritingAtPath: url.path) else { return FileHandle.nullDevice }
        h.seekToEndOfFile()
        return h
    }
}

/// 重起时要等新进程起来的活。round 是 HelperProcess.environmentChanged 给的：这一轮或更新的进程说起来了才放行，要被换掉的进程说的不算。
/// 定时器跟着一轮走，轮到下一轮就不算数，这时留下的活归下一轮。起不来也不一直等。只在主线程上用
final class RestartGate {
    private let limit: TimeInterval
    private let after: (TimeInterval, @escaping () -> Void) -> Void
    private var round: Int?
    private var waiting: [() -> Void] = []

    init(limit: TimeInterval = 20,
         after: @escaping (TimeInterval, @escaping () -> Void) -> Void = { DispatchQueue.main.asyncAfter(deadline: .now() + $0, execute: $1) }) {
        self.limit = limit
        self.after = after
    }

    func restarting(_ round: Int) {
        self.round = round
        after(limit) { [weak self] in
            if self?.round == round { self?.open() }
        }
    }

    func whenUp(_ then: @escaping () -> Void) {
        if round == nil { then() } else { waiting.append(then) }
    }

    /// 起的时候是第 `process` 轮的进程说它起来了
    func up(from process: Int) {
        if let round, process >= round { open() }
    }

    private func open() {
        round = nil
        let run = waiting
        waiting = []
        run.forEach { $0() }
    }
}

/// 和子进程之间的管道：它写的一行一个 JSON，读到就交到主线程；写的时候它已经退了也不要紧
private final class HelperPipe {
    private static let longest = 1 << 20          // 通知这种小东西，超了当它坏了
    private let read: FileHandle
    private let write: FileHandle
    private var buffer = Data()                   // 只在读的队列上碰

    init(read: FileHandle, write: FileHandle, onMessage: @escaping ([String: Any]) -> Void) {
        self.read = read
        self.write = write
        _ = fcntl(write.fileDescriptor, F_SETNOSIGPIPE, 1)
        read.readabilityHandler = { [weak self] handle in
            let data = handle.availableData
            if data.isEmpty { handle.readabilityHandler = nil; return }
            self?.take(data, onMessage)
        }
    }

    private func take(_ data: Data, _ onMessage: @escaping ([String: Any]) -> Void) {
        buffer.append(data)
        while let end = buffer.firstIndex(of: 0x0A) {
            let line = buffer.subdata(in: buffer.startIndex..<end)
            buffer.removeSubrange(buffer.startIndex...end)
            if let o = try? JSONSerialization.jsonObject(with: line) as? [String: Any] {
                DispatchQueue.main.async { onMessage(o) }
            }
        }
        if buffer.count > Self.longest { buffer.removeAll() }
    }

    func send(_ message: [String: Any]) {
        guard var line = try? JSONSerialization.data(withJSONObject: message) else { return }
        line.append(0x0A)
        try? write.write(contentsOf: line)
    }

    func close() {
        read.readabilityHandler = nil
        try? write.close()
        try? read.close()
    }
}
