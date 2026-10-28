import AppKit
import CryptoKit
import Foundation
import SwiftUI

/// 检查更新（10-19：没有升级的菜单项，测试者拿到 beta1 以后换版本只能自己去下 DMG，容易卡住）。
/// 按这份主控自己的渠道查：内部版去 luoke-internal-builds 查 internal-*，beta 去 luoke-releases 查 beta*；本机版、开发版不查（"由开发者换装"）。
/// 有更新就给版本号和更新说明的链接；点"升级"：按那一版的发布清单下主控的 DMG → 核大小和 sha256 → 只读挂载 →
/// 核 .app 的签名（我们的 Team ID）和 DMG 的公证票据 → 打开它，走现有的原地升级（旧版进 previous.noindex，数据和模型保留）。
/// 新主控起来后卸下这次挂载、删掉下载的 DMG。全在后台线程做，界面只显示进度（不卡主线程，10-19 升级页的教训）。
enum Updater {
    // 内部版单独一个仓库（10-28，luoke-core#36）：外部的人从 luoke-releases 的发布列表里容易装错内部版，装了就一直跟内部版渠道
    static var repo: String { channel == .internalBuild ? "luokelabs/luoke-internal-builds" : "luokelabs/luoke-releases" }
    static let teamID = "NJQ5W3T9K5"
    static let testAPI = ProcessInfo.processInfo.environment["LUOKE_UPDATE_API"]
    static let perPage = 100
    static func api(page: Int) -> String { testAPI ?? "https://api.github.com/repos/\(repo)/releases?per_page=\(perPage)&page=\(page)" }
    static var dir: URL { AppInfo.envDir.appendingPathComponent("downloads/update", isDirectory: true) }
    static var mountFile: URL { AppInfo.envDir.appendingPathComponent("run/update-mount.json") }
    static let autoKey = "autoCheckUpdates"
    static let lastKey = "lastUpdateCheck"

    enum Channel { case internalBuild, beta, local, none }

    static var myRelease: String { Manifests.release()?["release"] as? String ?? "" }
    static var myBuiltAt: Double { (Manifests.release()?["built_at"] as? NSNumber)?.doubleValue ?? 0 }

    static var channel: Channel {
        let r = myRelease
        if r.hasPrefix("internal-") { return .internalBuild }
        if r.hasPrefix("beta") { return .beta }
        if r.hasPrefix("local-") { return .local }
        return .none
    }

