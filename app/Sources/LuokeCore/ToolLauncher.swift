// 主控跑工具的空闲任务（luoke_core/jobs.py）：主控不直接起工具，起螺壳自己的这个模式，本体照 ToolHost 的办法起——
// 停着起、核过真正在跑的那个进程的签名再放行。核对只在本体一处，主控核完再起会有被换掉的空档。
//
//   螺壳 --run-tool <配件> <子命令> [--开关 ...]
//
// - 配件、程序、签名标识写死在 HostedTool.known；参数只能是那里 `jobs` 列的某一条，一字不差。
// - 程序是螺壳自己带的（Contents/Helpers，HostedTool.find）；用户在「应用」里开启了才起。核的和 ToolHost 一样：团队、Developer ID、
//   标识、和螺壳记的版本一致，核完它还停着、一条指令没跑过。
// - 工具的标准输入、输出、错误就是这个进程的（主控给的）。环境同 mcp：HOME、TMPDIR、PATH、LANG（开发版、实验室加 LUOKE_HUB_SOCKET）；
//   描述符 3 是本体这边一对 socketpair 的一端，不设 LUOKE_HOST_FD：工具只拿它认「是螺壳起的」（对面答话的就是父进程），
//   在上面请什么都答 failed（空闲任务不提权、不叫到前面）。工作目录 /。
// - 收到 SIGTERM、SIGINT、SIGHUP 转给工具（主控让它收尾）。主控硬杀时杀整个进程组（主控把这个进程放进自己的进程组，工具跟着它）。
// - 退出码：工具的照传，工具被信号结束是 128+信号；本体自己的错是 120 参数不对、121 没带或没开启、122 起不来、123 核不过。
import Darwin
import Foundation

enum ToolLauncher {
    static let flag = "--run-tool"
    static let badArguments: Int32 = 120
    static let notInstalled: Int32 = 121
    static let spawnFailed: Int32 = 122
    static let checkFailed: Int32 = 123

    /// 参数是写死的那几条之一，一字不差
    static func valid(_ args: [String], known: HostedTool.Known) -> Bool { known.jobs.contains(args) }

    /// waitpid 的状态换成退出码：正常退出照传，被信号结束是 128+信号
    static func exitCode(_ status: Int32) -> Int32 {
        let sig = status & 0x7f
        return sig == 0 ? (status >> 8) & 0xff : 128 + sig
    }

    static func main(_ args: [String]) -> Never { exit(run(args)) }

