// 首次运行流程（V1 §6）：预检 → 勾选配件 → 一屏同意 → 下载安装 → 冒烟检查 → 登录项 → 启动常驻配件。
// 有发布清单（Resources/release.json）就按清单：列清单里的配件、算大小、让主控下载模型 / 引擎包 / 配件 .app 并装到位；
// 没有清单（开发版）就从本机来源目录（LUOKE_MODEL_SOURCE，默认 ~/.omlx/models）放模型链接、只认已经在 ~/Applications 的配件。
// 安装器按文件名预选一个配件（Installer.preselect）；主控已装、首次运行已走过时，再运行某个安装器就只走"加装"：勾选 → 同意 → 下载安装 → 启动。
import AppKit
import ServiceManagement
import SwiftUI

struct AccessoryChoice: Identifiable {
    let id: String          // 配件标识
    let name: String
    let bundleID: String
    let type: String        // app / component / input-method
    let memory: Bool
    let size: Int64         // 清单里的 zip 大小（没有清单时 0）
    let installed: Bool
    let unavailable: String?   // 这台 Mac 装不了的原因（例如 需要 macOS 26），非空就不能勾
    var selected: Bool
}

enum FirstRun {
    static let doneKey = "firstRunDone"

    static func needed() -> Bool {
        return !UserDefaults.standard.bool(forKey: doneKey) && ProcessInfo.processInfo.environment["LUOKE_SKIP_FIRST_RUN"] != "1"
    }

    static var window: NSWindow?

    static func show(preselect: String? = nil) { present(FirstRunModel(preselect: preselect), title: "欢迎使用螺壳") }

    /// 主控已装好、首次运行已走过：再运行某个应用的安装器，在「应用」窗口那一行里装
    static func showAdd(accessory: String) { AppsWindow.install(accessory) }

    private static func present(_ model: FirstRunModel, title: String) {
        if let w = window, w.isVisible { w.close() }
        let host = NSHostingController(rootView: FirstRunView(m: model))
        let w = NSWindow(contentViewController: host)
        w.title = title
        w.styleMask = [.titled, .closable]
        // 规范：首次安装窗口 760×540，不可调整大小
        w.setContentSize(NSSize(width: 760, height: 540))
        w.center()
        w.isReleasedWhenClosed = false
        window = w
        AppWindows.track(w)        // 开着时在程序坞和 ⌘Tab 里（#20）
        Uninstall.bringFront(w)
    }

    static func finish() {
        UserDefaults.standard.set(true, forKey: doneKey)
        window?.close()
    }
}

final class FirstRunModel: ObservableObject {
    enum Step: String { case preflight, accessories, consent, fetching, smoke, done }
    @Published var step: Step = .preflight { didSet { FirstRunModel.mark(step.rawValue); if step == .done { prepareInputMethod(); watchNotifications() } } }
    @Published var inputMethodState = ""     // 输入法 --status 的 state；空 = 还没看
    @Published var tryText = ""
    @Published var refocusTry = 0               // 「切换到螺壳」切过去以后加一：试打框松开焦点再拿回来，让这个进程重新接上输入法（见 selectInputMethod）
    @Published var selectFailed = false          // 「切换到螺壳」没切过去（--select 退出码 3）
    @Published var reregistered = false          // 重新登记过一次了：还切不过去就只能注销重新登录（luoke-ime#15，10-22 复验：同一登录会话里点几次都不行）
    @Published var reregistering = false
    static var otherIMECopies = 0               // --status 报的别的副本数
    /// 完成页上各应用的通知状态（10-20：开发机上邮件助手的通知一直被系统拒，下午的提醒全没看到，测试者也会这样默默收不到）
    struct NotifyRow: Identifiable { let id: String; let name: String; let bundleID: String; let exe: URL; var state: String }
    @Published var notifyRows: [NotifyRow] = []
    private var notifyTimer: Timer?
    private var inputMethodTimer: Timer?
    static let timelineKey = "firstRunTimeline"

    /// 每一步到达的时刻记进 UserDefaults（只有步骤名和时间，诊断信息里带上：估计测试者卡在哪一步、各步多久）
    static func mark(_ step: String) {
        var t = UserDefaults.standard.array(forKey: timelineKey) as? [[String: Any]] ?? []
        t.append(["step": step, "at": Date().timeIntervalSince1970])
        UserDefaults.standard.set(t, forKey: timelineKey)
    }
    @Published var tierText = ""
    @Published var expectText = ""   // 按内存档和芯片照实说体验（顶层 ADR 0017）
    @Published var diskText = ""
    @Published var preflightOK = false
    @Published var accessories: [AccessoryChoice] = []
    @Published var plan: [PlanRow] = []           // 要取的东西：名字、版本、大小
    @Published var releaseNotes = ""              // 升级页的「这一版有什么不同」：和检查更新窗口同一份（发布页的更新说明，brand #15）
    var planLabels: [String: PlanRow] = [:]       // 清单项名字 → 那一行（下载页逐项显示用）
    var fetchStarted: Date?
    var previewDownloads: [String: [String: Any]]?   // 截图用的示例下载状态
    @Published var liveDownloads: [String: [String: Any]] = [:]   // 主控 /status 里这一轮的下载状态（升级卡片显示进度用）
    @Published var planNames: [String] = []        // 交给主控下载的清单项名字
    @Published var progress = ""
    @Published var error: String?
    @Published var installedInputMethod = false
    @Published var failedReasons: [String: String] = [:]   // 没下下来的那几项为什么：主控照实分清"服务器上没有"、"连不上"、"核对不过"（10-19）
    @Published var failed: [String] = []           // 这轮没下下来 / 没装上的清单项（主控报 failed）：检查页给"重试"，完成页照实说
    let preselect: String?
    let upgrade: (from: String, to: String)?       // 升级模式：主控本体已换好，这里只更新应用 / 引擎 / 模型
    var tier: String = "128"
    var memoryTier: String = "128"
    @Published var tierChoices: [Manifests.TierChoice] = []   // 大内存机器：「完整」/「省空间」（issue luoke-core#2）
    @Published var chosenTier: String = "" { didSet { if !chosenTier.isEmpty { tier = chosenTier; refreshTierText() } } }

    init(preselect: String?, upgrade: (from: String, to: String)? = nil) {
        self.preselect = preselect
        self.upgrade = upgrade
        if upgrade == nil { FirstRunModel.mark("preflight") }
        if let up = upgrade, !FirstRunModel.previewing {
            DispatchQueue.global().async { [weak self] in
                let notes = Updater.notes(for: up.to)
                DispatchQueue.main.async { self?.releaseNotes = notes }
            }
        }
    }

    static var previewing = false

    /// 要装输入法、废纸篓里又有同 bundle id 的旧副本吗（只看开头一层，不读内容以外的东西）
    var oldIMEInTrash: Bool {
        guard let ime = accessories.first(where: { $0.type == "input-method" && $0.selected && !$0.installed }) else { return false }
        let trash = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent(".Trash")
        let items = (try? FileManager.default.contentsOfDirectory(at: trash, includingPropertiesForKeys: nil)) ?? []
        return items.contains { $0.pathExtension == "app" && Bundle(url: $0)?.bundleIdentifier == ime.bundleID }
    }
    static let minMemGB = 16
    var memTooLow: Bool { upgrade == nil && Diagnostics.memGB() < FirstRunModel.minMemGB }