    static func matches(_ tag: String) -> Bool {
        switch channel {
        case .internalBuild: return tag.range(of: #"^internal-\d{4}(-\d+)?$"#, options: .regularExpression) != nil
        // beta1、beta1.1（beta 分支上修问题出的小版本）都认（10-26：原来只认 beta1 这种，装了 beta1 的人看不到 beta1.1）
        case .beta: return tag.range(of: #"^beta\d+(\.\d+)?$"#, options: .regularExpression) != nil
        default: return false
        }
    }

    /// notes：GitHub 发布页的正文（markdown），窗口里只列「## 更新说明」这一节
    struct Found {
        let tag: String; let page: String; let manifest: [String: Any]; var notes: String = ""
        var hubSize: Int64? {
            let hub = (manifest["items"] as? [String: Any])?["hub"] as? [String: Any]
            return ((hub?["files"] as? [[String: Any]])?.first?["size"] as? NSNumber)?.int64Value
        }
    }

    enum Failure: Error, LocalizedError {
        case notFound(String), unreachable(String), unverified(String), other(String)
        case keep(String)      // 只在内部用：没装上，但下好的安装包留着（换包脚本没起来、授权取消了）
        var errorDescription: String? {
            switch self {
            case .notFound(let s): return "服务器上找不到 \(s)（这个版本可能还没发布完，或者地址失效了）"
            case .unreachable(let s): return "更新服务器没有答复（\(s.trimmingCharacters(in: CharacterSet(charactersIn: "。.")))）"
            case .unverified(let s): return "下下来了但核对不过：\(s)。没有装，原来的螺壳不动"
            case .other(let s), .keep(let s): return s
            }
        }
    }

    // ---- 网络 ----------------------------------------------------------------------------------------
    private static func get(_ url: URL, timeout: TimeInterval = 30) throws -> Data {
        var req = URLRequest(url: url, timeoutInterval: timeout)
        req.setValue("application/vnd.github+json", forHTTPHeaderField: "Accept")
        req.setValue("luoke-core-updater", forHTTPHeaderField: "User-Agent")     // 只带取更新必需的：不带机器、版本以外的东西
        let sem = DispatchSemaphore(value: 0)
        var out: Result<Data, Error> = .failure(Failure.other("没有答复"))
        URLSession.shared.dataTask(with: req) { data, resp, err in
            if let err { out = .failure(Failure.unreachable(err.localizedDescription)) }
            else if let h = resp as? HTTPURLResponse, h.statusCode == 404 || h.statusCode == 410 { out = .failure(Failure.notFound(url.lastPathComponent)) }
            else if let h = resp as? HTTPURLResponse, !(200..<300).contains(h.statusCode) { out = .failure(Failure.unreachable("HTTP \(h.statusCode)")) }
            else { out = .success(data ?? Data()) }
            sem.signal()
        }.resume()
        sem.wait()
        return try out.get()
    }

    /// 这个渠道里比自己新的最新一版；没有就 nil。会阻塞（网络），别在主线程上调。
    static func check() throws -> Found? {
        guard channel == .internalBuild || channel == .beta else { return nil }
        // 翻页，直到这一页里有这个渠道的版本（10-28：原来只看最新 40 条，内部版一天两版，三周后最近的 beta 就掉出第一页）
        var list: [[String: Any]] = []
        for page in 1...10 {
            let data = try get(URL(string: api(page: page))!)
            guard let batch = try? JSONSerialization.jsonObject(with: data) as? [[String: Any]] else { throw Failure.other("更新服务器的答复看不懂") }
            list += batch
            if testAPI != nil || batch.count < perPage || list.contains(where: { matches($0["tag_name"] as? String ?? "") }) { break }
        }
        let cands = list.filter { ($0["draft"] as? Bool) != true && matches($0["tag_name"] as? String ?? "") }
            .sorted { ($0["published_at"] as? String ?? "") > ($1["published_at"] as? String ?? "") }
        for r in cands.prefix(5) {
            guard let tag = r["tag_name"] as? String, tag != myRelease,
                  let assets = r["assets"] as? [[String: Any]],
                  let rel = assets.first(where: { ($0["name"] as? String) == "release.json" }),
                  let u = rel["browser_download_url"] as? String, let url = URL(string: u) else { continue }
            guard let m = try? JSONSerialization.jsonObject(with: try get(url)) as? [String: Any] else { continue }
            if ((m["built_at"] as? NSNumber)?.doubleValue ?? 0) > myBuiltAt {
                return Found(tag: tag, page: r["html_url"] as? String ?? "https://github.com/\(repo)/releases/tag/\(tag)", manifest: m,
                             notes: r["body"] as? String ?? "")
            }
            return nil            // 最新的一版不比自己新：不往更旧的找
        }
        return nil
    }

    /// 某一版发布页的「更新说明」（升级页用，brand #15：和检查更新窗口用同一份）。会阻塞（网络），连不上就空
    static func notes(for tag: String) -> String {
        guard let url = URL(string: "https://api.github.com/repos/\(repo)/releases/tags/\(tag)"),
              let data = try? get(url, timeout: 15),
              let o = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return "" }
        return userNotes(o["body"] as? String ?? "")
    }

    /// 发布正文里给用户看的那一节（「## 更新说明」到下一个二级标题）；没有这一节就整篇
    static func userNotes(_ body: String) -> String {
        let lines = body.components(separatedBy: "\n")
        guard let i = lines.firstIndex(where: { $0.trimmingCharacters(in: .whitespaces) == "## 更新说明" }) else { return body }
        let rest = lines[(i + 1)...]
        let end = rest.firstIndex(where: { $0.hasPrefix("## ") }) ?? rest.endIndex
        return rest[..<end].joined(separator: "\n").trimmingCharacters(in: .whitespacesAndNewlines)
    }

    // ---- 下载、核对、交给新主控 --------------------------------------------------------------------------
    /// progress(已下, 总共)
    static func download(_ f: Found, progress: @escaping (Int64, Int64) -> Void) throws -> URL {
        guard let hub = (f.manifest["items"] as? [String: Any])?["hub"] as? [String: Any],
              let file = (hub["files"] as? [[String: Any]])?.first, let name = file["path"] as? String,
              let sha = file["sha256"] as? String, let size = (file["size"] as? NSNumber)?.int64Value,
              let base = (hub["urls"] as? [String])?.first else { throw Failure.other("这一版的发布清单里没有螺壳的安装包") }
        guard let url = URL(string: base.replacingOccurrences(of: "{path}", with: name)) else { throw Failure.other("下载地址不对") }
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        let dest = dir.appendingPathComponent("\(f.tag)-\(name)")
        // 上次下好了、只是没换成（授权取消等）：哈希对得上就直接用，不重下
        if let old = try? Data(contentsOf: dest), Int64(old.count) == size,
           SHA256.hash(data: old).map({ String(format: "%02x", $0) }).joined() == sha {
            progress(size, size)
            return dest
        }
        progress(0, size)
        let data = try FileDownload.fetch(url, timeout: 600) { done in progress(done, size) }
        guard Int64(data.count) == size else { throw Failure.unverified("安装包大小不对，可能下载时断了") }
        let got = SHA256.hash(data: data).map { String(format: "%02x", $0) }.joined()
        guard got == sha else { throw Failure.unverified("安装包不完整，可能下载时断了") }
        try data.write(to: dest, options: .atomic)
        return dest
    }

    @discardableResult
    private static func run(_ cmd: String, _ args: [String]) -> (Int32, String) {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: cmd)
        p.arguments = args
        let pipe = Pipe()
        p.standardOutput = pipe
        p.standardError = pipe
        guard (try? p.run()) != nil else { return (-1, "") }
        let out = String(decoding: pipe.fileHandleForReading.readDataToEndOfFile(), as: UTF8.self)
        p.waitUntilExit()
        return (p.terminationStatus, out)
    }

