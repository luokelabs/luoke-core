// 螺壳搜索收进本体（ADR 0022）：程序在 Contents/Helpers/luoke-search，清单在 Resources/search。
// 本体当子进程起它，权限算在螺壳头上：Info.plist 的 7 条说明和 tools/entitlements-core.plist 都在外层，它自己签的不算。
// 它靠签名认自己（codesign -i ai.luoke.search，build_app.sh、release_sign.sh），没有这个 id 就起不来。
// 起的每一次都停着起，核过才放行，和扫描同一条路（ToolHost.swift）：常驻的走 HelperProcess.check，settings 走 Spawn.capture。
// 这一份没带它（开发版没给 LUOKE_SEARCH_SRC，或这一版打不出来）时这里什么都不做，装在 ~/Applications 的旧包照旧。
import AppKit
import UniformTypeIdentifiers
import Foundation
import Security

enum SearchHelper: BundledApp {
    static let accessory = "luoke-search"
    static let bundleID = "ai.luoke.search"
    private static let program = Bundle.main.bundleURL.appendingPathComponent("Contents/Helpers/luoke-search")
    private static var manifestURL: URL { AppInfo.resources.appendingPathComponent("search/luoke-accessory.json") }

    private static var object: [String: Any]? {
        guard let data = try? Data(contentsOf: manifestURL) else { return nil }
        return try? JSONSerialization.jsonObject(with: data) as? [String: Any]
    }

    static var bundled: Bool { FileManager.default.isExecutableFile(atPath: program.path) && object != nil }

    /// 清单。type 写 bundled：别处按 type == "app" 当 .app 处理的路子（打开、升级、通知）都走不到它
    static var manifest: Accessories.Manifest? {
        guard bundled, let o = object, Manifests.macOSSatisfies(o["min_macos"] as? String) else { return nil }   // 它要的系统比螺壳声称的新
        return Accessories.Manifest(accessory: accessory, bundleID: bundleID, name: o["name"] as? String ?? accessory,
                                    resident: o["resident"] as? Bool ?? true, type: "bundled", app: Bundle.main.bundleURL,
                                    version: o["version"] as? String ?? "", bundled: true)
    }

    /// 开发版、实验室构建带的是开发版程序（build_app.sh），用自己的数据和主控
    private static var developmentBuild: Bool { BundledApps.developmentBuild }
    /// 正式包设了 LUOKE_LAB：它的程序只认真实的数据目录，不起
    static var mayRun: Bool { developmentBuild || !AppInfo.isLab }

    private static func wanted() -> Bool {
        guard mayRun, let m = manifest, enabled else { return false }
        return Accessories.wantsResident(m)
    }

    private static var environment: [String: String] {
        var env = ["PATH": "/usr/bin:/bin", "HOME": NSHomeDirectory()]
        if developmentBuild {
            // 不在环境目录下：agent 和卸载只认和编号绑在一起的几个位置（accessories.py 的 _own_data 改清单改到这里）
            env["LUOKE_SEARCH_HOME"] = NSHomeDirectory() + "/Library/Application Support/luoke-search"
            env["LUOKE_HUB_SOCKET"] = AppInfo.socketPath
        }
        if let off = BundledApps.offValue(accessory) { env["LUOKE_PERMISSIONS_OFF"] = off }
        return env
    }

    private static let process: HelperProcess = {
        let p = HelperProcess(program: program, arguments: ["--resident", "--parent-pid", String(getpid())], environment: environment,
                              logFile: AppInfo.envDir.appendingPathComponent("logs/search.log"))
        p.wanted = wanted
        p.check = { HostedTool.check(accessory, pid: $0) }
        p.access = .luoke           // 读用户的文件要用螺壳的授权（完全磁盘访问、文件夹），不 disclaim（Spawn.swift）
        p.onHostMessage = { m, round in if m["up"] != nil { gate.up(from: round) } else { play(m) } }
        p.onExit = { token.clear() }              // 主线程上，总在下一个进程起之前（HelperProcess.launch 要等 process 为空）
        p.onHostRequest = { m, round in
            guard m["method"] as? String == "hello" else { return HelperProcess.answer(m) }
            guard let t = token.mint(round: round) else { return ["status": "failed", "why": "口令没生成"] }
            return ["status": "ok", "token": t]
        }
        return p
    }()

