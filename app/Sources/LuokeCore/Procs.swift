import Darwin
import Foundation

/// 按内核给的事实认进程（和 luoke_core/procs.py 同一个做法）：可执行文件的真实路径（proc_pidpath）、原样的 argv（KERN_PROCARGS2）。
/// 不解析 ps 的文字：从 launchd 起的主控没有 UTF-8 locale，ps 把中文 .app 路径印成转义（10-19 开发机上，升级没请走旧的记忆进程）。
enum Procs {
    static func pids() -> [pid_t] {
        let n = proc_listallpids(nil, 0)
        guard n > 0 else { return [] }
        var buf = [pid_t](repeating: 0, count: Int(n) + 64)
        let got = buf.withUnsafeMutableBytes { proc_listallpids($0.baseAddress, Int32($0.count)) }
        return Array(buf.prefix(max(0, Int(got)))).filter { $0 > 0 }
    }

    static func exePath(_ pid: pid_t) -> String? {
        var buf = [CChar](repeating: 0, count: 4096)
        let n = proc_pidpath(pid, &buf, UInt32(buf.count))
        return n > 0 ? String(cString: buf) : nil
    }

    /// 进程启动时的 argv；读不到就是 []
    static func argv(_ pid: pid_t) -> [String] {
        var mib: [Int32] = [CTL_KERN, KERN_ARGMAX]
        var argmax: Int32 = 0
        var size = MemoryLayout<Int32>.size
        guard sysctl(&mib, 2, &argmax, &size, nil, 0) == 0, argmax > 0 else { return [] }
        var buf = [UInt8](repeating: 0, count: Int(argmax))
        mib = [CTL_KERN, KERN_PROCARGS2, pid]
        size = buf.count
        guard sysctl(&mib, 3, &buf, &size, nil, 0) == 0, size > 4 else { return [] }
        let argc = Int(buf.withUnsafeBytes { $0.load(as: Int32.self) })
        var i = 4
        while i < size && buf[i] != 0 { i += 1 }        // 可执行文件路径
        while i < size && buf[i] == 0 { i += 1 }        // 填充
        var out: [String] = []
        var start = i
        while i < size && out.count < argc {
            if buf[i] == 0 {
                out.append(String(decoding: buf[start..<i], as: UTF8.self))
                start = i + 1
            }
            i += 1
        }
        return out
    }

    static func inside(_ path: String?, _ app: URL) -> Bool {
        guard let path else { return false }
        let roots = Set([app.standardizedFileURL.path, app.resolvingSymlinksInPath().path].map { $0.hasSuffix("/") ? $0 : $0 + "/" })
        return roots.contains { path.hasPrefix($0) }
    }

    /// 用这个 .app 里的解释器跑着 `-m <module>` 的进程，不含自己
    static func runningModule(app: URL, module: String) -> [pid_t] {
        let me = ProcessInfo.processInfo.processIdentifier
        return pids().filter { pid in
            guard pid != me, inside(exePath(pid), app) else { return false }
            let a = argv(pid)
            return zip(a, a.dropFirst()).contains { $0 == "-m" && $1 == module }
        }
    }
}