    func preflight() {
        let mem = Diagnostics.memGB()
        memoryTier = Manifests.tierFor(memGB: mem)     // 档的分界只在配方 tiers 表里有一份（2026-10-16 自查：原来这里另写了一份）
        // 加装 / 升级时档以主控现在用的为准（用户可能选过省空间）；首次运行给选
        tier = upgrade != nil ? (HubModel.shared.status?.tier ?? Manifests.chosenTier(memGB: mem) ?? memoryTier) : (Manifests.chosenTier(memGB: mem) ?? memoryTier)
        tierChoices = upgrade != nil ? [] : Manifests.tierChoices(memGB: mem)
        if tierChoices.count > 1, chosenTier.isEmpty { chosenTier = tier }
        refreshTierText()
        // 引擎包要求的 macOS（清单项 engine.min_macos，发布脚本按包里的 Mach-O 算出；oMLX 0.7.0 的 mlx 要 26.2）：不够就不往下走
        if let eng = Manifests.items()["engine"], let minOS = eng["min_macos"] as? String, !Manifests.macOSSatisfies(minOS) {
            preflightOK = false
            let v = ProcessInfo.processInfo.operatingSystemVersion
            error = "引擎需要 macOS \(minOS) 或更新，这台 Mac 是 \(v.majorVersion).\(v.minorVersion)；请先升级系统。"
        }
        // 内存低于最低档就不往下走（10-22 定，样板 first-mem）：v1 最低是 16 GB 档，8 GB 的 Mac 跑不动底座
        // （DESIGN 里 8 GB 只跑专用模型是以后的事）。加装、升级时主控已经装着，不拦
        if memTooLow {
            preflightOK = false
            error = "这台 Mac 只有 \(mem) GB 内存，跑不动螺壳的模型。螺壳需要 \(FirstRunModel.minMemGB) GB 或更多内存。"
        }
        loadAccessories()
        // 升级：这台 Mac 的情况不用再看一遍，直接到"要更新的"（步骤条第一步就叫这个；10-21 负责人升级时停在了"先看看这台 Mac"）
        if upgrade != nil && preflightOK && step == .preflight {
            applyTierChoice()
            makePlan()
            step = .consent
        }
    }

    func refreshTierText() {
        let mem = Diagnostics.memGB()
        tierText = "内存 \(mem) GB → \(tier) GB 档" + (tier != memoryTier ? "（选了省空间；按 \(memoryTier) GB 档也行）" : "")
        expectText = FirstRunModel.expectations(tier: tier, chip: Diagnostics.sysctl("machdep.cpu.brand_string"))
        // 磁盘按选的档重算（审查 2026-10-17：之前改选省空间不重算，空间不够装"完整"的人解不开灰掉的"继续"）
        let free = (try? URL(fileURLWithPath: NSHomeDirectory()).resourceValues(forKeys: [.volumeAvailableCapacityForImportantUsageKey]).volumeAvailableCapacityForImportantUsage) ?? 0
        diskText = "可用磁盘 " + Diagnostics.humanSize(free)
        let need = Manifests.diskNeeded(tier: tier)    // 按清单里本档要下的东西算，不另写一份数字
        let diskOK = upgrade != nil || free > need      // 升级时模型多半已经在了，不按整档的数字拦
        let diskMsg = "磁盘可用空间不到 \(Diagnostics.humanSize(need))，装不下引擎和模型。" + (tierChoices.count > 1 && tier == memoryTier ? "可以改选「省空间」。" : "")
        if diskOK {
            if error?.hasPrefix("磁盘可用空间") == true { error = nil }
            preflightOK = error == nil
        } else {
            preflightOK = false
            error = diskMsg
        }
    }

    /// 首次运行选了档：主控还没起，直接写进它的设置；选的就是内存算的档就不写
    func applyTierChoice() {
        guard tierChoices.count > 1 else { return }
        Manifests.writeChosenTier(tier == memoryTier ? nil : tier)
    }

    /// 可装的配件：有清单就按清单（标出已装的），没有就列已知的两个
    func loadAccessories() {
        if FirstRunModel.previewing { return }      // --render-ui：用截图里给的假数据，不读这台 Mac 上装了什么
        let installedIDs = Set(Accessories.installed().map { $0.accessory })
        var found: [AccessoryChoice] = []
        let fromManifest = Manifests.accessories().filter { $0.type != "component" }    // 组件配件（记忆）随声明"读写记忆"的配件装，不单列
        if !fromManifest.isEmpty {
            for a in fromManifest {
                let installed = installedIDs.contains(a.id)
                let unavailable: String? = Manifests.macOSSatisfies(a.minMacOS) ? nil : "需要 macOS \(a.minMacOS ?? "") 或更新；这台 Mac 是 \(ProcessInfo.processInfo.operatingSystemVersion.majorVersion)"
                let selected = unavailable == nil && (preselect == nil ? !installed : (a.id == preselect && !installed))
                found.append(AccessoryChoice(id: a.id, name: a.name, bundleID: a.bundleID, type: a.type, memory: a.memory, size: a.size, installed: installed, unavailable: unavailable, selected: selected))
            }
        } else {
            let known = [("luoke-mail", "螺壳邮件助手", "ai.luoke.mail", "app", true), ("luoke-ime", "螺壳输入法", "ai.luoke.inputmethod.Luoke", "input-method", true)]
            for (id, name, bid, type, mem) in known {
                let installed = installedIDs.contains(id)
                found.append(AccessoryChoice(id: id, name: name, bundleID: bid, type: type, memory: mem, size: 0, installed: installed, unavailable: nil, selected: preselect == nil ? true : id == preselect))
            }
        }
        accessories = found
    }

    /// 照实说这台 Mac 能有什么体验：文字只在配方的 expectations 块里维护一份（顶层 ADR 0017；2026-10-16 自查后从代码里挪出去）。
    /// 芯片类别只用来挑预计的那句（M1 / 基础款 / Pro 以上）；慢不慢最终由主控装好后实测。记忆的"这一档不从打字里归纳"按配方里该任务支持的档来。
    static func expectations(tier: String, chip: String) -> String {
        let recipe = Manifests.recipe() ?? [:]
        let exp = recipe["expectations"] as? [String: Any] ?? [:]
        var lines = ((exp["tiers"] as? [String: Any])?[tier] as? [String]) ?? []
        if let typing = (recipe["tasks"] as? [String: Any])?["luoke-mem.typing"] as? [String: Any],
           let tiers = typing["tiers"] as? [String], !tiers.contains(tier), let note = exp["typing_unsupported"] as? String {
            lines.append(note)
        }
        let pro = chip.contains("Pro") || chip.contains("Max") || chip.contains("Ultra")
        let m1 = chip.contains("M1 ") || chip.hasSuffix("M1")
        if let ime = exp["ime"] as? [String: String], let line = ime[m1 ? "m1" : (pro ? "pro" : "base")] {
            lines.append(line)
        }
        return lines.joined(separator: "\n")
    }

    /// 一屏说清要下载什么：本档的模型（还没有的）、引擎包（没装的）、勾选的配件、随之要装的螺壳记忆
    /// 确认页、升级页的一行：名字（粗）、版本或型号（灰）、大小（右）——样板的写法（10-21 #15）
    struct PlanRow { let name: String; let detail: String; let size: String }

    private func engineLabel() -> String {
        Manifests.engineVersion().replacingOccurrences(of: "omlx-", with: "oMLX ")
    }

    /// 这一档的底座模型（配方 assignment[档].base）；别的都是专用模型（打分、嵌入）
    private func modelRow(_ m: String) -> PlanRow {
        let base = ((Manifests.recipe()?["assignment"] as? [String: Any])?[tier] as? [String: Any])?["base"] as? String
        return PlanRow(name: m == base ? "底座模型" : "专用模型", detail: ModelName.display(Manifests.modelDir(m) ?? m), size: Diagnostics.humanSize(Manifests.itemSize(m)))
    }