    /// 挂载、核签名和公证、把新版拷到暂存处、卸掉 DMG、起换包脚本，然后主控自己退出，脚本换好包再打开新版（ADR 0012）。
    /// 不再打开 DMG 里的安装器：同一个 bundle id 再起一个实例，旧的会作为它的父进程挂在程序坞上（10-24）。
    /// verified：核对都过了、脚本起来了，接下来主控退出
    static func install(dmg: URL, to tag: String, notarized: Bool = true, verified: @escaping () -> Void) throws {
        do {
            try installChecked(dmg: dmg, to: tag, notarized: notarized, verified: verified)
        } catch Failure.keep(let why) {
            throw Failure.other(why)
        } catch {
            try? FileManager.default.removeItem(at: dmg)      // 核对不过、挂不上的安装包不留在 downloads/update/（下次重新下）
            throw error
        }
    }

    /// notarized = false 只给开发版的测试入口（--upgrade-from-dmg）：不核公证和团队签名
    private static func installChecked(dmg: URL, to tag: String, notarized: Bool, verified: @escaping () -> Void) throws {
        guard !notarized || run("/usr/bin/xcrun", ["stapler", "validate", dmg.path]).0 == 0 else { throw Failure.unverified("安装包没通过苹果的安全检查") }
        let (rc, out) = run("/usr/bin/hdiutil", ["attach", "-nobrowse", "-readonly", "-plist", dmg.path])
        guard rc == 0, let plist = try? PropertyListSerialization.propertyList(from: Data(out.utf8), format: nil) as? [String: Any],
              let mount = ((plist["system-entities"] as? [[String: Any]]) ?? []).compactMap({ $0["mount-point"] as? String }).first
        else { throw Failure.other("安装包挂载不上") }
        // 改名（10-23）：internal-1026 起安装包里叫「螺壳.app」，之前叫「螺壳主控.app」；两个都认
        let app = [AppInfo.appName, AppInfo.legacyName, "螺壳", "螺壳主控"].map { URL(fileURLWithPath: mount).appendingPathComponent("\($0).app") }
            .first { FileManager.default.fileExists(atPath: $0.path) } ?? URL(fileURLWithPath: mount).appendingPathComponent("螺壳.app")
        let (vrc, _) = run("/usr/bin/codesign", ["--verify", "--deep", "--strict", app.path])
        let info = run("/usr/bin/codesign", ["-dv", app.path]).1
        guard vrc == 0, !notarized || info.contains("TeamIdentifier=\(teamID)") else {
            run("/usr/bin/hdiutil", ["detach", "-quiet", mount])
            throw Failure.unverified("安装包里的螺壳不是螺壳实验室签的")
        }
        // 拷到暂存处再核一次（拷坏了不换），然后 DMG 就用完了：卸下、删掉
        let staged = Installer.stagingDir.appendingPathComponent("\(AppInfo.appName).app")
        let copied = AppPlacement.copy(app, to: staged, register: false)
            && run("/usr/bin/codesign", ["--verify", "--deep", "--strict", staged.path]).0 == 0
        run("/usr/bin/hdiutil", ["detach", "-quiet", mount])
        guard copied else {
            try? FileManager.default.removeItem(at: staged)
            throw Failure.other("没能把新版准备好（拷贝或核对签名没过），原来的螺壳没动")
        }
        if Installer.targetTaken { try? FileManager.default.removeItem(at: staged); throw Failure.other(Installer.takenMessage) }
        Installer.writeUpgrade(from: myRelease, to: tag)
        verified()        // 卡片到「交给新版」这一步；授权取消、脚本没起来，失败就写在这一步上
        if let why = Swap.start(current: Installer.installed ?? Bundle.main.bundleURL, staged: staged, target: Installer.target,
                                waitPID: ProcessInfo.processInfo.processIdentifier, admin: Installer.needsAdmin) {
            try? FileManager.default.removeItem(at: Installer.upgradeFile)
            try? FileManager.default.removeItem(at: staged)
            throw Failure.keep(why)      // 安装包留着：「重试」不用整个重下
        }
        try? FileManager.default.removeItem(at: dmg)
        // 脚本在等我们退出：停记忆进程，退出（applicationWillTerminate 停主控进程和引擎）。新版起来显示升级卡片
        Memory.stopAll(hubApp: Bundle.main.bundleURL)
        DispatchQueue.main.async { NSApp.terminate(nil) }
    }

