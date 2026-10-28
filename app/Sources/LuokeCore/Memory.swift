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

    /// 打开记忆界面：起 `python3 -B <entry> [--note <编号>]`，起完不管（单实例、开浏览器由 luoke_mem 自己保证）
    static func open(note: String? = nil, view: String? = nil) {
        if AppInfo.isLab && ProcessInfo.processInfo.environment["LUOKE_HOME"] == nil {
            // 实验室构建不碰本机用户在用的 ~/.luoke/mem.db（审查 2026-10-17；顶层 CLAUDE.md"测试环境拒绝真实目录"）
            alert("实验室构建没有指定记忆的位置", "设 LUOKE_HOME 指向测试用的目录再打开。")
            return
        }
        guard let entry = manifest()?["entry"] as? [String], !entry.isEmpty else { return }   // 安装包里一定带记忆，不弹窗
        let p = Process()
        p.executableURL = URL(fileURLWithPath: python)
        var args = ["-B"] + entry
        if let n = note, !n.isEmpty { args += ["--note", n] } else if view == "recent" { args += ["--view", "recent"] }   // 两个都带时以 note 为准（luoke-mem 0.16.0，ADR 0011）
        p.arguments = args
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

    private static func alert(_ title: String, _ text: String) {
        let a = NSAlert()
        a.messageText = title
        a.informativeText = text
        a.runModal()
    }

    /// luoke://memory[?note=<编号>]
    static func handle(_ url: URL) -> Bool {
        guard url.scheme == "luoke", url.host == "memory", url.path.isEmpty || url.path == "/" else { return false }
        var note = URLComponents(url: url, resolvingAgainstBaseURL: false)?.queryItems?.first(where: { $0.name == "note" })?.value
        if let n = note, n.isEmpty || !n.allSatisfy({ $0.isASCII && $0.isNumber }) { note = nil }   // 只放行纯数字的编号
        let view = URLComponents(url: url, resolvingAgainstBaseURL: false)?.queryItems?.first(where: { $0.name == "view" })?.value
        open(note: note, view: view == "recent" ? "recent" : nil)      // view 只认 recent，别的值忽略
        return true
    }

    /// 某个主控 .app 自带 Python 跑的记忆进程（界面服务、learn、forget-tool）的 pid：只按命令行含 `-m luoke_mem` 认，
    /// 不按"可执行文件在主控 .app 里"认——输入法的个人化组件也跑在主控的 Python 上（顶层 ADR 0014）
    static func processes(hubApp: URL) -> [pid_t] {
        Procs.runningModule(app: hubApp, module: "luoke_mem")
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
