// 安装器模式（ADR 0002、DESIGN §8）和发布清单的读取。
// 每个应用的下载按钮给的都是同一个主控 .app，放在以应用命名的 DMG 里、文件名叫"安装<应用>.app"。打开时：
//   1. 按自己的文件名在发布清单里找要预选的应用（items[*].installer）；
//   2. ~/Applications/螺壳.app（改名前是 螺壳主控.app）已装着：把预选交给它（run/preselect.json）、打开它、自己退出，不覆盖；
//   3. 否则（在 DMG 里、被 App Translocation、或文件名不是 螺壳.app）：把自己拷到那个固定位置、去掉 quarantine 标记、登记、从那里重启。
import AppKit
import ServiceManagement
import CryptoKit
import Foundation

enum Manifests {
    static func json(_ path: String) -> [String: Any]? {
        guard let d = FileManager.default.contents(atPath: path) else { return nil }
        return (try? JSONSerialization.jsonObject(with: d)) as? [String: Any]
    }
    static func recipe() -> [String: Any]? { json(AppInfo.recipe) }
    static func release() -> [String: Any]? { json(AppInfo.release) }
    static var hasRelease: Bool { FileManager.default.fileExists(atPath: AppInfo.release) }
    static func items() -> [String: [String: Any]] { (release()?["items"] as? [String: [String: Any]]) ?? [:] }

    // ---- 用户可选的档（省空间 / 完整；issue luoke-core#2，2026-10-16 定进 beta1）----
    struct TierChoice { let tier: String, label: String, base: String, sizeGB: Double, note: String }
    static var settingsFile: URL { AppInfo.envDir.appendingPathComponent("settings.json") }

    /// 这台 Mac 内存够的档里，底座不同的才算不同的选项：最高的叫「完整」，更低的叫「省空间」；只有一个选项就不用选
    static func tierChoices(memGB: Int) -> [TierChoice] {
        guard let r = recipe(), let tiers = r["tiers"] as? [String: Any], let assign = r["assignment"] as? [String: Any],
              let models = r["models"] as? [String: Any] else { return [] }
        var out: [TierChoice] = []
        var seen = Set<String>()
        for name in tiers.keys.sorted(by: { (Int($0) ?? 0) > (Int($1) ?? 0) }) {
            guard let t = tiers[name] as? [String: Any], ((t["min_gb"] as? NSNumber)?.intValue ?? 0) <= memGB,
                  let base = (assign[name] as? [String: Any])?["base"] as? String, !seen.contains(base) else { continue }
            seen.insert(base)
            let m = models[base] as? [String: Any] ?? [:]
            out.append(TierChoice(tier: name, label: out.isEmpty ? "完整" : "省空间", base: m["dir"] as? String ?? base,
                                  sizeGB: (m["size_gb"] as? NSNumber)?.doubleValue ?? 0, note: evidenceNote(base: base, tier: name)))
        }
        return out
    }