    func makePlan() {
        var p: [PlanRow] = []
        var names: [String] = []
        if let up = upgrade, Manifests.hasRelease {
            // 升级：只补引擎包没装的（版本变了就是没装）、本档还没有的模型；数据和已有的模型不动。
            // 应用不在这里换（早会 #13：螺壳和应用各自升级）：有新版的在「应用」页里单独升，旧版靠验证记录照常能用
            p.append(PlanRow(name: "螺壳", detail: "\(up.from) → \(up.to)", size: "已换好"))
            if !Manifests.engineInstalled(), Manifests.items()["engine"] != nil {
                p.append(PlanRow(name: "推理引擎", detail: engineLabel(), size: Diagnostics.humanSize(Manifests.itemSize("engine"))))
                names.append("engine"); planLabels["engine"] = p.last
            }
            for m in Manifests.modelsFor(tier: tier) where !Manifests.modelInstalled(m) {
                p.append(modelRow(m))
                names.append(m); planLabels[m] = p.last
            }
            plan = p
            planNames = names.sorted { (planKind($0), Manifests.itemSize($0)) < (planKind($1), Manifests.itemSize($1)) }
            return
        }
        if Manifests.hasRelease {
            p.append(PlanRow(name: "螺壳", detail: Updater.myRelease, size: "已装好"))
            if !Manifests.engineInstalled(), Manifests.items()["engine"] != nil {
                p.append(PlanRow(name: "推理引擎", detail: engineLabel(), size: Diagnostics.humanSize(Manifests.itemSize("engine"))))
                names.append("engine"); planLabels["engine"] = p.last
            }
            for m in Manifests.modelsFor(tier: tier) where !Manifests.modelInstalled(m) {
                p.append(modelRow(m))
                names.append(m); planLabels[m] = p.last
            }
            let selected = accessories.filter { $0.selected && !$0.installed }
            let byID = Dictionary(Manifests.accessories().map { ($0.id, $0) }, uniquingKeysWith: { a, _ in a })
            for a in selected {
                p.append(PlanRow(name: a.name, detail: byID[a.id]?.version ?? "", size: Diagnostics.humanSize(a.size)))
                names.append(a.id); planLabels[a.id] = p.last
            }
            let installedIDs = Set(Accessories.installed().map { $0.accessory })
            if selected.contains(where: { $0.memory }) || accessories.contains(where: { $0.installed && $0.memory }),
               let mem = Manifests.accessories().first(where: { $0.type == "component" }), !installedIDs.contains(mem.id), !names.contains(mem.id) {
                p.append(PlanRow(name: mem.name, detail: mem.version, size: Diagnostics.humanSize(mem.size)))
                names.append(mem.id); planLabels[mem.id] = p.last
            }
        } else {
            p.append(PlanRow(name: "推理引擎", detail: "oMLX（开发版：用本机已装的）" + (Manifests.engineProblem().map { "——\($0)" } ?? ""), size: "—"))
            let base = tier != "128" ? ("Qwen3.5-9B-4bit", "5.8 GB") : ("gemma-4-26b-a4b-it-8bit", "26 GB")
            p.append(PlanRow(name: "底座模型", detail: ModelName.display(base.0), size: base.1))
            if accessories.contains(where: { $0.selected && $0.id == "luoke-ime" }) { p.append(PlanRow(name: "专用模型", detail: ModelName.display("Qwen3.5-0.8B-Base-4bit"), size: "0.5 GB")) }
        }
        plan = p
        // 顺序：应用 .app（几十 MB，装好就打开，测试者可以趁模型下载时连邮箱）→ 引擎包 → 模型（小的先）
        planNames = names.sorted { (planKind($0), Manifests.itemSize($0)) < (planKind($1), Manifests.itemSize($1)) }
    }

    private func planKind(_ n: String) -> Int {
        guard let it = Manifests.items()[n], let k = it["kind"] as? String else { return 2 }
        return k == "accessory" ? 0 : (k == "engine" ? 1 : 2)
    }

    var totalText: String {
        guard Manifests.hasRelease else { return "" }
        let total = planNames.reduce(Int64(0)) { $0 + Manifests.itemSize($1) }
        return total > 0 ? "合计 " + Diagnostics.humanSize(total) + "（按 20 MB/s 约 \(max(1, total / (20 << 20) / 60)) 分钟）" : ""
    }

    /// "下载"一步：有发布清单就让主控按清单真下载并安装；没有就退回开发版的办法——从本机来源目录放符号链接。
    func fetch() {
        fetchStarted = Date()
        step = .fetching
        let fm = FileManager.default
        try? fm.createDirectory(atPath: AppInfo.modelDir, withIntermediateDirectories: true)
        installedInputMethod = accessories.contains { $0.selected && !$0.installed && $0.type == "input-method" }
        if Manifests.hasRelease {
            progress = "螺壳正在按发布清单下载…"
            if upgrade == nil { HubModel.shared.startHub() }
            let names = planNames      // 升级只补引擎和模型，不换应用（应用在「应用」窗口里各自升级），所以这里不用先结束应用
            DispatchQueue.global().async { self.driveDownloads(names: names) }
            return
        }
        linkFromSource()
        step = .smoke
        HubModel.shared.startHub()     // 现在起主控：它会做冒烟检查，下一步等结果
    }

    /// 经主控下载：POST /downloads {"names": [...]}，然后看 /status 里的进度，直到都完成（主控下完会自己重启引擎、做冒烟检查）。
    private func driveDownloads(names: [String]) {
        let client = HubModel.shared.client
        var started = false
        var refusal: String?
        for _ in 0..<60 {
            do { _ = try client.request("POST", "/downloads", body: ["names": names]); started = true; break }
            catch let e as HubError where e.code == "unsupported" || e.code == "invalid_request" {
                refusal = e.message   // 主控明确拒绝了（例如开发构建没有发布清单）：不用再等，把原因原样给用户（luokelabs/luoke-core#1）
                break
            } catch { Thread.sleep(forTimeInterval: 2) }
        }
        guard started else {
            DispatchQueue.main.async { self.progress = refusal ?? "螺壳没接下载请求（看 app.log）"; self.step = .smoke }
            return
        }
        if names.isEmpty {
            DispatchQueue.main.async { self.progress = "没有要下载的"; self.step = .smoke }
            return
        }
        var opened = Set<String>()
        while true {
            Thread.sleep(forTimeInterval: 1)
            guard let (_, st) = try? client.request("GET", "/status"), let dl = st["downloads"] as? [String: [String: Any]] else { continue }
            var lines: [String] = []
            var allDone = true
            for name in names {
                guard let d = dl[name] else { allDone = false; continue }
                let state = d["state"] as? String ?? "?"
                if state == "done", d["installed"] != nil, !opened.contains(name) {
                    opened.insert(name)       // 应用装好了就打开它，不等模型：常驻的带 --resident，其他 app 类型的照普通打开；输入法和组件配件不打开
                    DispatchQueue.main.async {
                        if let m = Accessories.installed().first(where: { $0.accessory == name }) { Accessories.noRestart.remove(m.bundleID) }   // 升级时压住的重启放开
                        Accessories.launchResident()
                        if let m = Accessories.installed().first(where: { $0.accessory == name }), m.type == "app", !Accessories.isRunning(m.bundleID) {
                            AppPlacement.open(m.app)
                            self.progress = "已装好并打开：\(m.name)。模型还在下载，可以先在应用里把邮箱连上。\n" + self.progress
                        }
                    }
                }
                let done = (d["done"] as? NSNumber)?.int64Value ?? 0
                let total = (d["total"] as? NSNumber)?.int64Value ?? 0
                let pct = total > 0 ? Int(Double(done) / Double(total) * 100) : 0
                lines.append("\(name)：\(state == "done" ? "完成" : (state == "failed" ? "失败 " + (d["error"] as? String ?? "") : "\(pct)%（\(Diagnostics.humanSize(done)) / \(Diagnostics.humanSize(total))）"))")
                if state == "queued" || state == "downloading" { allDone = false }
            }
            DispatchQueue.main.async { self.progress = lines.joined(separator: "\n"); self.liveDownloads = dl }
            if allDone {
                let bad = names.filter { (dl[$0]?["state"] as? String) == "failed" }
                var reasons: [String: String] = [:]
                for n in bad { reasons[n] = (dl[n]?["error"] as? String) ?? "" }
                DispatchQueue.main.async {
                    self.failed = bad
                    self.failedReasons = reasons
                    // 升级时没装上的应用：旧版还在原处（新版核对通过才替换），放开重启压制、拉起旧版（审查 2026-10-17）
                    for n in bad {
                        guard let m = Accessories.installed().first(where: { $0.accessory == n }) else { continue }
                        Accessories.noRestart.remove(m.bundleID)
                        if m.type == "app", Accessories.wantsResident(m), !Accessories.isRunning(m.bundleID) { Accessories.launch(m) }
                    }
                }
                break
            }
        }
        DispatchQueue.main.async {
            self.step = .smoke
        }
    }

    /// 检查页的"重试"：只把没成的那几项再交给主控（主控对点名的项重新排队，网络断过、地址暂时不通这类都能再来）
    func retryFailed() {
        let names = failed
        guard !names.isEmpty else { return }
        failed = []
        step = .fetching
        progress = "重新下载：" + names.joined(separator: "、")
        DispatchQueue.global().async { self.driveDownloads(names: names) }
    }