    /// update-mount.json 里记的挂载（旧版写的是单个对象，也认）
    static func mountRecords() -> [[String: String]] {
        guard let d = FileManager.default.contents(atPath: mountFile.path), let o = try? JSONSerialization.jsonObject(with: d) else { return [] }
        if let list = o as? [[String: String]] { return list }
        if let one = o as? [String: String] { return [one] }
        return []
    }

    /// 现在挂着的、镜像文件在 downloads/update/ 里的卷（记录丢了的也找得到）
    private static func mountsFromUpdateDir() -> [String] {
        let (rc, out) = run("/usr/bin/hdiutil", ["info", "-plist"])
        guard rc == 0, let plist = try? PropertyListSerialization.propertyList(from: Data(out.utf8), format: nil) as? [String: Any] else { return [] }
        let prefix = dir.standardizedFileURL.resolvingSymlinksInPath().path + "/"
        return ((plist["images"] as? [[String: Any]]) ?? []).filter {
            URL(fileURLWithPath: $0["image-path"] as? String ?? "").standardizedFileURL.resolvingSymlinksInPath().path.hasPrefix(prefix)
        }.flatMap { (($0["system-entities"] as? [[String: Any]]) ?? []).compactMap { $0["mount-point"] as? String } }
    }

    /// 主控启动时调一次（不管首次设置走没走完，luoke-core#28）：卸下"检查更新"升级时挂的安装包、删掉 downloads/update/ 里下载的 DMG。
    /// 记录里的和记录丢了、但镜像在 downloads/update/ 里的挂载都卸。
    /// 先等 DMG 里那份安装器真退出了（再多等 2 秒，让 LaunchServices 和程序坞收到退出），再注销、卸下：10-23 升到
    /// internal-1023 后程序坞里留了一个"后台运行"的死图标——安装器还没退完，它所在的卷就被卸了，程序坞没收到它退出。
    /// 卸不下（还有东西开着）就隔几秒再试，不用 -force。在后台线程做，不挡启动。
    static func cleanupAfterUpgrade() {
        let records = mountRecords()
        try? FileManager.default.removeItem(at: mountFile)
        DispatchQueue.global(qos: .utility).async {
            var mounts: [String] = []
            for m in records.compactMap({ $0["mount"] }) + mountsFromUpdateDir() where !mounts.contains(m) { mounts.append(m) }
            for m in mounts where FileManager.default.fileExists(atPath: m) {
                let onVolume = { NSRunningApplication.runningApplications(withBundleIdentifier: Bundle.main.bundleIdentifier ?? "ai.luoke.core")
                    .contains { !$0.isTerminated && ($0.bundleURL?.standardizedFileURL.path ?? "").hasPrefix(m + "/") } }
                for _ in 0..<120 where onVolume() { Thread.sleep(forTimeInterval: 0.5) }      // 最多等 1 分钟
                Thread.sleep(forTimeInterval: 2)
                for name in ["螺壳.app", "螺壳主控.app"] { AppPlacement.unregister(URL(fileURLWithPath: m).appendingPathComponent(name)) }
                for _ in 0..<5 where FileManager.default.fileExists(atPath: m) {
                    if run("/usr/bin/hdiutil", ["detach", "-quiet", m]).0 == 0 { break }
                    Thread.sleep(forTimeInterval: 5)
                }
            }
            // 下载的安装包用完就没用了：记录里的和 downloads/update/ 里剩下的 DMG 都删（这时不会有正在下的：下载只在检查更新窗口里进行）
            for f in records.compactMap({ $0["dmg"] }) { try? FileManager.default.removeItem(atPath: f) }
            for f in (try? FileManager.default.contentsOfDirectory(at: dir, includingPropertiesForKeys: nil)) ?? [] where f.pathExtension == "dmg" {
                try? FileManager.default.removeItem(at: f)
            }
        }
    }