    /// 实测差距：配方 evidence 里这个底座在这一档上几个硬性任务的分数，一行（没有证据就空）
    static func evidenceNote(base: String, tier: String) -> String {
        guard let ev = recipe()?["evidence"] as? [[String: Any]] else { return "" }
        let names = ["luoke-mail.triage": "邮件分拣", "luoke-mail.cleanup": "清理判断", "luoke-mem.typing": "记忆归纳"]
        var parts: [String] = []
        for e in ev where (e["model"] as? String) == base && ((e["tiers"] as? [String]) ?? []).contains(tier) {
            guard let task = e["task"] as? String, let short = names[task], let c = e["conclusion"] as? String,
                  let r = c.range(of: #"\d+(\.\d+)?/\d+(\.\d+)?"#, options: .regularExpression), !parts.contains(where: { $0.hasPrefix(short) }) else { continue }
            parts.append("\(short) \(c[r])")
        }
        return parts.joined(separator: "，")
    }

    /// 主控设置里用户选的档（主控没起来时直接读文件；没选或不合法就 nil）
    static func chosenTier(memGB: Int) -> String? {
        guard let t = json(settingsFile.path)?["tier"] as? String, let tiers = recipe()?["tiers"] as? [String: Any],
              let d = tiers[t] as? [String: Any], ((d["min_gb"] as? NSNumber)?.intValue ?? 0) <= memGB else { return nil }
        return t
    }

    /// 首次运行时主控还没起：把选的档直接写进它的 settings.json（合并，别的设置不动）；nil 表示按内存算
    static func writeChosenTier(_ tier: String?) {
        var o = json(settingsFile.path) ?? [:]
        o["tier"] = tier ?? NSNull()
        try? FileManager.default.createDirectory(at: settingsFile.deletingLastPathComponent(), withIntermediateDirectories: true)
        if let d = try? JSONSerialization.data(withJSONObject: o, options: [.prettyPrinted]) { try? d.write(to: settingsFile, options: .atomic) }
    }

    static func itemSize(_ name: String) -> Int64 {
        guard let files = items()[name]?["files"] as? [[String: Any]] else { return 0 }
        return files.reduce(0) { $0 + (($1["size"] as? NSNumber)?.int64Value ?? 0) }
    }

    /// 内存多少 GB 算哪一档：按配方 tiers 表（min_gb ≤ 内存 < max_gb）；配方没有就按 24 / 64 分
    static func tierFor(memGB: Int) -> String {
        if let tiers = recipe()?["tiers"] as? [String: Any] {
            for (name, v) in tiers {
                guard let t = v as? [String: Any] else { continue }
                let lo = (t["min_gb"] as? NSNumber)?.intValue ?? 0, hi = (t["max_gb"] as? NSNumber)?.intValue ?? Int.max
                if memGB >= lo && memGB < hi { return name }
            }
        }
        return memGB < 24 ? "16" : (memGB < 64 ? "32" : "128")
    }

    /// 本档首次运行要占的磁盘：清单里本档的模型 + 引擎包 + 1 GB 余量；没有清单时按 20 / 40 GB
    static func diskNeeded(tier: String) -> Int64 {
        guard hasRelease else { return tier == "128" ? 40 * (1 << 30) : 20 * (1 << 30) }
        var total: Int64 = 1 << 30
        for name in modelsFor(tier: tier) where !modelInstalled(name) { total += itemSize(name) }
        if !engineInstalled() { total += itemSize("engine") }
        return total
    }

    /// 本档要的模型名（配方 assignment：底座 + 专用模型）
    static func modelsFor(tier: String) -> [String] {
        guard let r = recipe(), let asg = (r["assignment"] as? [String: Any])?[tier] as? [String: Any] else { return [] }
        var out: [String] = []
        if let b = asg["base"] as? String { out.append(b) }
        for (_, v) in (asg["special"] as? [String: String]) ?? [:] where !out.contains(v) { out.append(v) }
        return out
    }
    static func modelDir(_ name: String) -> String? {
        ((recipe()?["models"] as? [String: Any])?[name] as? [String: Any])?["dir"] as? String
    }
    /// 模型装好了：目录里的 .luoke-item.json 记的摘要等于清单这一项的摘要（审查 2026-10-17：之前只看目录在不在）。
    /// 没有摘要的旧目录算"要下"，主控下载时按哈希复用里面对得上的文件，不重下。
    static func modelInstalled(_ name: String) -> Bool {
        guard let dir = modelDir(name) else { return false }
        let path = AppInfo.modelDir + "/" + dir
        guard let item = items()[name] else { return FileManager.default.fileExists(atPath: path) }
        guard let marker = json(path + "/.luoke-item.json"), let digest = marker["digest"] as? String else { return false }
        return digest == itemDigest(item)
    }

    /// 和 luoke_core/download.py 的 item_digest 同一算法：按路径排好的"路径\tsha256"逐行，取 sha256
    static func itemDigest(_ item: [String: Any]) -> String {
        let files = (item["files"] as? [[String: Any]] ?? []).compactMap { f -> (String, String)? in
            guard let p = f["path"] as? String, let h = f["sha256"] as? String else { return nil }
            return (p, h)
        }.sorted { $0.0 < $1.0 }
        let text = files.map { "\($0.0)\t\($0.1)" }.joined(separator: "\n")
        return SHA256.hash(data: Data(text.utf8)).map { String(format: "%02x", $0) }.joined()
    }
    static func engineVersion() -> String { ((recipe()?["engine"] as? [String: Any])?["version"] as? String) ?? "" }
    /// 引擎在不在：按清单装的那份（<env>/engines/<版本>/oMLX.app），或配方 engine.app 指的那份——后者要 Info.plist 的版本和配方一致
    /// （"omlx-0.7.0" ↔ "0.7.0"；和主控 hub.py 的 recipe_engine_app 同一规则，用户自己装的别的版本不算）
    static func engineInstalled() -> Bool { engineProblem() == nil }

    /// 引擎为什么不算在：nil = 在；否则一句话分清"没装"和"版本不对"（luokelabs/luoke-core#1 要求分开说）。
    static func engineProblem() -> String? {
        let installed = AppInfo.envDir.appendingPathComponent("engines/\(engineVersion())/oMLX.app/Contents").path
        if FileManager.default.fileExists(atPath: installed) { return nil }
        let v = engineVersion()
        let want = v.hasPrefix("omlx-") ? String(v.dropFirst(5)) : v
        // 发布版只用清单里的引擎包，不用本机另装的 oMLX（和主控 hub.py 的 recipe_engine_app 同一规则，审查 2026-10-17）
        if hasRelease && !AppInfo.isLab { return "引擎包还没装（要下载 \(Diagnostics.humanSize(itemSize("engine")))）" }
        guard let app = (recipe()?["engine"] as? [String: Any])?["app"] as? String, !app.isEmpty else { return "配方没有指定引擎位置" }
        guard let info = NSDictionary(contentsOfFile: app + "/Contents/Info.plist"),
              let have = info["CFBundleShortVersionString"] as? String else { return "没装引擎：\(app) 不存在（要 oMLX \(want)）" }
        return have == want ? nil : "引擎版本不对：\(app) 是 \(have)，配方要 \(want)"
    }

    struct AccessoryItem {
        let id: String, name: String, bundleID: String, type: String, memory: Bool, size: Int64, minMacOS: String?, version: String
    }
    /// 清单里的配件（kind=accessory），按标识排序
    static func accessories() -> [AccessoryItem] {
        items().compactMap { (k, v) -> AccessoryItem? in
            guard v["kind"] as? String == "accessory" else { return nil }
            return AccessoryItem(id: k, name: v["name"] as? String ?? k, bundleID: v["bundle_id"] as? String ?? "",
                                 type: v["type"] as? String ?? "app", memory: v["memory"] as? Bool ?? false, size: itemSize(k),
                                 minMacOS: v["min_macos"] as? String, version: v["version"] as? String ?? "")
        }.sorted { $0.id < $1.id }
    }
    /// 这台 Mac 的 macOS 够不够配件要的（"26" / "26.1"）
    static func macOSSatisfies(_ min: String?) -> Bool {
        guard let min = min else { return true }
        let parts = min.split(separator: ".").compactMap { Int($0) }
        let v = ProcessInfo.processInfo.operatingSystemVersion
        let have = [v.majorVersion, v.minorVersion, v.patchVersion]
        for (i, p) in parts.prefix(3).enumerated() {
            if have[i] > p { return true }
            if have[i] < p { return false }
        }
        return true
    }
}

enum Installer {
    // 装在哪（ADR 0012，10-24）：新装的放 /Applications；已装的以实际位置为准，/Applications 和 ~/Applications 都认，
    // 新旧名字（螺壳 / 改名前的螺壳主控）都认，只认 bundle id 和自己一样的。
    static var systemDir: URL { URL(fileURLWithPath: "/Applications", isDirectory: true) }
    static var userDir: URL { FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Applications", isDirectory: true) }
    /// 认得的位置，按优先顺序
    static var candidates: [URL] {
        [systemDir, userDir].flatMap { d in [AppInfo.appName, AppInfo.legacyName].map { d.appendingPathComponent("\($0).app") } }
    }
    /// 已装的那份（10-24：开发机上 agent 的开发版也叫「螺壳（开发）.app」，所以按 bundle id 认）
    static var installed: URL? { candidates.first { isOurs($0) } }
    /// 这个位置上的 .app 是不是我们（bundle id 一样）
    static func isOurs(_ app: URL) -> Bool {
        AppPlacement.bundleID(app) != nil && AppPlacement.bundleID(app) == Bundle.main.bundleIdentifier
    }
    /// 这个目录我们自己能不能直接写（/Applications：管理员账户能，标准账户不能）
    static func writable(_ dir: URL) -> Bool { FileManager.default.isWritableFile(atPath: dir.path) }
    /// 新版放哪：已装在 /Applications 就还在那里（写不进去就要授权）；装在 ~/Applications 的，/Applications 写得进去就搬过去，
    /// 写不进去就留在原处（不为搬家要授权）；新装的放 /Applications，写不进去退回 ~/Applications
    static var target: URL {
        let sys = systemDir.appendingPathComponent("\(AppInfo.appName).app")
        let user = userDir.appendingPathComponent("\(AppInfo.appName).app")
        let foreign = { (u: URL) in FileManager.default.fileExists(atPath: u.path) && !isOurs(u) }
        if foreign(sys) { return user }          // /Applications 里的「螺壳」是别的应用：用 ~/Applications，不拦（10-25 核出）
        if let cur = installed, cur.deletingLastPathComponent().standardizedFileURL.path == systemDir.path { return sys }
        if writable(systemDir) { return sys }
        return user
    }

    /// 正在跑的是认得的位置上我们的另一份，不是 installed（两处各有一份）
    static var isStrayCopy: Bool {
        let me = Bundle.main.bundleURL.standardizedFileURL.resolvingSymlinksInPath().path
        return candidates.contains { $0.standardizedFileURL.resolvingSymlinksInPath().path == me } && isOurs(Bundle.main.bundleURL)
    }

    /// 装好的那份启动时：认得的位置上我们的另几份（没在跑的）挪进 previous.noindex、注销，免得系统、配件、登录项认到旧的。
    /// 挪走过就重新登记一次登录项（可能还指着旧的那份；用户关掉过的不动）
    static func retireStrayCopies() {
        guard let cur = installed else { return }
        let me = Bundle.main.bundleIdentifier ?? ""
        var moved = false
        for c in candidates where c.standardizedFileURL.path != cur.standardizedFileURL.path && isOurs(c) {
            let running = NSRunningApplication.runningApplications(withBundleIdentifier: me)
                .contains { $0.bundleURL?.standardizedFileURL.path == c.standardizedFileURL.path && $0.processIdentifier != getpid() }
            if running { continue }
            if (try? AppPlacement.retire(c, into: previousDir, name: c.lastPathComponent)) != nil { moved = true }
        }
        if moved, SMAppService.mainApp.status != .notRegistered { try? SMAppService.mainApp.register() }
    }
    /// 换到 target 要不要管理员授权
    static var needsAdmin: Bool { !writable(target.deletingLastPathComponent()) }
    /// 两处的「螺壳」都被别的应用占着（bundle id 不一样）：不覆盖、不认
    static var targetTaken: Bool { FileManager.default.fileExists(atPath: target.path) && !isOurs(target) }
    static var takenMessage: String {
        "「应用程序」里已经有叫「\(AppInfo.appName)」的别的应用（\(AppPlacement.bundleID(target) ?? "认不出是什么")），螺壳没动它。" +
        "把它改个名字或挪走，再试一次。"
    }
    static var preselectFile: URL { AppInfo.envDir.appendingPathComponent("run/preselect.json") }
    static var upgradeFile: URL { AppInfo.envDir.appendingPathComponent("run/upgrade.json") }
    // .noindex：Spotlight 不收录，旧版不会被登记进 LaunchServices（顶层 CLAUDE.md；2026-10-16 审查发现原来叫 previous）
    static var previousDir: URL { AppInfo.envDir.appendingPathComponent("previous.noindex", isDirectory: true) }
    /// 升级时先把新版拷到这里、核对好，再一步换上（.noindex：Spotlight 不收录）
    static var stagingDir: URL { AppInfo.envDir.appendingPathComponent("staging.noindex", isDirectory: true) }

    /// 这份主控是不是装好的那份（/Applications 或 ~/Applications，新旧名字）：不是（例如 previous.noindex 里的旧版被人打开）就不起
    /// 主控进程，免得抢 socket、再起一个引擎。两处各有一份时只认 installed 那份（优先 /Applications）
    static var isCanonicalCopy: Bool {
        if AppInfo.isLab || ProcessInfo.processInfo.environment["LUOKE_ALLOW_ANY_COPY"] == "1" { return true }
        guard let cur = installed else { return false }
        return Bundle.main.bundleURL.standardizedFileURL.resolvingSymlinksInPath().path == cur.standardizedFileURL.resolvingSymlinksInPath().path
    }

    static func builtAt(_ releaseJSON: URL) -> Double { (Manifests.json(releaseJSON.path)?["built_at"] as? NSNumber)?.doubleValue ?? 0 }
    static func releaseName(_ releaseJSON: URL) -> String { Manifests.json(releaseJSON.path)?["release"] as? String ?? "?" }

    static func writeUpgrade(from: String, to: String) {
        try? FileManager.default.createDirectory(at: upgradeFile.deletingLastPathComponent(), withIntermediateDirectories: true)
        let obj: [String: Any] = ["from": from, "to": to, "at": Date().timeIntervalSince1970]
        if let d = try? JSONSerialization.data(withJSONObject: obj) { try? d.write(to: upgradeFile) }
    }

    /// 新装上的主控启动时取走"刚升级"的标记（取完就删）：据此显示升级页，把应用、引擎、模型也更新到清单的版本
    static func takeUpgrade() -> (from: String, to: String)? {
        guard let d = FileManager.default.contents(atPath: upgradeFile.path),
              let o = (try? JSONSerialization.jsonObject(with: d)) as? [String: Any] else { return nil }
        try? FileManager.default.removeItem(at: upgradeFile)
        return (o["from"] as? String ?? "?", o["to"] as? String ?? "?")
    }
    static let lsregister = AppPlacement.lsregister

    /// 这次是不是以安装器身份启动的
    static func isInstallerLaunch() -> Bool {
        if ProcessInfo.processInfo.environment["LUOKE_NO_INSTALLER"] == "1" || AppInfo.isLab { return false }
        let url = Bundle.main.bundleURL.standardizedFileURL
        if candidates.contains(where: { $0.standardizedFileURL.path == url.path }) { return false }     // 装好的位置上（拖进去的也算）
        return url.lastPathComponent != "\(AppInfo.appName).app" || url.path.hasPrefix("/Volumes/") || url.path.contains("/AppTranslocation/")
    }

    /// 主控 DMG 里的「螺壳」被直接双击（不是应用的安装器）：提示先拖进「应用程序」（ADR 0012）
    static var isUndraggedLaunch: Bool {
        let url = Bundle.main.bundleURL.standardizedFileURL
        return url.lastPathComponent == "\(AppInfo.appName).app" && (url.path.hasPrefix("/Volumes/") || url.path.contains("/AppTranslocation/"))
    }

    /// 按文件名预选："安装邮件助手.app" → 清单里 installer 字段相同的配件标识
    static func preselect() -> String? {
        let name = Bundle.main.bundleURL.lastPathComponent
        return Manifests.items().first(where: { ($0.value["installer"] as? String) == name })?.key
    }

    static func writePreselect(_ accessory: String) {
        try? FileManager.default.createDirectory(at: preselectFile.deletingLastPathComponent(), withIntermediateDirectories: true)
        let obj: [String: Any] = ["accessory": accessory, "at": Date().timeIntervalSince1970]
        if let d = try? JSONSerialization.data(withJSONObject: obj) { try? d.write(to: preselectFile) }
    }

    /// 已装的主控在启动或再次打开时取走预选（取完就删）
    static func takePreselect() -> String? {
        guard let d = FileManager.default.contents(atPath: preselectFile.path),
              let o = (try? JSONSerialization.jsonObject(with: d)) as? [String: Any] else { return nil }
        try? FileManager.default.removeItem(at: preselectFile)
        return o["accessory"] as? String
    }

    @discardableResult
    static func shell(_ cmd: String, _ args: [String]) -> Int32 {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: cmd)
        p.arguments = args
        p.standardOutput = FileHandle.nullDevice
        p.standardError = FileHandle.nullDevice
        do { try p.run() } catch { return -1 }
        p.waitUntilExit()
        return p.terminationStatus
    }