    /// 起工具、核、等它退出，返回退出码。known、find、enabled、check 换掉是给测试的
    static func run(_ args: [String], known: [String: HostedTool.Known] = HostedTool.known,
                    find: (String) -> HostedTool? = { HostedTool.find($0) },
                    enabled: (String) -> Bool = { HostedTool.enabled($0) },
                    check: (pid_t, HostedTool) -> String? = { CodeSigning.checkRunning(pid: $0, tool: $1) }) -> Int32 {
        func fail(_ code: Int32, _ why: String) -> Int32 {
            FileHandle.standardError.write(Data("螺壳 --run-tool：\(why)\n".utf8))
            return code
        }
        guard let accessory = args.first, let k = known[accessory], k.onDemand else { return fail(badArguments, "不认识这个工具") }
        let rest = Array(args.dropFirst())
        guard valid(rest, known: k) else { return fail(badArguments, "\(accessory) 的参数不对：\(rest.joined(separator: " "))") }
        guard let tool = find(accessory) else { return fail(notInstalled, "螺壳没有带 \(accessory)") }
        guard enabled(accessory) else { return fail(notInstalled, "\(accessory) 没有开启") }

        let signals = Forwarder()       // 先接住信号：起了停着的工具以后这个进程再被直接结束，工具就一直停着
        defer { signals.stop() }
        if let sig = signals.pending { return 128 + sig }
        // 标准输入、输出、错误先复制一份再映射到 0–2（posix_spawn 的 dup2 不用同号）
        let fds = ([0, 1, 2] as [Int32]).map { fd -> Int32 in
            let d = fcntl(fd, F_DUPFD_CLOEXEC, 3)
            return d >= 0 ? d : open("/dev/null", O_RDWR | O_CLOEXEC)
        }
        defer { fds.forEach { close($0) } }
        let hostSide: Int32, toolHost: Int32
        do { (hostSide, toolHost) = try FD.pair() } catch { return fail(spawnFailed, "\(accessory) 起不来：\(error)") }
        FD.closeOnExec(hostSide)
        let pid: pid_t
        do {
            defer { close(toolHost) }
            pid = try Spawn.run(tool.program.path, rest, env: HostedTool.environment(accessory), cwd: "/", suspended: true,
                                fds: [(fds[0], 0), (fds[1], 1), (fds[2], 2), (toolHost, 3)], access: .luoke)    // 扫描的空闲任务要用螺壳的授权
        } catch {
            close(hostSide)
            return fail(spawnFailed, "\(accessory) 起不来：\(error)")
        }
        let answered = refuseAll(hostSide)
        defer {
            shutdown(hostSide, SHUT_RDWR)       // 工具起的进程可能还拿着另一头：不等它们，读的那边马上读到结尾
            answered.wait()
            close(hostSide)
        }
        if let why = Spawn.release(pid, check: { check(pid, tool) }) {      // 核不过就已经结束、收掉了
            return fail(checkFailed, "\(accessory)：\(why)")
        }
        signals.started(pid)
        // 先等它退出但不收（WNOWAIT），在锁里记下「退了」再收：转发信号那边不会发给重用了这个 pid 的别的进程
        var info = siginfo_t()
        while waitid(P_PID, id_t(pid), &info, WEXITED | WNOWAIT) < 0 && errno == EINTR {}
        signals.exited()
        var status: Int32 = 0
        while waitpid(pid, &status, 0) < 0 && errno == EINTR {}
        return exitCode(status)
    }

    /// 描述符 3 的本体这一头：每读到一行带 id 的请求就答 failed。答话的是这个进程（工具的父进程），工具据此认出是螺壳起的
    static func refuseAll(_ fd: Int32) -> DispatchSemaphore {
        FD.answerAll(fd, name: "run-tool-fd3") { _ in ["status": "failed", "why": "空闲任务不代办"] }
    }

    /// SIGTERM、SIGINT、SIGHUP 不结束这个进程，转给工具；工具还没起时记下来
    final class Forwarder {
        private let lock = NSLock()
        private var pid: pid_t = 0          // 0 还没起，-1 已经退了
        private var first: Int32 = 0
        private var sources: [DispatchSourceSignal] = []

        init() {
            for sig in [SIGTERM, SIGINT, SIGHUP] {
                signal(sig, SIG_IGN)         // 工具那边 posix_spawn 把信号都恢复成默认
                let s = DispatchSource.makeSignalSource(signal: sig, queue: .global())
                s.setEventHandler { [weak self] in self?.got(sig) }
                s.resume()
                sources.append(s)
            }
        }

        var pending: Int32? {
            lock.lock(); defer { lock.unlock() }
            return first == 0 ? nil : first
        }

        private func got(_ sig: Int32) {
            lock.lock(); defer { lock.unlock() }
            if pid > 0 { kill(pid, sig) } else if pid == 0, first == 0 { first = sig }
        }

        /// 工具放行了：之前收到的转给它
        func started(_ p: pid_t) {
            lock.lock(); defer { lock.unlock() }
            pid = p
            if first != 0 { kill(p, first) }
        }

        func exited() {
            lock.lock(); defer { lock.unlock() }
            pid = -1
        }

        /// 不再转发，信号恢复默认（在测试里跑完要还原）
        func stop() {
            sources.forEach { $0.cancel() }
            for sig in [SIGTERM, SIGINT, SIGHUP] { signal(sig, SIG_DFL) }
        }
    }
}