    // ---- 每天一次的自动检查（只提示，不自动装）---------------------------------------------------------
    static var autoEnabled: Bool {
        get { UserDefaults.standard.object(forKey: autoKey) as? Bool ?? true }
        set { UserDefaults.standard.set(newValue, forKey: autoKey) }
    }

    static func autoCheckIfDue(found: @escaping (Found) -> Void) {
        guard autoEnabled, channel == .internalBuild || channel == .beta else { return }
        let last = UserDefaults.standard.double(forKey: lastKey)
        // 内部版一天两版（10:00、21:00，10-24）：每 3 小时查一次；beta 照旧每天一次（luoke-top 10-25 建议）
        let every: Double = channel == .internalBuild ? 3 * 3600 : 24 * 3600
        guard Date().timeIntervalSince1970 - last > every else { return }
        DispatchQueue.global().async {
            // 查成了才记"今天查过"（luoke-top 10-23 核查：之前先记再查，离线那一刻查不成就要等 24 小时）；没查成，下一个钟头再试
            let r: Found?
            do { r = try check() } catch { return }
            UserDefaults.standard.set(Date().timeIntervalSince1970, forKey: lastKey)
            if let f = r { DispatchQueue.main.async { found(f) } }
        }
    }

    // ---- 窗口 ---------------------------------------------------------------------------------------
    static var window: NSWindow?
    static func show(found: Found? = nil) {
        if let w = window, w.isVisible { Uninstall.bringFront(w); return }
        let host = NSHostingController(rootView: UpdateView(m: UpdateModel(found: found)))
        let w = NSWindow(contentViewController: host)
        w.title = "检查更新"
        w.styleMask = [.titled, .closable]
        w.setContentSize(NSSize(width: 560, height: 460))
        w.center()
        w.isReleasedWhenClosed = false
        window = w
        AppWindows.track(w)
        Uninstall.bringFront(w)
    }
}