    /// 输入法是这轮要装的、而且装上了（没下下来的不算）
    var inputMethodReady: Bool {
        installedInputMethod && !accessories.contains { $0.type == "input-method" && failed.contains($0.id) }
    }

    /// 完成页的第一句：有没装上的就照实说，不说"装好了"
    var doneText: String {
        if !failed.isEmpty {
            let names = failed.map { n in Manifests.accessories().first(where: { $0.id == n })?.name ?? n }
            return "没装上：" + names.joined(separator: "、") + "。可以再打开它的安装器重试。"
        }
        return "螺壳会在菜单栏常驻，应用现在可以用模型了。"
    }

    private func linkFromSource() {
        let src = ProcessInfo.processInfo.environment["LUOKE_MODEL_SOURCE"] ?? (NSHomeDirectory() + "/.omlx/models")
        let fm = FileManager.default
        let names = ["local/Qwen3.5-0.8B-Base-4bit", "mlx-community/Qwen3.5-9B-4bit", "gemma-4-26b-a4b-it-8bit", "mlx-community/gemma-4-e4b-it-4bit"]
        var linked: [String] = []
        var missing: [String] = []
        for n in names {
            let from = src + "/" + n
            let to = AppInfo.modelDir + "/" + (n as NSString).lastPathComponent
            if fm.fileExists(atPath: from) {
                try? fm.removeItem(atPath: to)
                do {
                    try fm.createSymbolicLink(atPath: to, withDestinationPath: from)
                    linked.append((n as NSString).lastPathComponent)
                } catch {
                    missing.append(n + "（放不了链接：\(error.localizedDescription)）")
                }
            } else {
                missing.append(n)
            }
        }
        progress = "已放好：" + (linked.isEmpty ? "无" : linked.joined(separator: "、"))
        if !missing.isEmpty { progress += "\n来源目录里没有：" + missing.joined(separator: "、") + "（相关任务会显示需下载）" }
    }

    /// 冒烟检查没通过（例如网络或内存一时的问题）：让主控再做一次，结果从 /status 的 smoke 回来
    func rerunSmoke() {
        progress = "正在重新检查…"
        DispatchQueue.global().async {
            do { _ = try HubModel.shared.client.request("POST", "/smoke", body: [:]) }
            catch { DispatchQueue.main.async { self.progress = "没能发起：" + ((error as? HubError)?.message ?? error.localizedDescription) } }
        }
    }

    func registerLoginItem() {
        do {
            try SMAppService.mainApp.register()
            progress = "已注册登录项"
        } catch {
            progress = "登录项没注册上：\(error.localizedDescription)（开发版未签名时系统可能拒绝）"
        }
    }

    func launchResidentAccessories() {
        Accessories.launchResident()
    }

    /// 输入法这轮装上了：每秒看一次状态；添加靠用户手动（macOS 26 上 0.1.8 及更早的 `--register` 弹出的"允许"确认点了也没加上，
    /// luoke-ime 10-20 在 32 GB 测试机实测）。只在 `--status` 答 not_registered 时调一次 `--register`（0.1.9 起只注册、不弹），完成页据此改说法（issue #13，和 luoke-ime 10-20 商定）。状态优先问输入法自己的 `--status`（0.1.9 起：
    /// not_registered / not_added / added_not_selectable / ready，每次新起进程现读，在起 IMKServer 之前就退出）；旧版没有这个命令
    /// （退出码非 0）就退回读 com.apple.inputsources，只分得出添没添加。
    func prepareInputMethod() {
        guard inputMethodReady, let ime = Accessories.installed().first(where: { $0.type == "input-method" }),
              let exe = Bundle(url: ime.app)?.executableURL else { return }
        let bid = ime.bundleID
        DispatchQueue.global().async {
            let first = FirstRunModel.inputMethodState(exe: exe, bundleID: bid)
            // 系统还没收录螺壳：调一次 --register，让它早点出现在「编辑… → +」的列表里（0.1.9 起在 macOS 26 上只注册、不弹确认；
            // not_registered 只有 --status 能答出来，也就只有 0.1.9 起才会走到这里，旧版的 --register 还会弹那个没用的确认）
            if first == "not_registered" { Installer.shell(exe.path, ["--register"]) }
            DispatchQueue.main.async {
                self.inputMethodState = first
                self.inputMethodTimer?.invalidate()
                guard first != "ready" else { FirstRunModel.startInputMethod(ime); return }
                self.inputMethodTimer = Timer.scheduledTimer(withTimeInterval: 1, repeats: true) { [weak self] t in
                    guard let self else { t.invalidate(); return }
                    DispatchQueue.global().async {
                        let st = FirstRunModel.inputMethodState(exe: exe, bundleID: bid)
                        DispatchQueue.main.async {
                            self.inputMethodState = st
                            if st == "ready" { t.invalidate(); FirstRunModel.startInputMethod(ime) }
                        }
                    }
                }
            }
        }
    }

    /// 刚添加好：输入法服务没在跑就在后台拉起来。10-27 16 GB 测试机：添加完在试打框里切到螺壳，按键原样成了英文——这个进程从没向系统要过
    /// 输入法的连接，系统也就没拉起服务；换到别的应用打字服务才起来（luoke-ime 查的日志）。
    /// 光有服务还不够，管用的是切过去后试打框重新聚焦（refocusTry；10-27 16 GB 测试机五种情况对照，输入法 0.1.20 / 0.1.21 都一样）
    static func startInputMethod(_ ime: Accessories.Manifest) {
        guard NSRunningApplication.runningApplications(withBundleIdentifier: ime.bundleID).isEmpty else { return }
        AppPlacement.open(ime.app, background: true)
    }

    /// 输入法的状态：`--status` 那一行 JSON 的 state；旧版输入法没有这个命令时，已添加就当 ready、没添加当 not_added
    static func inputMethodState(exe: URL, bundleID: String) -> String {
        let p = Process()
        p.executableURL = exe
        p.arguments = ["--status"]
        let out = Pipe()
        p.standardOutput = out
        p.standardError = FileHandle.nullDevice
        if (try? p.run()) != nil {
            let data = out.fileHandleForReading.readDataToEndOfFile()
            p.waitUntilExit()
            if p.terminationStatus == 0, let o = try? JSONSerialization.jsonObject(with: data) as? [String: Any], let st = o["state"] as? String {
                let others = (o["other_copies"] as? [Any])?.count ?? 0     // 0.1.11 起：LaunchServices 认到的别的副本（luoke-ime#15）
                DispatchQueue.main.async { FirstRunModel.otherIMECopies = others }
                return st
            }
        }
        return Uninstall.inputSourceEnabled(bundleID: bundleID) ? "ready" : "not_added"
    }

    /// 完成页上用户点「切换到螺壳」：在向导里、刚添加好时替用户切过去是合理的（不在后台切；luoke-ime 10-20）
    /// 每个普通应用的启动器答 `--notify-status`（luoke-core b3c7131 起的启动器才有；旧的答不出 JSON 就不列）：
    /// ok（允许且横幅开着）/ ask（还没问过）/ off（被拒或横幅关了）
    static func notifyState(exe: URL) -> String? {
        let p = Process()
        p.executableURL = exe
        p.arguments = ["--notify-status"]
        let out = Pipe()
        p.standardOutput = out
        p.standardError = FileHandle.nullDevice
        guard (try? p.run()) != nil else { return nil }
        let data = out.fileHandleForReading.readDataToEndOfFile()
        p.waitUntilExit()
        guard p.terminationStatus == 0, let o = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let auth = o["authorization"] as? String else { return nil }
        if auth == "not_determined" { return "ask" }
        if ["authorized", "provisional", "ephemeral"].contains(auth) && (o["alert"] as? String) != "disabled" { return "ok" }
        return "off"
    }