    /// 把控制交给 ~/Applications/螺壳.app，然后退出。安装器和它是同一个 bundle id：直接 openApplication 会被 LaunchServices
    /// 当成"打开已在跑的那个"落回安装器自己（2026-10-15 在 16 GB 测试机实测：安装器收到 reopen、取走预选、显示首次运行，再按流程退出，
    /// 什么都没剩下）。所以：它已经在跑就只激活它（它每 3 秒取一次 run/preselect.json），没在跑就明确要求起一个新进程。
    static func handOff(to url: URL) {
        let me = ProcessInfo.processInfo.processIdentifier
        let bid = Bundle.main.bundleIdentifier ?? "ai.luoke.core"
        let target = url.standardizedFileURL.path
        let running = NSRunningApplication.runningApplications(withBundleIdentifier: bid)
            .filter { $0.processIdentifier != me && $0.bundleURL?.standardizedFileURL.path == target }
        if let app = running.first {
            app.activate(options: [])
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.3) { NSApp.terminate(nil) }
            return
        }
        AppPlacement.open(url, newInstance: true) { _ in NSApp.terminate(nil) }
    }

    /// 安装器流程；出错时弹一个框说明，不动已有的东西
    static func run() {
        let pre = preselect()
        if let current = installed {
            // 已装着主控：这份安装器更新（清单的 built_at 更大）就升级——换掉主控，再由新主控把引擎、模型更新到清单的版本，
            // 用户数据和下载过的模型都不动（2026-10-16 定，进 beta1；V1 §6）；不更新就照旧只加装预选的应用
            let installedRel = current.appendingPathComponent("Contents/Resources/release.json")
            let mineRel = URL(fileURLWithPath: AppInfo.release)
            if builtAt(mineRel) > builtAt(installedRel) {
                // 换包要先等旧主控退出、再换新版，大约一分钟；这段时间旧主控已经没了、新主控还没起，之前屏幕上什么都没有
                // （10-21 负责人：升级窗口没了、菜单栏也没图标，不知道发生了什么）。所以安装器显示升级卡片，活在后台线程里做
                let from = releaseName(installedRel), to = releaseName(mineRel)
                InstallerProgress.show(title: "正在升级到 \(to)", text: "螺壳会先退出，换好新版后自己重新打开，大约一分钟。你的数据和下载过的模型都不动。")
                // 换包没成时卡片上的两个按钮：重试 = 再换一次；先用着 = 打开原来那份
                InstallerProgress.model?.retry = { DispatchQueue.global().async { upgrade(from: from, to: to, preselect: pre) } }
                InstallerProgress.model?.later = { handOff(to: installed ?? current) }
                DispatchQueue.global().async { upgrade(from: from, to: to, preselect: pre) }
                return
            }
            if let p = pre { writePreselect(p) }
            handOff(to: current)
            return
        }
        // 主控 DMG 里的「螺壳」被直接双击（ADR 0012）：先请用户拖进「应用程序」，也可以让我们装进去
        if isUndraggedLaunch {
            NSApp.activate()
            let a = NSAlert()
            a.messageText = "先把「\(AppInfo.appName)」拖进「应用程序」"
            a.informativeText = "在刚才打开的窗口里，把左边的「\(AppInfo.appName)」拖到右边的「应用程序」，再从「应用程序」里打开它。"
            a.addButton(withTitle: "好")
            a.addButton(withTitle: "帮我装进去")
            if a.runModal() == .alertFirstButtonReturn { NSApp.terminate(nil); return }
        }
        if targetTaken {
            let a = NSAlert()
            a.messageText = "装不进「应用程序」文件夹"
            a.informativeText = takenMessage
            a.runModal()
            NSApp.terminate(nil)
            return
        }
        let dest = target      // 新装：/Applications 写得进去就放那里，不然放 ~/Applications（新装不为位置要授权）
        if !AppPlacement.copy(Bundle.main.bundleURL, to: dest, register: true) {
            let a = NSAlert()
            a.messageText = "装不进「应用程序」文件夹"
            a.informativeText = "把螺壳拷到 \(dest.deletingLastPathComponent().path) 失败了。可以把它拖进「应用程序」文件夹，再从那里打开。"
            a.runModal()
            NSApp.terminate(nil)
            return
        }
        if let p = pre { writePreselect(p) }
        handOff(to: dest)
    }

