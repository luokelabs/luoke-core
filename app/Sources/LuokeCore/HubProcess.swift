// 拉起和看住主控进程：自带的 Python 跑 luoke_core，主控环境在 ~/Library/Application Support/luoke-core（实验室构建另一个目录）。
import Foundation

enum AppInfo {
    static let version: String = Bundle.main.infoDictionary?["CFBundleVersion"] as? String ?? "0.1.0.dev1"   // 主控版本号；CFBundleShortVersionString 是给人看的发布号（build_app.sh）
    static let isLab: Bool = labBuild(bundleID: Bundle.main.bundleIdentifier ?? "", environment: ProcessInfo.processInfo.environment)
    /// 实验室构建按 bundle id 认；LUOKE_LAB=1 只在开发时认（没打包成 .app、开发版）。正式版认了的话，同一个用户的任何程序
    /// `launchctl setenv` 一下，螺壳下次就带 --lab 起主控：装配件不核签名、socket 谁都放行
    static func labBuild(bundleID: String, environment: [String: String]) -> Bool {
        bundleID.hasSuffix(".lab") || (overridesAllowed(bundleID: bundleID) && environment["LUOKE_LAB"] == "1")
    }
    /// 开发版（ai.luoke.core.dev）：自己的名字和 bundle id，数据和正式版是同一份（usesRealData）
    static let isDev: Bool = (Bundle.main.bundleIdentifier ?? "").hasSuffix(".dev")
    /// 开发实例（docs/dev-instance.md）：bundle id `ai.luoke.core.<名字>.lab` 的实验室构建，自己的名字，
    /// 数据、环境目录、socket 都和装着的螺壳是同一份，同一时间只开一个。普通的实验室构建 ai.luoke.core.lab 不算
    static let instance: String? = instanceName(bundleID: Bundle.main.bundleIdentifier ?? "")
    static func instanceName(bundleID id: String) -> String? {
        let prefix = "ai.luoke.core.", suffix = ".lab"
        guard id.hasPrefix(prefix), id.hasSuffix(suffix), id.count > prefix.count + suffix.count else { return nil }
        let name = String(id.dropFirst(prefix.count).dropLast(suffix.count))
        return name.range(of: "^[a-z0-9][a-z0-9-]*$", options: .regularExpression) != nil ? name : nil
    }
    /// 包名（10-23：不叫「螺壳主控」了，就叫「螺壳」）；internal-1026 之前装的是 legacyName，升级时换成新名字。
    /// 开发实例带上名字（build_app.sh 同样起名）：菜单、程序切换器里分得出来；升级、安装按名字找位置，也不会落到本机在用的「螺壳.app」上
    static let appName: String = instance.map { "螺壳（\($0)）" } ?? (isDev ? "螺壳（开发）" : "螺壳")
    static let legacyName: String = instance.map { "螺壳主控（\($0)）" } ?? (isDev ? "螺壳主控（开发）" : "螺壳主控")
    static let envDir: URL = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask).first!
        .appendingPathComponent(envDirName(realData: usesRealData), isDirectory: true)
    /// 用真实数据的（正式版、开发版、开发实例）都用 luoke-core：主控的状态、模型、日志、socket 只有一份，输入法这些自己起的程序
    /// 按这个位置找主控，开发实例顶替装着的螺壳时它们照样找得到（11-14 夜里输入法找不到开发实例的主控）。只有普通的实验室构建另放
    static func envDirName(realData: Bool) -> String { realData ? "luoke-core" : "luoke-core-lab" }
    /// 用不用本机用户的真实数据（记忆、agent、邮件助手、搜索、扫描的数据，装应用的位置，主控自己的环境目录）：正式版、开发版、
    /// 开发实例都用，本机开发不另起一套（docs/dev-instance.md）。只有普通的实验室构建（ai.luoke.core.lab，评测和自动化测试用）用自己的
    static let usesRealData: Bool = !isLab || instance != nil
    /// 同一个环境目录里已经有主控在应答（另一个螺壳开着）：socket 连得上就是。连不上（没有、或者是上次留下的）不算
    static func hubAnswering(at path: String) -> Bool {
        let fd = socket(AF_UNIX, SOCK_STREAM, 0)
        guard fd >= 0 else { return false }
        defer { close(fd) }
        var addr = sockaddr_un()
        addr.sun_family = sa_family_t(AF_UNIX)
        let bytes = Array(path.utf8)
        guard bytes.count < MemoryLayout.size(ofValue: addr.sun_path) else { return false }
        withUnsafeMutableBytes(of: &addr.sun_path) { buf in
            for (i, b) in bytes.enumerated() { buf[i] = b }
        }
        let len = socklen_t(MemoryLayout<sockaddr_un>.size)
        return withUnsafePointer(to: &addr) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) { connect(fd, $0, len) == 0 }
        }
    }
    static let socketPath: String = envDir.appendingPathComponent("run/hub.sock").path
    static let resources: URL = Bundle.main.resourceURL ?? URL(fileURLWithPath: ".")
    /// 换主控代码和配方的覆盖（LUOKE_PYTHON、LUOKE_CORE_PATH、LUOKE_RECIPE、LUOKE_RELEASE）只有开发时认：没打包成 .app 的、开发版、实验室构建（按 bundle id，不看
    /// LUOKE_LAB 这类环境变量）。正式版带着完全磁盘访问，认了的话同一个用户的任何程序 `launchctl setenv` 一下，
    /// 下次螺壳就替它跑任意 Python（扫描那边审查 11-05 提的）
    static var allowsCodeOverrides: Bool { overridesAllowed(bundleID: Bundle.main.bundleIdentifier ?? "") }
    static func overridesAllowed(bundleID id: String) -> Bool { id.isEmpty || id.hasSuffix(".dev") || id.hasSuffix(".lab") }
    /// 开发、测试用的环境变量（换更新源、允许任意位置的副本、不当安装器）：和上面一样只有开发时认，正式版一律当没设
    static func devEnvironment(_ key: String) -> String? { allowsCodeOverrides ? ProcessInfo.processInfo.environment[key] : nil }
    /// 自带 Python；开发时没打包成 .app，用环境变量 LUOKE_PYTHON 指一个能跑 luoke_core 的解释器（正式版不认）。
    static var python: String {
        if allowsCodeOverrides, let p = ProcessInfo.processInfo.environment["LUOKE_PYTHON"] { return p }
        return resources.appendingPathComponent("python/bin/python3").path
    }
    static var recipe: String {
        if allowsCodeOverrides, let r = ProcessInfo.processInfo.environment["LUOKE_RECIPE"] { return r }
        return resources.appendingPathComponent("recipe.json").path
    }
    static var modelDir: String { envDir.appendingPathComponent("models").path }
    /// 发布清单：开发时用 LUOKE_RELEASE 指一份（正式版不认）；打包时放 Resources/release.json（没有就退回放符号链接）。
    static var release: String {
        if allowsCodeOverrides, let r = ProcessInfo.processInfo.environment["LUOKE_RELEASE"] { return r }
        return resources.appendingPathComponent("release.json").path
    }
    /// 开发时 luoke_core 包的位置（打包后在自带 Python 的 site-packages 里，不用设；正式版不认，它会变成 PYTHONPATH）。
    static var pythonPath: String? { allowsCodeOverrides ? ProcessInfo.processInfo.environment["LUOKE_CORE_PATH"] : nil }
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
        var args = ["-B", "-m", "luoke_core", "serve", "--recipe", AppInfo.recipe, "--model-dir", AppInfo.modelDir,
                    "--env", AppInfo.envDir.path, "--socket", AppInfo.socketPath]
        if FileManager.default.fileExists(atPath: AppInfo.release) { args += ["--release", AppInfo.release] }
        // 打进螺壳.app 的螺壳引擎：配方要的是这一版时主控就用它，不下载引擎包
        if let e = Manifests.bundledEngine() { args += ["--bundled-engine", e.path, "--bundled-engine-version", e.version] }
        args.append("--parent-watch")   // 本体没了（哪怕被 kill），主控几秒内自己退出并停掉引擎
        if AppInfo.isLab { args.append("--lab") }
        if AppInfo.isLab && AppInfo.usesRealData { args.append("--real-data") }     // 开发实例：实验室构建，但用真实数据、装到真实的位置
        // disclaim：主控在包里，自己照样算螺壳；挡的是它起的包外程序（引擎等，那边由主控再 disclaim 一次，见 Spawn.swift）
        let p = Spawn.process(AppInfo.python, args)
        var env = ["PATH": "/usr/bin:/bin", "HOME": NSHomeDirectory(), "PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1"]
        if let pp = AppInfo.pythonPath { env["PYTHONPATH"] = pp }
        if let lh = ProcessInfo.processInfo.environment["LUOKE_HOME"] { env["LUOKE_HOME"] = lh }
        if let hf = ProcessInfo.processInfo.environment["HF_HOME"] { env["HF_HOME"] = hf }   // 复用本机模型时找自定义位置的 HF 缓存（审查 2026-10-17）
        // 环境目录照实告诉主控：换下来的旧版配件放 <环境目录>/previous.noindex（accessories.py 没给时按正式版的目录算，开发版、实验室会放错地方）
        env["LUOKE_ENV_DIR"] = AppInfo.envDir.path
        // 复用本机模型的位置（开发实例从本机在用的螺壳的模型克隆，只读、不占空间）：和别的覆盖一样只有开发时认
        if let roots = AppInfo.devEnvironment("LUOKE_LOCAL_MODEL_ROOTS") { env["LUOKE_LOCAL_MODEL_ROOTS"] = roots }
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