    /// 跑一次它的命令，`capture` 那一路读回来。描述符 3 给它认是螺壳起的（装好的搜索没有就不跑）
    private static func run(_ args: [String], capture fd: Int32) throws -> Spawn.Output {
        try Spawn.capture(program.path, args, env: environment.map { "\($0.key)=\($0.value)" }, capture: fd, access: .luoke, channel: true) { HostedTool.check(accessory, pid: $0) }
    }

    /// 主控起来以后（HubModel.startHub）、用户开启时。重复调不要紧；没开启时只挪旧的 .app
    static func start() {
        guard manifest != nil else { return }
        if oldAppGone { process.start(); return }
        guard !retiring else { return }
        retiring = true
        DispatchQueue.global().async {
            let hadOne = retireOldApp()
            DispatchQueue.main.async {
                oldAppGone = true
                retiring = false
                process.start()
                if hadOne && !enabled { askWhenFront() }      // 老用户：旧授权不跟过来，要重新开启
            }
        }
    }

    private static var retiring = false
    private static var oldAppGone = false

    /// 收进来以前单独装的 螺壳搜索.app：先结束，再挪进废纸篓，然后才起这一份，不然两份抢同一个数据目录。数据不动，这一份接着用。
    /// 用户没开启也做（旧的那份不该留着跑）。开发版、实验室不动：它们有自己的数据，本机装着的不归它们管。返回有没有旧的
    private static func retireOldApp() -> Bool {
        guard !developmentBuild, !AppInfo.isLab else { return false }
        return BundledApps.retireApps([bundleID], label: "螺壳搜索", grace: 15)         // 手上的活要做完，它自己的收尾最长等这么久
    }

    /// 开机自己起来时不弹要授权的窗口：等用户把螺壳叫到前面
    private static func askWhenFront() {
        if NSApp.isActive { SearchActivation.show(upgraded: true); return }
        var token: NSObjectProtocol?
        token = NotificationCenter.default.addObserver(forName: NSApplication.didBecomeActiveNotification, object: nil, queue: .main) { _ in
            if let token { NotificationCenter.default.removeObserver(token) }
            if !enabled { SearchActivation.show(upgraded: true) }
        }
    }

    /// 让 QuickTime 从搜到的那一刻开始放：自动化权限在螺壳名下，由本体代做。它经描述符 3 说 {"play": 路径, "at": 秒}
    private static func play(_ m: [String: Any]) {
        guard let (path, source) = playScript(m) else { return }
        // NSAppleScript 只能在主线程上跑
        DispatchQueue.main.async {
            // 第一次控制 QuickTime 系统会弹框：用户没刚点过就只打开文件，不跳到那一刻
            guard UserAction.recent() else { NSWorkspace.shared.open(URL(fileURLWithPath: path)); return }
            var error: NSDictionary?
            if NSAppleScript(source: source)?.executeAndReturnError(&error) == nil || error != nil { NSWorkspace.shared.open(URL(fileURLWithPath: path)) }
        }
    }

    /// 程序说的话不算数：只放存在的绝对路径上的音视频（打开别的文件可能就是运行它），路径和秒数都不能带出 AppleScript 的字符串
    static func playScript(_ m: [String: Any], exists: (String) -> Bool = { FileManager.default.fileExists(atPath: $0) }) -> (path: String, source: String)? {
        guard let path = m["play"] as? String, path.hasPrefix("/"), !path.contains("\n"), !path.contains("\r"), exists(path),
              UTType(filenameExtension: (path as NSString).pathExtension)?.conforms(to: .audiovisualContent) == true else { return nil }
        let raw = (m["at"] as? NSNumber)?.doubleValue ?? 0
        let at = raw.isFinite ? max(0, raw) : 0
        let escaped = path.replacingOccurrences(of: "\\", with: "\\\\").replacingOccurrences(of: "\"", with: "\\\"")
        return (path, """
            tell application "QuickTime Player"
                activate
                set movie to open POSIX file "\(escaped)"
                set current time of movie to \(at)
            end tell
            """)
    }

    private static let gate = RestartGate()