    func watchNotifications() {
        let apps = Accessories.installed().filter { $0.type == "app" }
        DispatchQueue.global().async {
            let rows = apps.compactMap { m -> NotifyRow? in
                guard let exe = Bundle(url: m.app)?.executableURL, let st = FirstRunModel.notifyState(exe: exe) else { return nil }
                return NotifyRow(id: m.accessory, name: m.name, bundleID: m.bundleID, exe: exe, state: st)
            }
            DispatchQueue.main.async {
                self.notifyRows = rows
                self.notifyTimer?.invalidate()
                guard rows.contains(where: { $0.state != "ok" }) else { return }
                self.notifyTimer = Timer.scheduledTimer(withTimeInterval: 2, repeats: true) { [weak self] t in
                    guard let self else { t.invalidate(); return }
                    let cur = self.notifyRows
                    DispatchQueue.global().async {
                        let upd = cur.map { r -> NotifyRow in var r = r; r.state = FirstRunModel.notifyState(exe: r.exe) ?? r.state; return r }
                        DispatchQueue.main.async {
                            self.notifyRows = upd
                            if !upd.contains(where: { $0.state != "ok" }) { t.invalidate() }
                        }
                    }
                }
            }
        }
    }

    /// 还没问过：以这个应用的名义发一条测试通知，系统会先弹出"允许通知"的询问
    func askNotifications(_ r: NotifyRow) {
        DispatchQueue.global().async {
            let p = Process()
            p.executableURL = r.exe
            p.arguments = ["--notify", r.name, "", "通知打开了：有事会在这里提醒你。"]
            p.standardOutput = FileHandle.nullDevice
            p.standardError = FileHandle.nullDevice
            try? p.run()
            p.waitUntilExit()
        }
    }

    func openNotificationSettings(_ r: NotifyRow) {
        if let u = URL(string: "x-apple.systempreferences:com.apple.Notifications-Settings.extension?id=\(r.bundleID)") { NSWorkspace.shared.open(u) }
    }

    /// `--select` 的退出码（0.1.11 起另起进程核对）：0 切过去了，1 调用失败，3 调用成功但系统没切过去——多半是系统把输入源
    /// 绑到了别的副本上（luoke-ime#15），给「重新登记输入法」。0.1.10 及更早只有 0 / 1
    func selectInputMethod() {
        guard let ime = Accessories.installed().first(where: { $0.type == "input-method" }),
              let exe = Bundle(url: ime.app)?.executableURL else { return }
        DispatchQueue.global().async {
            let code = Installer.shell(exe.path, ["--select"])
            DispatchQueue.main.async {
                self.selectFailed = code == 3
                if code == 0 { self.refocusTry += 1 }
            }
        }
    }

    /// 重新登记：注销同 bundle id 的别的副本，把装着的这份挪开再放回、登记（POST /accessories/reregister），然后再切一次
    func reregisterInputMethod() {
        guard let ime = Accessories.installed().first(where: { $0.type == "input-method" }) else { return }
        reregistering = true
        DispatchQueue.global().async {
            let ok = (try? HubModel.shared.client.request("POST", "/accessories/reregister", body: ["accessory": ime.accessory], timeout: 60)) != nil
            DispatchQueue.main.async {
                self.reregistering = false
                self.reregistered = true
                if ok { self.selectInputMethod() } else { self.selectFailed = true }
            }
        }
    }

    func openKeyboardSettings() {
        if let url = URL(string: "x-apple.systempreferences:com.apple.Keyboard-Settings.extension") { NSWorkspace.shared.open(url) }
    }
}

struct FirstRunView: View {
    @ObservedObject var m: FirstRunModel
    @FocusState private var tryFocused: Bool
    @ObservedObject private var hub = HubModel.shared

    /// 步骤（luoke-core#15，brand ui/src/install/Install.tsx）：首次安装 5 步；本机试跑并在「下载安装」里。升级不走这里（UpgradeWindow）
    private let steps = ["这台 Mac", "选应用", "确认下载", "下载安装", "完成"]
    private var stepIndex: Int {
        [FirstRunModel.Step.preflight: 0, .accessories: 1, .consent: 2, .fetching: 3, .smoke: 3, .done: 4][m.step] ?? 0
    }

    var body: some View {
        HStack(spacing: 0) {
            LKRail(steps: steps, current: stepIndex)
            VStack(spacing: 0) {
                ScrollView {
                    VStack(alignment: .leading, spacing: 0) { page }
                        .frame(maxWidth: .infinity, alignment: .topLeading)
                        .padding(.horizontal, 32).padding(.top, 28).padding(.bottom, 16)
                }
                .scrollIndicators(.never)
                footer
            }
            .background(Color.lkBackground)
        }
        .foregroundStyle(Color.lkForeground)
        .frame(width: 760, height: 540)
        .onAppear { m.preflight() }
    }

    // ---- 每一页 -------------------------------------------------------------------------------------------

    @ViewBuilder private var page: some View {
        switch m.step {
        case .preflight: preflightPage
        case .accessories: appsPage
        case .consent: consentPage
        case .fetching, .smoke: fetchPage
        case .done: donePage
        }
    }

    private var preflightPage: some View {
        let mem = Diagnostics.memGB()
        let chip = Diagnostics.sysctl("machdep.cpu.brand_string")
        let v = ProcessInfo.processInfo.operatingSystemVersion
        let facts: [(String, String, String)] = [
            ("cpu", "芯片", chip.isEmpty ? "Apple 芯片" : chip),
            ("memorychip", "内存", "\(mem) GB"),
            ("internaldrive", "可用磁盘", m.diskText.replacingOccurrences(of: "可用磁盘 ", with: "")),
            ("desktopcomputer", "系统", "macOS \(v.majorVersion).\(v.minorVersion)" + (v.patchVersion > 0 ? ".\(v.patchVersion)" : "")),
        ]
        let choice = Manifests.tierChoices(memGB: mem).first { $0.tier == m.tier }
        return VStack(alignment: .leading, spacing: 0) {
            LKTitle(title: "先看看这台 Mac", sub: m.memTooLow ? "这台 Mac 现在还装不了。" : nil)
            HStack(spacing: 8) {
                ForEach(facts, id: \.1) { f in
                    let bad = f.0 == "memorychip" && m.memTooLow
                    VStack(alignment: .leading, spacing: 2) {
                        Image(systemName: f.0).font(.system(size: 14)).foregroundStyle(Color.lkMuted).padding(.bottom, 6)
                        Text(f.1).font(.system(size: 12)).foregroundStyle(Color.lkMuted)
                        Text(f.2).font(.system(size: 14, weight: .medium)).lineLimit(1).minimumScaleFactor(0.7)
                            .foregroundStyle(bad ? Color.lkDanger : Color.lkForeground)
                    }
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(.horizontal, 12).padding(.vertical, 10)
                    .background(Color.lkCard).clipShape(.rect(cornerRadius: 8))
                    .overlay(RoundedRectangle(cornerRadius: 8).stroke(bad ? Color.lkDanger.opacity(0.5) : Color.lkBorder))
                }
            }
            if m.tierChoices.count > 1 {
                Text("占多少空间").font(.system(size: 14, weight: .medium)).padding(.top, 24).padding(.bottom, 8)
                VStack(spacing: 8) {
                    ForEach(m.tierChoices, id: \.tier) { c in
                        let on = m.chosenTier == c.tier
                        Button { m.chosenTier = c.tier } label: {
                            HStack(alignment: .top, spacing: 10) {
                                Image(systemName: on ? "largecircle.fill.circle" : "circle").foregroundStyle(on ? Color.lkPrimary : Color.lkInput)
                                VStack(alignment: .leading, spacing: 2) {
                                    HStack(spacing: 8) {
                                        Text(c.label).font(.system(size: 14, weight: .medium))
                                        Text(ModelName.display(c.base)).font(.system(size: 13)).foregroundStyle(Color.lkMuted)
                                        Spacer()
                                        Text("约 \(String(format: "%.0f", c.sizeGB)) GB").font(.system(size: 13)).foregroundStyle(Color.lkMuted).monospacedDigit()
                                    }
                                }
                            }
                            .padding(14).contentShape(Rectangle())
                            .background(on ? Color.lkMutedBg : Color.lkCard).clipShape(.rect(cornerRadius: 12))
                            .overlay(RoundedRectangle(cornerRadius: 12).stroke(on ? Color.lkPrimary : Color.lkBorder))
                        }
                        .buttonStyle(.plain)
                    }
                }
                Text("装完也能在螺壳的设置里换；换了会下载另一个模型。").font(.system(size: 13)).foregroundStyle(Color.lkMuted).padding(.top, 8)
            } else if let c = choice, !m.memTooLow {
                Text("会装的模型").font(.system(size: 14, weight: .medium)).padding(.top, 24).padding(.bottom, 8)
                VStack(alignment: .leading, spacing: 8) {
                    HStack(spacing: 8) {
                        LKBadge(text: ModelName.display(c.base), kind: .outline)
                        Text("\(c.tier) GB 档").font(.system(size: 12)).foregroundStyle(Color.lkMuted)
                        Spacer()
                        Text("约 \(String(format: "%.0f", c.sizeGB)) GB").font(.system(size: 14)).monospacedDigit()
                    }
                    if !m.expectText.isEmpty { Text(m.expectText).font(.system(size: 13)).foregroundStyle(Color.lkMuted).fixedSize(horizontal: false, vertical: true) }
                }
                .padding(14).background(Color.lkCard).clipShape(.rect(cornerRadius: 12))
                .overlay(RoundedRectangle(cornerRadius: 12).stroke(Color.lkBorder))
            }
            if let e = m.error { LKNote(icon: "exclamationmark.triangle", text: e, kind: .danger).padding(.top, 16) }
        }
    }

