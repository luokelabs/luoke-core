// 拉起和看住主控进程：自带的 Python 跑 luoke_core，主控环境在 ~/Library/Application Support/luoke-core（实验室构建另一个目录）。
import Foundation

enum AppInfo {
    static let version: String = Bundle.main.infoDictionary?["CFBundleVersion"] as? String ?? "0.1.0.dev1"   // 主控版本号；CFBundleShortVersionString 是给人看的发布号（build_app.sh）
    static let isLab: Bool = (Bundle.main.bundleIdentifier ?? "").hasSuffix(".lab") || ProcessInfo.processInfo.environment["LUOKE_LAB"] == "1"
    /// 开发版（ai.luoke.core.dev，10-23）：自己的名字、自己的数据目录，和装着的正式版互不相干
    static let isDev: Bool = (Bundle.main.bundleIdentifier ?? "").hasSuffix(".dev")
    /// 包名（10-23：不叫「螺壳主控」了，就叫「螺壳」）；internal-1026 之前装的是 legacyName，升级时换成新名字
    static let appName: String = isDev ? "螺壳（开发）" : "螺壳"
    static let legacyName: String = isDev ? "螺壳主控（开发）" : "螺壳主控"
    static let envDir: URL = {
        let base = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask).first!
        return base.appendingPathComponent(isLab ? "luoke-core-lab" : (isDev ? "luoke-core-dev" : "luoke-core"), isDirectory: true)
    }()
    static let socketPath: String = envDir.appendingPathComponent("run/hub.sock").path
    static let resources: URL = Bundle.main.resourceURL ?? URL(fileURLWithPath: ".")
    /// 自带 Python；开发时没打包成 .app，用环境变量 LUOKE_PYTHON 指一个能跑 luoke_core 的解释器。
    static var python: String {
        if let p = ProcessInfo.processInfo.environment["LUOKE_PYTHON"] { return p }
        return resources.appendingPathComponent("python/bin/python3").path
    }
    static var recipe: String {
        if let r = ProcessInfo.processInfo.environment["LUOKE_RECIPE"] { return r }
        return resources.appendingPathComponent("recipe.json").path
    }
    static var modelDir: String { envDir.appendingPathComponent("models").path }
    /// 发布清单：开发时用 LUOKE_RELEASE 指一份；打包时放 Resources/release.json（没有就退回放符号链接）。
    static var release: String {
        if let r = ProcessInfo.processInfo.environment["LUOKE_RELEASE"] { return r }
        return resources.appendingPathComponent("release.json").path
    }
    /// 开发时 luoke_core 包的位置（打包后在自带 Python 的 site-packages 里，不用设）。
    static var pythonPath: String? { ProcessInfo.processInfo.environment["LUOKE_CORE_PATH"] }
}

final class HubProcess {
    private var process: Process?
    private(set) var lastExit: Int32?
    var onExit: ((Int32) -> Void)?

    var isRunning: Bool { process?.isRunning ?? false }

    func start() throws {
        if isRunning { return }
        try FileManager.default.createDirectory(at: AppInfo.envDir.appendingPathComponent("run"), withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
        try FileManager.default.createDirectory(at: AppInfo.envDir.appendingPathComponent("logs"), withIntermediateDirectories: true)
        try FileManager.default.createDirectory(atPath: AppInfo.modelDir, withIntermediateDirectories: true)
        let p = Process()
        p.executableURL = URL(fileURLWithPath: AppInfo.python)
        var args = ["-B", "-m", "luoke_core", "serve", "--recipe", AppInfo.recipe, "--model-dir", AppInfo.modelDir,
                    "--env", AppInfo.envDir.path, "--socket", AppInfo.socketPath]
        if FileManager.default.fileExists(atPath: AppInfo.release) { args += ["--release", AppInfo.release] }
        args.append("--parent-watch")   // 本体没了（哪怕被 kill），主控几秒内自己退出并停掉引擎
        if AppInfo.isLab { args.append("--lab") }
        p.arguments = args
        var env = ["PATH": "/usr/bin:/bin", "HOME": NSHomeDirectory(), "PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1"]
        if let pp = AppInfo.pythonPath { env["PYTHONPATH"] = pp }
        if let lh = ProcessInfo.processInfo.environment["LUOKE_HOME"] { env["LUOKE_HOME"] = lh }
        if let hf = ProcessInfo.processInfo.environment["HF_HOME"] { env["HF_HOME"] = hf }   // 复用本机模型时找自定义位置的 HF 缓存（审查 2026-10-17）
        p.environment = env
        p.currentDirectoryURL = URL(fileURLWithPath: "/")
        let log = FileHandle(forWritingAtPath: AppInfo.envDir.appendingPathComponent("logs/app.log").path)
            ?? { FileManager.default.createFile(atPath: AppInfo.envDir.appendingPathComponent("logs/app.log").path, contents: nil)
                 return FileHandle(forWritingAtPath: AppInfo.envDir.appendingPathComponent("logs/app.log").path)! }()
        log.seekToEndOfFile()
        p.standardOutput = log
        p.standardError = log
        p.terminationHandler = { [weak self] proc in
            self?.lastExit = proc.terminationStatus
            self?.onExit?(proc.terminationStatus)
        }
        try p.run()
        process = p
    }

    func stop() {
        guard let p = process, p.isRunning else { return }
        p.terminate()   // SIGTERM：主控自己会停引擎、删 socket
        let deadline = Date().addingTimeInterval(40)
        while p.isRunning && Date() < deadline { Thread.sleep(forTimeInterval: 0.2) }
        if p.isRunning { kill(p.processIdentifier, SIGKILL) }
    }
}