    /// 搜索的口令：每起一次常驻的搜索，它在描述符 3 上说 hello 时生成一个新的，随答复交给它（不进参数、环境、文件）。
    /// agent 核心经本体的通道要（ToolHost 的 `token`），每个发给搜索的请求都带上；发布版的搜索没口令或不对一律拒。
    /// 搜索重起就换；agent 拿旧的被拒，再要一次
    static let token = SearchToken()

    /// 现在这个常驻搜索的口令；还没起过、已经不在了是 nil
    static var currentToken: String? { token.current }

    /// 用户同意或收回了一项权限：常驻的程序起的时候才拿到环境，重起才算数。它自己的设置不动。
    /// 不在这一下里停：停要等它退出，紧跟着的系统询问不该等。旧的 .app 还没挪走时也不起，start 里等挪完才起，那时取的已经是新环境
    static func permissionChanged() {
        guard enabled else { return }
        gate.restarting(process.environmentChanged())
        guard oldAppGone else { return }
        DispatchQueue.main.async { process.restart() }
    }

    /// 刚重起时工具的 socket 还没起来，这时转过去的活会失败：等它说起来了（描述符 3 上的 up）再做
    static func whenUp(_ then: @escaping () -> Void) {
        gate.whenUp(then)
    }

    /// 把要到权限的地方写进搜索的设置（它自己的 settings 命令，检查和 agent 改设置时一样）。程序还没起时调。返回失败的原因
    static func choose(folders: [String], remove: [String], photos: Bool?) -> String? {
        guard mayRun else { return "这个构建不开启螺壳搜索" }      // 它的程序只认真实的数据目录，这里不能写
        let args = ["settings"] + folders.flatMap { ["--add-folder", $0] } + remove.flatMap { ["--remove-folder", $0] }
            + (photos.map { ["--photos", $0 ? "on" : "off"] } ?? [])
        do {
            let r = try run(args, capture: 2)
            if r.status == 0 { return nil }
            let why = String(decoding: r.data, as: UTF8.self).trimmingCharacters(in: .whitespacesAndNewlines)
            return why.isEmpty ? "luoke-search 异常退出（\(ToolLauncher.exitCode(r.status))）" : why
        } catch let e as Spawn.Refused {
            return e.why
        } catch {
            return "\(error)"
        }
    }

    /// 搜索的设置里已选的
    struct Chosen: Decodable {
        let folders: [String]
        let photos: Bool
    }

    /// 读不了（还没写过时读到的是空的）：nil，窗口按默认勾，写的时候再报错
    static func current() -> Chosen? {
        guard mayRun else { return nil }
        guard let r = try? run(["settings", "--json"], capture: 1), r.status == 0 else { return nil }
        return try? JSONDecoder().decode(Chosen.self, from: r.data)
    }

    /// 换装前（Installer）：这份程序不是这个进程起的，按路径认
    static func stopAll(hubApp: URL) {
        let program = hubApp.appendingPathComponent("Contents/Helpers/luoke-search")
        let n = HelperProcess.clearStale(path: program.resolvingSymlinksInPath().path, name: program.lastPathComponent, grace: 5)
        if n > 0 { NSLog("螺壳搜索：换装前结束了 %d 个进程", n) }
    }

    /// 本体退出、用户停用、卸载全部：停掉，不再自动起。主线程上调，排在停主控之前，它要向主控报一声
    static func stop() {
        guard bundled else { return }
        process.stop()
    }
}

/// 口令：32 字节随机数（SecRandomCopyBytes），十六进制。只留最新一次起的那个（round 大的）
final class SearchToken: @unchecked Sendable {
    private let lock = NSLock()
    private var round = -1
    private var value: String?

    func mint(round r: Int) -> String? {
        lock.lock(); defer { lock.unlock() }
        guard r >= round else { return nil }        // 已经换过的旧进程
        var bytes = [UInt8](repeating: 0, count: 32)
        guard SecRandomCopyBytes(kSecRandomDefault, bytes.count, &bytes) == errSecSuccess else { return nil }
        let t = bytes.map { String(format: "%02x", $0) }.joined()
        round = r
        value = t
        return t
    }

    /// 常驻的搜索不在了（退了、停了、重起次数用完）：不再交出它的口令。round 留着，旧进程晚到的 hello 照样拿不到
    func clear() {
        lock.lock(); defer { lock.unlock() }
        value = nil
    }

    var current: String? {
        lock.lock(); defer { lock.unlock() }
        return value
    }
}