    private func appIcon(_ id: String) -> NSImage? {
        LKAsset.image(["luoke-mail": "app-mail.png", "luoke-ime": "app-ime.png"][id] ?? "app-core.png")
    }

    private var appsPage: some View {
        VStack(alignment: .leading, spacing: 0) {
            LKTitle(title: "要装哪些应用", sub: nil)
            LKGroup {
                do {
                    HStack(spacing: 14) {
                        if let img = appIcon("luoke-core") { Image(nsImage: img).resizable().frame(width: 44, height: 44).clipShape(.rect(cornerRadius: 10)) }
                        VStack(alignment: .leading, spacing: 2) {
                            HStack(spacing: 8) { Text("螺壳").font(.system(size: 14, weight: .medium)); LKBadge(text: "必装", kind: .secondary) }
                            Text("在本机跑模型").font(.system(size: 13)).foregroundStyle(Color.lkMuted)
                        }
                        Spacer()
                        Image(systemName: "checkmark").font(.system(size: 13, weight: .semibold)).foregroundStyle(Color.lkMuted)
                    }
                    .padding(.horizontal, 16).padding(.vertical, 14)
                    Divider().overlay(Color.lkBorder)
                }
                ForEach(Array($m.accessories.enumerated()), id: \.offset) { i, $a in
                    if i > 0 { Divider().overlay(Color.lkBorder) }
                    HStack(spacing: 14) {
                        if let img = appIcon(a.id) { Image(nsImage: img).resizable().frame(width: 44, height: 44).clipShape(.rect(cornerRadius: 10)) }
                        VStack(alignment: .leading, spacing: 2) {
                            HStack(spacing: 8) {
                                Text(a.name).font(.system(size: 14, weight: .medium))
                                if a.installed { LKBadge(text: "已装", kind: .secondary) }
                            }
                            Text(a.unavailable ?? (a.type == "input-method" ? "本机联想的中文输入法"
                                                   : (a.memory ? "用本机模型理收件箱" : "")))
                                .font(.system(size: 13)).foregroundStyle(a.unavailable == nil ? Color.lkMuted : Color.lkDanger)
                                .fixedSize(horizontal: false, vertical: true)
                        }
                        Spacer()
                        Toggle("", isOn: $a.selected).toggleStyle(.checkbox).labelsHidden().tint(.lkPrimary)
                            .disabled(a.installed || a.unavailable != nil)
                    }
                    .padding(.horizontal, 16).padding(.vertical, 14)
                }
            }
        }
    }

    private var consentPage: some View {
        VStack(alignment: .leading, spacing: 0) {
            LKTitle(title: "要下载这些", sub: nil)
            LKGroup {
                ForEach(Array(m.plan.enumerated()), id: \.offset) { i, item in
                    if i > 0 { Divider().overlay(Color.lkBorder) }
                    HStack(spacing: 8) {
                        Text(item.name).font(.system(size: 14, weight: .medium))
                        Text(item.detail).font(.system(size: 13)).foregroundStyle(Color.lkMuted).lineLimit(1)
                        Spacer()
                        Text(item.size).font(.system(size: 14)).foregroundStyle(Color.lkMuted).monospacedDigit()
                    }
                    .padding(.horizontal, 16).padding(.vertical, 10)
                }
            }
            if !m.totalText.isEmpty { Text(m.totalText).font(.system(size: 13)).foregroundStyle(Color.lkMuted).padding(.top, 8) }
            VStack(spacing: 8) {
                if m.oldIMEInTrash {
                    // luoke-ime#15：旧的输入法副本还在废纸篓里时，同一个登录会话里装新版，系统常常还绑着旧的、切不过去
                    LKNote(icon: "trash", text: "废纸篓里有旧的螺壳输入法。先清空废纸篓、注销并重新登录一次再装，输入法更容易一次就能用；也可以直接装，装完切不过去时再重新登录。", kind: .warning)
                }
                if Manifests.hasRelease {
                    LKNote(icon: "checkmark.shield", text: "螺壳和引擎来自 GitHub Releases，模型来自 ModelScope。每个文件核对过哈希才会用。")
                    LKNote(icon: "magnifyingglass", text: "下载前会先在本机找同一个模型（oMLX、Hugging Face、LM Studio 的目录）。找到了就直接用，不再下载，也不动原来的文件。")
                }
            }
            .padding(.top, 16)
        }
    }

    /// 下载页逐项一行：状态图标、名字、已下 / 总大小或"已装好"（样板 Install.tsx 的 Fetching）
    private struct FetchRow: Identifiable { let id: String; let label: String; let state: LKStateIcon.State; let right: String; var detail: String? = nil }

    private var downloads: [String: [String: Any]] { m.previewDownloads ?? hub.status?.downloads ?? [:] }

    private var fetchRows: [FetchRow] {
        // 第一行是主控自己：这时它已经装好 / 换好了（样板 first-fetch-failed）
        [FetchRow(id: "hub", label: "螺壳", state: .done, right: "已装好")] + m.planNames.map { n in
            let d = downloads[n] ?? [:]
            let label = m.planLabels[n].map { $0.name + ($0.detail.isEmpty ? "" : "  ") } ?? n
            let total = (d["total"] as? NSNumber)?.int64Value ?? Manifests.itemSize(n)
            let done = (d["done"] as? NSNumber)?.int64Value ?? 0
            switch d["state"] as? String {
            case "done": return FetchRow(id: n, label: label, state: .done, right: "已装好")
            case "failed":
                let why = m.failedReasons[n].flatMap { $0.isEmpty ? nil : $0 } ?? "没下下来，可以重试。"
                return FetchRow(id: n, label: label, state: .fail,
                                right: done > 0 ? "\(Diagnostics.humanSize(done)) / \(Diagnostics.humanSize(total))" : Diagnostics.humanSize(total), detail: why)
            case "downloading": return FetchRow(id: n, label: label, state: .busy, right: "\(Diagnostics.humanSize(done)) / \(Diagnostics.humanSize(total))")
            default: return FetchRow(id: n, label: label, state: .wait, right: Diagnostics.humanSize(total))
            }
        }
    }

    /// 总进度：已下字节 / 总字节；剩余时间按开始以来的平均速度估。
    /// 头 10 秒、下到 2% 以前不估：开头连上、测速的那几秒把平均速度拉得很低（10-27 16 GB 测试机：开头写"还剩约 35 分钟"，实际 6 分钟下完）
    private var fetchProgress: (fraction: Double, text: String) {
        var done: Int64 = 0, total: Int64 = 0
        for n in m.planNames {
            let d = downloads[n] ?? [:]
            let t = (d["total"] as? NSNumber)?.int64Value ?? Manifests.itemSize(n)
            total += t
            done += (d["state"] as? String) == "done" ? t : ((d["done"] as? NSNumber)?.int64Value ?? 0)
        }
        guard total > 0 else { return (m.step == .smoke ? 1 : 0, m.step == .smoke ? "完成" : "准备下载") }
        let f = min(1, Double(done) / Double(total))
        if f >= 1 || m.step == .smoke { return (1, "完成") }
        var text = "下载中"
        if let start = m.fetchStarted, Date().timeIntervalSince(start) >= 10, f >= 0.02 {
            let rate = Double(done) / Date().timeIntervalSince(start)
            let left = Double(total - done) / max(1, rate)
            text += left < 60 ? " · 还剩不到 1 分钟" : " · 还剩约 \(Int((left / 60).rounded(.up))) 分钟"
        }
        return (f, text)
    }