/// 带进度的下载（会阻塞，别在主线程上调）
final class FileDownload: NSObject, URLSessionDownloadDelegate {
    private let progress: (Int64) -> Void
    private let sem = DispatchSemaphore(value: 0)
    private var result: Result<Data, Error> = .failure(Updater.Failure.other("没有答复"))

    private init(progress: @escaping (Int64) -> Void) { self.progress = progress }

    static func fetch(_ url: URL, timeout: TimeInterval, progress: @escaping (Int64) -> Void) throws -> Data {
        let d = FileDownload(progress: progress)
        let cfg = URLSessionConfiguration.ephemeral
        cfg.timeoutIntervalForRequest = 60
        cfg.timeoutIntervalForResource = timeout
        let session = URLSession(configuration: cfg, delegate: d, delegateQueue: nil)
        session.downloadTask(with: url).resume()
        d.sem.wait()
        session.finishTasksAndInvalidate()
        return try d.result.get()
    }

    func urlSession(_ session: URLSession, downloadTask: URLSessionDownloadTask, didWriteData bytesWritten: Int64,
                    totalBytesWritten: Int64, totalBytesExpectedToWrite: Int64) {
        progress(totalBytesWritten)
    }

    func urlSession(_ session: URLSession, downloadTask: URLSessionDownloadTask, didFinishDownloadingTo location: URL) {
        if let h = downloadTask.response as? HTTPURLResponse, !(200..<300).contains(h.statusCode) {
            result = .failure(h.statusCode == 404 || h.statusCode == 410 ? Updater.Failure.notFound(downloadTask.originalRequest?.url?.lastPathComponent ?? "")
                                                                          : Updater.Failure.unreachable("HTTP \(h.statusCode)"))
        } else if let data = try? Data(contentsOf: location) {      // 临时文件在这个回调返回后就没了：当场读出来
            result = .success(data)
        } else {
            result = .failure(Updater.Failure.other("下载的文件读不出来"))
        }
    }

    func urlSession(_ session: URLSession, task: URLSessionTask, didCompleteWithError error: Error?) {
        if let error { result = .failure(Updater.Failure.unreachable(error.localizedDescription)) }
        sem.signal()
    }
}

/// 检查更新窗口的状态（luoke-brand 样板 updater-*）
final class UpdateModel: ObservableObject {
    enum Phase: Equatable {
        case checking
        case info(String)           // 本机版、开发版：不查更新
        case latest
        case found
        case working(step: Int)     // 0 下载安装包、1 核对签名和公证、2 交给新版主控升级
        case offline(String)        // 查更新时连不上
        case checkFailed(String)    // 查更新时别的错
        case unverified(String)     // 安装包核对不过：没有装
        case failed(step: Int, String)
    }


    @Published var phase: Phase = .checking
    @Published var found: Updater.Found?
    @Published var done: Int64 = 0
    @Published var total: Int64 = 0
    /// 已装、有新版本的应用名（螺壳不顺带升级应用，这里提醒去「应用」里升）
    @Published var appsWithUpdates: [String] = []

    init(found: Updater.Found?, autoCheck: Bool = true) {
        self.found = found
        if !FirstRunModel.previewing { appsWithUpdates = AppsWindow.updatable().map(\.name) }
        if found != nil { phase = .found } else if autoCheck { check() }
    }

    var busy: Bool {
        switch phase { case .checking, .working: return true; default: return false }
    }

    func check() {
        switch Updater.channel {
        case .local: phase = .info("这是本机版螺壳（\(Updater.myRelease)），由开发者直接换装，不从网上查更新。"); return
        case .none: phase = .info("这是开发版螺壳，没有发布渠道，不查更新。"); return
        default: break
        }
        phase = .checking
        DispatchQueue.global().async {
            let r = Result { try Updater.check() }
            DispatchQueue.main.async {
                switch r {
                case .success(let f?): self.found = f; self.phase = .found
                case .success(nil): self.phase = .latest
                case .failure(let e):
                    if case Updater.Failure.unreachable = e { self.phase = .offline(e.localizedDescription) }
                    else { self.phase = .checkFailed(e.localizedDescription) }
                }
            }
        }
    }

