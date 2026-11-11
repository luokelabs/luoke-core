// 螺壳记忆并进主控（顶层 ADR 0014，2026-10-16 定进 beta1；V1 §6）：记忆的代码和清单在主控 .app 里，
// 界面是单独的进程（主控自带的 Python 跑清单的 entry），主控只负责起它、在升级 / 卸载前请它退出。主控本身不读记忆。
import AppKit

enum Memory {
    static var manifestURL: URL { Bundle.main.bundleURL.appendingPathComponent("Contents/Resources/memory/luoke-accessory.json") }
    static var python: String { Bundle.main.bundleURL.appendingPathComponent("Contents/Resources/python/bin/python3").path }
    static var bundled: Bool { FileManager.default.fileExists(atPath: manifestURL.path) }

    static func manifest() -> [String: Any]? {
        guard let d = FileManager.default.contents(atPath: manifestURL.path) else { return nil }
        return (try? JSONSerialization.jsonObject(with: d)) as? [String: Any]
    }

    /// 打开记忆界面。带主窗口的这一份翻到主窗口的「记忆」页（一条理解的编号翻到那一条），不开浏览器（11-10：只有一个窗口）；
    /// 没带的起 `python3 -B <entry> [--note <编号>]`，起完不管（单实例、开浏览器由 luoke_mem 自己保证）
    static func open(note: String? = nil, view: String? = nil) {
        if MainWindow.presenter != nil { MainWindow.openMemory(note: note, view: view); return }
        if let why = CodeSigning.bundleProblem { alert("记忆打不开", "\(why)，请重新安装螺壳。"); return }
        if AppInfo.isLab && ProcessInfo.processInfo.environment["LUOKE_HOME"] == nil {
            // 实验室构建不碰本机用户在用的 ~/.luoke/mem.db（审查 2026-10-17；顶层 CLAUDE.md"测试环境拒绝真实目录"）
            alert("实验室构建没有指定记忆的位置", "设 LUOKE_HOME 指向测试用的目录再打开。")
            return
        }
        guard let m = manifest(), let entry = m["entry"] as? [String], !entry.isEmpty else { return }   // 安装包里一定带记忆，不弹窗
        let args = entryArguments(entry, note: note, view: view, version: m["version"] as? String)
        let p = Spawn.process(python, args)                 // disclaim（Spawn.swift）：记忆界面是本机 HTTP 服务，它起的程序不借螺壳的授权
        var env = ["PATH": "/usr/bin:/bin", "HOME": NSHomeDirectory(), "PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1",
                   "LUOKE_HUB_SOCKET": AppInfo.socketPath]
        if let lh = ProcessInfo.processInfo.environment["LUOKE_HOME"] { env["LUOKE_HOME"] = lh }
        p.environment = env
        p.currentDirectoryURL = URL(fileURLWithPath: "/")
        p.standardInput = FileHandle.nullDevice
        p.standardOutput = FileHandle.nullDevice
        p.standardError = FileHandle.nullDevice
        do { try p.run() } catch {
            NSLog("打开螺壳记忆失败：%@", error.localizedDescription)
            alert("螺壳记忆打不开", error.localizedDescription)
        }
    }

    /// `app --view people` 从 luoke-mem 0.36.1 起才认，更旧的会被 argparse 拒掉、什么都不开
    static let peopleSince = "0.36.1"

    /// 没带主窗口时起界面的参数：`-B <entry> [--note <编号> | --view recent|people]`，两个都带时以 note 为准（luoke-mem 0.16.0，ADR 0011）。
    /// version 是带着的记忆清单的版本，不认 people 的打开首页
    static func entryArguments(_ entry: [String], note: String?, view: String?, version: String?) -> [String] {
        var args = ["-B"] + entry
        if let n = note, !n.isEmpty { args += ["--note", n] }
        else if view == "recent" { args += ["--view", "recent"] }
        else if view == "people", let v = version, v.compare(peopleSince, options: .numeric) != .orderedAscending { args += ["--view", "people"] }
        return args
    }

    private static func alert(_ title: String, _ text: String) {
        // 一个窗口的例外：只从 open 里没带主窗口的那条路来（带主窗口的不起这个界面）
        let a = NSAlert()
        a.messageText = title
        a.informativeText = text
        a.runModal()
    }

    /// luoke://memory[?note=<编号>|?view=recent|?view=people]
    static func handle(_ url: URL) -> Bool {
        guard url.scheme == "luoke", url.host == "memory", url.path.isEmpty || url.path == "/" else { return false }
        var note = URLComponents(url: url, resolvingAgainstBaseURL: false)?.queryItems?.first(where: { $0.name == "note" })?.value
        if let n = note, n.isEmpty || !n.allSatisfy({ $0.isASCII && $0.isNumber }) { note = nil }   // 只放行纯数字的编号
        let view = URLComponents(url: url, resolvingAgainstBaseURL: false)?.queryItems?.first(where: { $0.name == "view" })?.value
        open(note: note, view: view == "recent" || view == "people" ? view : nil)      // view 只认 recent、people，别的值忽略
        return true
    }

    /// 某个主控 .app 自带 Python 跑的记忆进程（界面服务、learn、forget-tool）的 pid：只按命令行含 `-m luoke_mem` 认，
    /// 不按"可执行文件在主控 .app 里"认——输入法的个人化组件也跑在主控的 Python 上（顶层 ADR 0014）
    static func processes(hubApp: URL) -> [pid_t] {
        // agent 核心（luoke-core#68）也跑在这份 Python 上：升级、卸载前一起请它退出（本体退出时它自己也会退）
        Procs.runningModule(app: hubApp, module: "luoke_mem") + Procs.runningModule(app: hubApp, module: "luoke_agent")
    }

    /// 请它们退出：SIGTERM，10 秒不退再 SIGKILL（升级、卸载全部之前）
    static func stopAll(hubApp: URL) {
        let pids = processes(hubApp: hubApp)
        guard !pids.isEmpty else { return }
        pids.forEach { kill($0, SIGTERM) }
        let deadline = Date().addingTimeInterval(10)
        var alive = pids
        while !alive.isEmpty && Date() < deadline {
            Thread.sleep(forTimeInterval: 0.2)
            alive = alive.filter { kill($0, 0) == 0 }
        }
        alive.forEach { kill($0, SIGKILL) }
        NSLog("螺壳记忆：结束了 %d 个进程（%d 个是强制的）", pids.count, alive.count)
    }
}