    /// 下载页走到头的几种结果（样板 first-fetch-failed / first-smoke-failed / first-smoke-deferred / first-no-base / first-hub-down）
    enum FetchOutcome { case working, ok, fetchFailed, smokeFailed, smokeDeferred, noBase, hubDown }

    var fetchOutcome: FetchOutcome {
        guard m.step == .smoke else { return .working }
        guard let st = hub.status else { return m.previewDownloads == nil && hub.hubDown ? .hubDown : .working }
        if !m.failed.isEmpty { return .fetchFailed }
        if st.base == nil && st.engineReady { return .noBase }
        if st.smokeOK == false { return .smokeFailed }
        if st.smokeDeferred { return .smokeDeferred }
        return st.smokeOK == true ? .ok : .working
    }

    private var fetchTitle: (String, String?) {
        switch fetchOutcome {
        case .working: return ("正在下载安装", nil)
        case .ok: return ("下载安装完成", nil)
        case .fetchFailed: return ("有 \(m.failed.count) 项没下下来", "可以重试，也可以先继续。")
        case .smokeFailed: return ("本机试跑没通过", nil)
        case .smokeDeferred: return ("本机试跑推迟了", "内存空出来后会自动试跑。")
        case .noBase: return ("引擎里没有底座模型", nil)
        case .hubDown: return ("螺壳没有启动", nil)
        }
    }

    private var fetchPage: some View {
        let prog = fetchProgress
        let outcome = fetchOutcome
        return VStack(alignment: .leading, spacing: 0) {
            LKTitle(title: fetchTitle.0, sub: fetchTitle.1)
            if outcome == .hubDown {
                LKNote(icon: "exclamationmark.triangle", text: (hub.hubExit.map { "螺壳进程退出了（代码 \($0)）。" } ?? ((hub.hubError ?? "螺壳进程起不来") + "。"))
                       + "点「重试」，还不行就在「设置 › 关于」里导出诊断信息发给我们。", kind: .danger)
            } else {
            if outcome == .working {
            VStack(alignment: .leading, spacing: 6) {
                HStack {
                    Text(prog.text).font(.system(size: 13))
                    Spacer()
                    Text("\(Int(prog.fraction * 100))%").font(.system(size: 13)).foregroundStyle(Color.lkMuted).monospacedDigit()
                }
                ProgressView(value: prog.fraction).progressViewStyle(.linear).tint(.lkBrand)
            }
            .padding(.bottom, 20)
            }
            VStack(alignment: .leading, spacing: 10) {
                ForEach(fetchRows) { r in
                    HStack(alignment: .top, spacing: 12) {
                        LKStateIcon(state: r.state)
                        VStack(alignment: .leading, spacing: 2) {
                            Text(r.label).font(.system(size: 14)).foregroundStyle(r.state == .wait ? Color.lkMuted : Color.lkForeground)
                            if let d = r.detail {
                                Text(d).font(.system(size: 12)).foregroundStyle(Color.lkMuted).fixedSize(horizontal: false, vertical: true)
                            }
                        }
                        Spacer()
                        Text(r.right).font(.system(size: 13)).foregroundStyle(Color.lkMuted).monospacedDigit()
                    }
                }
                Divider().overlay(Color.lkBorder)
                HStack(spacing: 12) {
                    LKStateIcon(state: smokeState)
                    Text("本机试跑").font(.system(size: 14)).foregroundStyle(m.step == .smoke ? Color.lkForeground : Color.lkMuted)
                    Spacer()
                    Text(smokeText).font(.system(size: 13)).foregroundStyle(Color.lkMuted)
                        .multilineTextAlignment(.trailing).fixedSize(horizontal: false, vertical: true)
                }
                switch outcome {
                case .smokeFailed:
                    LKNote(icon: "exclamationmark.triangle", text: "先点「重新检查」。还不行可以先继续，再在「设置 › 关于」里导出诊断信息发给我们。", kind: .danger)
                        .padding(.top, 6)
                case .smokeDeferred:
                    LKNote(icon: "exclamationmark.triangle", text: "例如你自己的 oMLX 正加载着大模型。可以先继续，不影响使用。", kind: .warning).padding(.top, 6)
                case .noBase:
                    LKNote(icon: "exclamationmark.triangle", text: "退出螺壳、重新打开再走一遍，还不行就在「设置 › 关于」里导出诊断信息发给我们。", kind: .danger).padding(.top, 6)
                default: EmptyView()
                }
            }
            }
        }
    }

    private var smokeState: LKStateIcon.State {
        guard m.step == .smoke, let st = hub.status else { return .wait }
        if st.smokeOK == true { return .done }
        if st.smokeDeferred { return .later }
        if st.smokeOK == false || (st.base == nil && st.engineReady) { return .fail }
        return .busy
    }

    private var smokeText: String {
        guard m.step == .smoke else { return "下完后自动做" }
        guard let st = hub.status else { return "等螺壳启动…" }
        if st.base == nil && st.engineReady { return "缺底座" }
        if st.smokeDeferred { return "稍后自动做" }
        if st.smokeOK == true { return "通过" }
        if st.smokeOK == false { return "没通过" }
        return st.phase == "starting" ? "加载模型…" : "等螺壳报告…"
    }

    private var donePage: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack(spacing: 16) {
                if let k = LKAsset.image(m.failed.isEmpty ? "k-wow.webp" : "k-peek.webp") { Image(nsImage: k).resizable().scaledToFit().frame(width: 64, height: 64) }
                VStack(alignment: .leading, spacing: 4) {
                    Text(m.failed.isEmpty ? "装好了" : "装好了一部分")
                        .font(.system(size: 20, weight: .semibold))
                    if !doneSub.isEmpty {
                        Text(doneSub).font(.system(size: 14)).foregroundStyle(Color.lkMuted).fixedSize(horizontal: false, vertical: true)
                    }
                }
            }
            .padding(.bottom, 20)

            do {
                LKGroup {
                    if Accessories.installed().contains(where: { $0.accessory == "luoke-mail" }) {
                        HStack(spacing: 12) {
                            if let img = LKAsset.image("app-mail.png") { Image(nsImage: img).resizable().frame(width: 36, height: 36).clipShape(.rect(cornerRadius: 8)) }
                            VStack(alignment: .leading, spacing: 2) {
                                Text("螺壳邮件助手").font(.system(size: 14, weight: .medium))
                                Text("已经在浏览器里打开了，第一次要连接邮箱。").font(.system(size: 13)).foregroundStyle(Color.lkMuted)
                            }
                            Spacer()
                            Button("打开") { if let a = Accessories.installed().first(where: { $0.accessory == "luoke-mail" }) { AppPlacement.open(a.app) } }
                                .buttonStyle(LKSecondaryButton())
                        }
                        .padding(.horizontal, 16).padding(.vertical, 12)
                        Divider().overlay(Color.lkBorder)
                    }
                    HStack(spacing: 12) {
                        if let img = LKAsset.image("app-core.png") { Image(nsImage: img).resizable().frame(width: 36, height: 36).clipShape(.rect(cornerRadius: 8)) }
                        VStack(alignment: .leading, spacing: 2) {
                            Text("螺壳").font(.system(size: 14, weight: .medium))
                            Text("在菜单栏的海螺图标里。").font(.system(size: 13)).foregroundStyle(Color.lkMuted)
                        }
                        Spacer()
                    }
                    .padding(.horizontal, 16).padding(.vertical, 12)
                }
                .padding(.bottom, 12)
            }

            if m.inputMethodReady { imeCard.padding(.bottom, 12) }