    func upgrade() {
        guard let f = found else { return }
        phase = .working(step: 0)
        done = 0
        total = f.hubSize ?? 0
        DispatchQueue.global().async {
            var step = 0
            do {
                let dmg = try Updater.download(f) { d, t in DispatchQueue.main.async { self.done = d; self.total = t } }
                step = 1
                DispatchQueue.main.async { self.phase = .working(step: 1) }
                try Updater.install(dmg: dmg, to: f.tag) {
                    step = 2
                    DispatchQueue.main.async { self.phase = .working(step: 2) }
                }
            } catch {
                let msg = error.localizedDescription
                DispatchQueue.main.async {
                    if case Updater.Failure.unverified(let why) = error { self.phase = .unverified(why) }
                    else { self.phase = .failed(step: step, msg) }
                }
            }
        }
    }
}

struct UpdateView: View {
    @ObservedObject var m: UpdateModel

    /// 一屏，不要左栏（10-23：自己跑完的流程不用分步导航，和设置页同一个思路）
    var body: some View {
        HStack(spacing: 0) {
            VStack(spacing: 0) {
                VStack(alignment: .leading, spacing: 0) {
                    header.padding(.bottom, 20)
                    content
                    Spacer(minLength: 0)
                }
                .frame(maxWidth: .infinity, alignment: .topLeading)
                .padding(.horizontal, 32).padding(.top, 28).padding(.bottom, 12)
                footer
            }
            .background(Color.lkBackground)
        }
        .foregroundStyle(Color.lkForeground)
        .frame(width: 560, height: 460)
    }

    // MARK: 标题

    private var title: String {
        switch m.phase {
        case .checking: return "正在查有没有更新"
        case .info: return "检查更新"
        case .latest: return m.appsWithUpdates.isEmpty ? "已经是最新的" : "螺壳已经是最新的"
        case .found: return "有新版本：\(m.found?.tag ?? "")"
        case .working(let s): return s == 0 ? "正在下载 \(m.found?.tag ?? "")" : (s == 1 ? "正在核对安装包" : "正在升级到 \(m.found?.tag ?? "")")
        case .offline: return "连不上更新服务器"
        case .checkFailed: return "没查成"
        case .unverified: return "安装包核对不过"
        case .failed: return UpgradeSteps.failedTitle
        }
    }

    private var subtitle: String {
        switch m.phase {
        case .checking: return ""
        case .info: return ""
        case .latest: return ""
        case .found:
            return m.found?.hubSize.map { "约 \(Diagnostics.humanSize($0))" } ?? ""
        case .working: return ""
        case .offline: return ""
        case .checkFailed: return ""
        case .unverified: return ""
        case .failed: return ""
        }
    }

    private var header: some View {
        HStack(alignment: subtitle.isEmpty ? .center : .top, spacing: 14) {
            if let icon = LKAsset.image("app-core.png") {
                Image(nsImage: icon).resizable().frame(width: 40, height: 40)
            }
            VStack(alignment: .leading, spacing: 4) {
                Text(title).font(.system(size: 20, weight: .semibold))
                if !subtitle.isEmpty { Text(subtitle).font(.system(size: 14)).foregroundStyle(Color.lkMuted) }
            }
            Spacer()
        }
    }

    // MARK: 正文