    /// 升级主控本体（ADR 0012）：新版拷到暂存处、核对签名 → 起换包脚本（目标写不进去就先弹管理员授权框；取消了什么都不动）
    /// → 结束已装的主控（同 bundle id，按路径挑）和它里面的记忆进程 → 自己退出；脚本等我们退出后把旧版挪进 previous.noindex、
    /// 新版换上、用 open 打开新版（它看到"刚升级"的标记就显示升级卡片）。登录项、命令行链接由新版启动时跟上新位置。
    static func upgrade(from: String, to: String, preselect: String?) {
        let me = ProcessInfo.processInfo.processIdentifier
        let bid = Bundle.main.bundleIdentifier ?? "ai.luoke.core"
        guard let current = installed else { DispatchQueue.main.async { fail("升级失败", "找不到已装的螺壳。") }; return }
        if targetTaken {      // 新名字被别的应用占着：先不动装着的那份
            DispatchQueue.main.async { fail("升级失败", takenMessage + "已装的螺壳没动，照常能用。") }
            return
        }
        // 先在旁边（环境目录的 staging.noindex，不在「应用程序」里）拷好新版、核对签名，再动装着的那份（10-23 定：
        // 之前先挪走旧版再往原处拷，拷到一半被系统挡住，留下半份被报"已损坏"）
        let staged = stagingDir.appendingPathComponent("\(AppInfo.appName).app")
        guard AppPlacement.copy(Bundle.main.bundleURL, to: staged, register: false),
              shell("/usr/bin/codesign", ["--verify", "--deep", "--strict", staged.path]) == 0 else {
            try? FileManager.default.removeItem(at: staged)
            DispatchQueue.main.async { fail("升级失败", "没能把新版准备好（拷贝或核对签名没过）。已装的螺壳没动，照常能用。") }
            return
        }
        writeUpgrade(from: from, to: to)
        if let why = Swap.start(current: current, staged: staged, target: target, waitPID: me, admin: needsAdmin) {
            try? FileManager.default.removeItem(at: upgradeFile)
            try? FileManager.default.removeItem(at: staged)
            DispatchQueue.main.async { fail("升级失败", why) }
            return
        }
        if let p = preselect { writePreselect(p) }       // 脚本起来了才写：授权取消时旧主控不会取走它、在失败卡片后面装应用（10-25 核出）
        // 脚本在等我们退出。先让已装的主控退出（它退出时会停引擎和打分进程，最多几十秒），再自己退出
        let target = current.standardizedFileURL.path
        let running = NSRunningApplication.runningApplications(withBundleIdentifier: bid)
            .filter { $0.processIdentifier != me && $0.bundleURL?.standardizedFileURL.path == target }
        running.forEach { $0.terminate() }
        var deadline = Date().addingTimeInterval(45)
        while Date() < deadline, running.contains(where: { !$0.isTerminated }) { Thread.sleep(forTimeInterval: 0.3) }
        running.filter { !$0.isTerminated }.forEach { $0.forceTerminate() }
        deadline = Date().addingTimeInterval(5)
        while Date() < deadline, running.contains(where: { !$0.isTerminated }) { Thread.sleep(forTimeInterval: 0.3) }
        Memory.stopAll(hubApp: current)      // 旧主控里跑着的记忆界面 / learn：只按命令行 -m luoke_mem 认，请它们退出再搬
        DispatchQueue.main.async { NSApp.terminate(nil) }
    }

    private static func fail(_ title: String, _ text: String) {
        if InstallerProgress.fail(text) { return }
        let a = NSAlert()
        a.messageText = title
        a.informativeText = text
        a.runModal()
        NSApp.terminate(nil)
    }
}