            if !m.notifyRows.isEmpty {
                LKGroup {
                    ForEach(Array(m.notifyRows.enumerated()), id: \.offset) { i, r in
                        if i > 0 { Divider().overlay(Color.lkBorder) }
                        HStack(spacing: 10) {
                            Image(systemName: "bell").font(.system(size: 14)).foregroundStyle(Color.lkMuted)
                            Text(r.name).font(.system(size: 14))
                            Spacer()
                            switch r.state {
                            case "ok": LKBadge(text: "通知已打开", kind: .success)
                            case "ask": Button("允许它发通知") { m.askNotifications(r) }.buttonStyle(LKSecondaryButton())
                            default:
                                LKBadge(text: "通知被关掉了", kind: .warning)
                                Button("打开通知设置") { m.openNotificationSettings(r) }.buttonStyle(LKSecondaryButton())
                            }
                        }
                        .padding(.horizontal, 16).padding(.vertical, 10)
                    }
                }
                .padding(.bottom, 12)
            }

            if !m.failed.isEmpty {
                Text(m.doneText).font(.system(size: 13)).foregroundStyle(Color.lkDanger).fixedSize(horizontal: false, vertical: true)
            }
        }
    }

    /// 完成页标题下的一句：只在有事要说时出现（没装上什么、应用有新版本）
    private var doneSub: String {
        return m.failed.isEmpty ? "" : m.doneText
    }

    private var imeCard: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack(spacing: 8) {
                Image(systemName: "keyboard").font(.system(size: 14))
                Text(m.inputMethodState == "ready" ? "试试螺壳输入法" : "输入法还差一步：添加到系统").font(.system(size: 14, weight: .medium))
                Spacer()
                if m.inputMethodState == "ready" { LKBadge(text: "已添加", kind: .success) }
                else if m.inputMethodState == "added_not_selectable" { LKBadge(text: "系统收录中", kind: .secondary) }
                else { LKBadge(text: "待添加", kind: .warning) }
            }
            if m.inputMethodState == "ready" {
                Text("切换到螺壳，在下面试着打 nihao。").font(.system(size: 13)).foregroundStyle(Color.lkMuted)
                HStack(spacing: 8) {
                    TextField("在这里打字", text: $m.tryText).textFieldStyle(.roundedBorder).focused($tryFocused)
                        .onChange(of: m.refocusTry) {
                            // 切过去以后松开再拿回焦点：文本框重新接输入法，才用得上刚切过去的螺壳
                            tryFocused = false
                            DispatchQueue.main.asyncAfter(deadline: .now() + 0.2) { tryFocused = true }
                        }
                    Button("切换到螺壳") { m.selectInputMethod() }.buttonStyle(LKSecondaryButton())
                }
                if m.selectFailed && m.reregistered {
                    // 重新登记过还是切不过去：多半是这次登录里系统的输入法服务（imklaunchagent 这类，不归我们结束），再点也没用；
                    // 原因没坐实，文案只说"没认上"（luoke-ime 10-22）。注销时这个窗口也会关，所以不说"页面会自己更新"
                    LKNote(icon: "rectangle.portrait.and.arrow.right", text: "还是切不过去：系统这次没认上螺壳输入法。请注销并重新登录一次（苹果菜单 → 退出登录），回来以后在菜单栏的输入法里选「螺壳」就能用了。", kind: .warning)
                } else if m.selectFailed || FirstRunModel.otherIMECopies > 0 {
                    Text(m.selectFailed ? "系统没切过去。多半是这台 Mac 上还登记着别的螺壳输入法副本（例如废纸篓里的旧版），系统绑到了那一份上。点下面的按钮重新登记，再切一次。"
                                        : "这台 Mac 上还登记着别的螺壳输入法副本（例如废纸篓里的旧版）。切不过去的话，点下面的按钮重新登记。")
                        .font(.system(size: 13)).foregroundStyle(Color.lkWarning).fixedSize(horizontal: false, vertical: true)
                    Button(m.reregistering ? "正在重新登记…" : "重新登记输入法") { m.reregisterInputMethod() }
                        .buttonStyle(LKSecondaryButton()).disabled(m.reregistering)
                }
            } else if m.inputMethodState == "added_not_selectable" {
                Text("已经添加了，系统还在收录螺壳输入法，稍等几秒……").font(.system(size: 13)).foregroundStyle(Color.lkMuted)
            } else {
                HStack(spacing: 4) {
                    ForEach(Array(["键盘设置", "输入源 · 编辑…", "左下角 +", "简体中文", "螺壳", "添加"].enumerated()), id: \.offset) { i, x in
                        Text(x).font(.system(size: 12)).lineLimit(1).fixedSize().padding(.horizontal, 5).padding(.vertical, 2)
                            .background(Color.lkMutedBg).clipShape(.rect(cornerRadius: 4))
                        if i < 5 { Image(systemName: "arrow.right").font(.system(size: 10)).foregroundStyle(Color.lkMuted) }
                    }
                }
                HStack(spacing: 12) {
                    Button { m.openKeyboardSettings() } label: { Label("打开键盘设置", systemImage: "arrow.up.right.square") }
                        .buttonStyle(LKSecondaryButton())
                    Text("添加好了这里会自动更新").font(.system(size: 12)).foregroundStyle(Color.lkMuted)
                }
                Text("系统会提示输入法能看到输入内容，螺壳输入法不联网。不想记进记忆，在输入法设置的「通用」里关掉「记录输入内容到记忆」。")
                    .font(.system(size: 12)).foregroundStyle(Color.lkMuted).fixedSize(horizontal: false, vertical: true)
            }
        }
        .padding(16).background(Color.lkCard).clipShape(.rect(cornerRadius: 12))
        .overlay(RoundedRectangle(cornerRadius: 12).stroke(Color.lkBorder))
    }

    // ---- 底栏：轻按钮在左、主按钮在右；下载开始后没有"上一步" ------------------------------------------------

    private var footer: some View {
        HStack(spacing: 8) {
            switch m.step {
            case .preflight:
                Spacer()
                Button("继续") { m.applyTierChoice(); m.step = .accessories }
                    .buttonStyle(LKPrimaryButton()).disabled(!m.preflightOK)
            case .accessories:
                Button("上一步") { m.step = .preflight }.buttonStyle(LKSecondaryButton(ghost: true))
                Spacer()
                Button("继续") { m.makePlan(); m.step = .consent }.buttonStyle(LKPrimaryButton())
            case .consent:
                Button("上一步") { m.step = .accessories }.buttonStyle(LKSecondaryButton(ghost: true))
                Spacer()
                Button("开始下载") { m.fetch() }.buttonStyle(LKPrimaryButton())
            case .fetching:
                Spacer()
                Button("继续") {}.buttonStyle(LKPrimaryButton()).disabled(true)      // 样板：下完、试跑过才能点
            case .smoke:
                Spacer()
                let next = { m.registerLoginItem(); m.launchResidentAccessories(); m.step = .done }
                switch fetchOutcome {
                case .fetchFailed:
                    Button("先继续") { next() }.buttonStyle(LKSecondaryButton())
                    Button { m.retryFailed() } label: { Label("重试", systemImage: "arrow.clockwise") }.buttonStyle(LKPrimaryButton())
                case .smokeFailed:
                    Button { m.rerunSmoke() } label: { Label("重新检查", systemImage: "arrow.clockwise") }.buttonStyle(LKSecondaryButton())
                    Button("继续") { next() }.buttonStyle(LKPrimaryButton())
                case .ok, .smokeDeferred, .noBase:
                    Button("继续") { next() }.buttonStyle(LKPrimaryButton())
                case .hubDown:
                    Button { hub.startHub() } label: { Label("重试", systemImage: "arrow.clockwise") }
                        .buttonStyle(LKPrimaryButton())
                case .working:
                    Button("继续") {}.buttonStyle(LKPrimaryButton()).disabled(true)
                }
            case .done:
                Spacer()
                if !m.failed.isEmpty { Button { m.retryFailed() } label: { Label("重试", systemImage: "arrow.clockwise") }.buttonStyle(LKSecondaryButton()) }
                Button("完成") { FirstRun.finish() }.buttonStyle(LKPrimaryButton())
            }
        }
        .padding(.horizontal, 32).padding(.vertical, 14)
        .frame(height: 64)
        .overlay(alignment: .top) { Rectangle().fill(Color.lkBorder).frame(height: 1) }
    }
}