    @ViewBuilder private var content: some View {
        switch m.phase {
        case .info(let text):
            Text(text).font(.system(size: 14)).foregroundStyle(Color.lkMuted).fixedSize(horizontal: false, vertical: true)
            appsNote.padding(.top, 12)
        case .found:
            notes
            LKNote(icon: "checkmark.shield", text: "这里只升级螺壳，数据都保留。说明里提到的应用在「应用」里单独升级。")
                .padding(.top, 12)
        case .working(let s):
            UpgradeProgress(current: s, line: s == 0 ? (m.total > 0 ? "\(Diagnostics.humanSize(m.done)) / \(Diagnostics.humanSize(m.total))" : "开始下载") : (s == 1 ? "正在核对…" : "螺壳会先退出，换好新版后自己重新打开，大约一分钟。"),
                            fraction: s == 0 && m.total > 0 ? Double(m.done) / Double(m.total) : nil)
        case .offline(let why):
            LKNote(icon: "wifi.slash", text: why + "。", kind: .danger)
        case .checkFailed(let why):
            LKNote(icon: "exclamationmark.triangle", text: why, kind: .danger)
        case .unverified(let why):
            LKNote(icon: "exclamationmark.triangle", text: "\(why)。重试，还不行就在「设置 › 关于」里导出诊断信息发给我们。", kind: .danger)
        case .failed(let step, let why):
            UpgradeProgress(current: step, failed: why)
        case .latest:
            appsNote
        case .checking:
            EmptyView()
        }
    }

    /// 螺壳是最新的，但已装的应用有新版本（螺壳不再顺带升级应用）：在这里也提一句，免得以为全都升好了
    @ViewBuilder private var appsNote: some View {
        if !m.appsWithUpdates.isEmpty {
            LKNote(icon: "arrow.up.circle", text: m.appsWithUpdates.joined(separator: "、") + " 有新版本，在「应用」里升级。", kind: .warning)
        }
    }

    /// 这一版有什么不同：发布正文的「更新说明」一节，直接列在窗口里（不跳网页）
    @ViewBuilder private var notes: some View {
        LKReleaseNotes(text: Updater.userNotes(m.found?.notes ?? ""))
    }

    // MARK: 底栏：左边自动检查开关，右边按钮

    private var inUpgrade: Bool {
        switch m.phase { case .working, .failed: return true; default: return false }
    }

    private var footer: some View {
        HStack(spacing: 8) {
            // 升级卡片（下载起到出错）和安装器、新版那两段长得一样：都不放自动检查开关
            if !inUpgrade {
                Toggle("", isOn: Binding(get: { Updater.autoEnabled }, set: { Updater.autoEnabled = $0 }))
                    .toggleStyle(.switch).controlSize(.mini).tint(.lkPrimary).labelsHidden()
                Text("每天自动检查").font(.system(size: 12)).foregroundStyle(Color.lkMuted).lineLimit(1).fixedSize()
            }
            Spacer()
            switch m.phase {
            case .found:
                Button("以后再说") { Updater.window?.close() }.buttonStyle(LKSecondaryButton(ghost: true))
                let tag = m.found?.tag ?? ""
                Button(tag.count <= 8 ? "升级到 \(tag)" : "升级") { m.upgrade() }.buttonStyle(LKPrimaryButton())
            case .working:
                Button("升级中") {}.buttonStyle(LKPrimaryButton()).disabled(true)
            case .checking:
                Button("完成") {}.buttonStyle(LKPrimaryButton()).disabled(true)
            case .offline, .checkFailed:
                Button { m.check() } label: { Label("重试", systemImage: "arrow.clockwise") }.buttonStyle(LKPrimaryButton())
            case .unverified:
                Button { m.upgrade() } label: { Label("重试", systemImage: "arrow.clockwise") }.buttonStyle(LKPrimaryButton())
            case .failed:
                Button("先用着") { Updater.window?.close() }.buttonStyle(LKSecondaryButton(ghost: true))
                Button { m.upgrade() } label: { Label("重试", systemImage: "arrow.clockwise") }.buttonStyle(LKPrimaryButton())
            case .latest, .info:
                if m.appsWithUpdates.isEmpty {
                    Button("完成") { Updater.window?.close() }.buttonStyle(LKPrimaryButton())
                } else {
                    Button("完成") { Updater.window?.close() }.buttonStyle(LKSecondaryButton(ghost: true))
                    Button("去升级应用") { Updater.window?.close(); AppsWindow.show() }.buttonStyle(LKPrimaryButton())
                }
            }
        }
        .padding(.horizontal, 32).frame(height: 64)
        .overlay(alignment: .top) { Rectangle().fill(Color.lkBorder).frame(height: 1) }
    }
}
