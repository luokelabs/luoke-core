// 卸载配件（V1 §6、§10b，2026-10-16 定进 beta1）：菜单「卸载…」→ 选配件 → 看清单（.app、它声明的数据目录、能从记忆里忘掉的条数）→ 勾选 → 删。
// 删文件由主控进程做（POST /uninstall，只有主控自己的身份能调）；本体负责：结束配件进程（按 bundle id，不按名字）、输入法先移除输入源、
// 「卸载全部」的收尾（环境目录、登录项、把自己移到废纸篓）。记忆文件 ~/.luoke/mem.db 永远不删（顶层 ADR 0014）。
import AppKit
import Carbon
import ServiceManagement
import SwiftUI

enum Uninstall {
    static var window: NSWindow?

    /// back = 从哪个窗口点进来的（「应用」或「设置」）：已经选好了，直接到确认；取消、完成都回那个窗口
    static func show(preselect: String? = nil, back: (() -> Void)? = nil) {
        if let w = window, w.isVisible {
            // 开着时又从别处点了「卸载」另一个：没开始删就换成新的；正在删就只叫到前面（10-24 核出：之前一律忽略新的预选）
            guard preselect != nil, let m = (w.contentViewController as? NSHostingController<UninstallView>)?.rootView.m, !m.busy else { bringFront(w); return }
            w.close()
        }
        let m = UninstallModel()
        // 从「应用」或「设置」点进来：已经选好了，直接到确认（同一时间只一个窗口）
        if let p = preselect, p == UninstallModel.all || m.items.contains(where: { $0.accessory == p }) { m.chosen = p; m.back = back; m.prepare() }
        let host = NSHostingController(rootView: UninstallView(m: m))
        let w = NSWindow(contentViewController: host)
        w.title = "卸载"
        w.styleMask = [.titled, .closable]
        w.setContentSize(NSSize(width: 560, height: 460))      // 和「应用」窗口一样大：从那里点卸载，换过来不跳
        w.center()
        w.isReleasedWhenClosed = false
        window = w
        AppWindows.track(w)
        bringFront(w)
    }

    /// 菜单栏菜单一关，系统把前台还给原来的应用，主控的窗口就压在后面（16 GB 测试机 2026-10-16 实测）：先无条件排到最前，再抢一次前台，稍后再抢一次。
    static func bringFront(_ w: NSWindow) {
        // macOS 14 起 activateIgnoringOtherApps 不起作用（10-21 负责人用「检查更新…」升级后，升级页开在别的窗口后面，只看到菜单栏图标）：
        // 窗口先 orderFrontRegardless 摆到最前，再用新的 NSApp.activate() 要前台；稍后再来一次
        w.orderFrontRegardless()
        w.makeKeyAndOrderFront(nil)
        NSApp.activate()
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.4) {
            w.orderFrontRegardless()
            NSApp.activate()
            w.makeKeyAndOrderFront(nil)
        }
    }

    /// 结束配件的进程：先礼貌退出，等 5 秒，再强制；只按 bundle id 找。退出监听会想重启常驻配件，先压住。
    /// 结束这个 bundle id 的进程，最多等 8 秒（会阻塞：别在主线程上调）。markNoRestart=false 时调用方已经在主线程上记好了"别重启"。
    static func terminate(bundleID: String, markNoRestart: Bool = true) {
        if markNoRestart { Accessories.noRestart.insert(bundleID) }
        let apps = NSRunningApplication.runningApplications(withBundleIdentifier: bundleID)
        apps.forEach { $0.terminate() }
        var deadline = Date().addingTimeInterval(5)
        while Date() < deadline, apps.contains(where: { !$0.isTerminated }) { Thread.sleep(forTimeInterval: 0.2) }
        apps.filter { !$0.isTerminated }.forEach { $0.forceTerminate() }
        deadline = Date().addingTimeInterval(3)
        while Date() < deadline, apps.contains(where: { !$0.isTerminated }) { Thread.sleep(forTimeInterval: 0.2) }
    }

    // ---- 输入法：输入源还启用着就不能删 .app（系统会留下一个打不开的输入源）。先自己试着停用，不行再请用户去系统设置里移除 ----
    private static func inputSources(bundleID: String) -> [TISInputSource] {
        guard let list = TISCreateInputSourceList(nil, true)?.takeRetainedValue() as? [TISInputSource] else { return [] }
        return list.filter { src in
            guard let p = TISGetInputSourceProperty(src, kTISPropertyBundleID) else { return false }
            return (Unmanaged<CFString>.fromOpaque(p).takeUnretainedValue() as String) == bundleID
        }
    }

    /// macOS 26 起第三方输入法的启用记在 com.apple.inputsources 的 AppleEnabledThirdPartyInputSources，以它为准：
    /// 16 GB 测试机 2026-10-16 实测，系统设置里移除之后本进程的 TIS 仍报 IsEnabled（缓存过期），卡死在提示上。更早的系统才看 TIS。
    /// 读的时候起一个新进程（`defaults export`），不读本进程的偏好缓存：TIS 的调用会把本进程缓存里的列表改脏，同步也不丢
    /// （10-21 在 macOS 26.4.1 上复现：停用写不进系统、缓存却改成"没有螺壳"，于是没拦住，删了 .app 留下失效的输入源）。
    static func inputSourceEnabled(bundleID: String) -> Bool {
        if ProcessInfo.processInfo.operatingSystemVersion.majorVersion >= 26 {
            let p = Process()
            p.executableURL = URL(fileURLWithPath: "/usr/bin/defaults")
            p.arguments = ["export", "com.apple.inputsources", "-"]
            let out = Pipe()
            p.standardOutput = out
            p.standardError = FileHandle.nullDevice
            guard (try? p.run()) != nil else { return false }
            let data = out.fileHandleForReading.readDataToEndOfFile()
            p.waitUntilExit()
            let plist = (try? PropertyListSerialization.propertyList(from: data, format: nil)) as? [String: Any]
            let list = plist?["AppleEnabledThirdPartyInputSources"] as? [[String: Any]]
            return (list ?? []).contains { ($0["Bundle ID"] as? String) == bundleID }
        }
        return inputSources(bundleID: bundleID).contains { src in
            guard let p = TISGetInputSourceProperty(src, kTISPropertyInputSourceIsEnabled) else { return false }
            return CFBooleanGetValue(Unmanaged<CFBoolean>.fromOpaque(p).takeUnretainedValue())
        }
    }

    /// 第三方输入法在 TIS 里是两个源：输入模式（可选中的那个，例如 ai.luoke.inputmethod.Luoke.Hans）和输入法本身；先停模式再停父项
    /// （luoke-ime 2026-10-16 说明），然后调用方核对两个的 IsEnabled 都已 false 才结束进程，不然系统一选中又把它拉起来。
    static func disableInputSource(bundleID: String) {
        let srcs = inputSources(bundleID: bundleID)
        func isMode(_ src: TISInputSource) -> Bool {
            guard let p = TISGetInputSourceProperty(src, kTISPropertyInputSourceType) else { return false }
            return (Unmanaged<CFString>.fromOpaque(p).takeUnretainedValue() as String) == (kTISTypeKeyboardInputMode as String)
        }
        for src in srcs.filter({ isMode($0) }) + srcs.filter({ !isMode($0) }) { TISDisableInputSource(src) }
    }

    static func openInputSettings() {
        if let u = URL(string: "x-apple.systempreferences:com.apple.Keyboard-Settings.extension?InputSources") { NSWorkspace.shared.open(u) }
    }
}

final class UninstallModel: ObservableObject {
    enum Step { case pick, confirm, working, done }
    struct Plan {
        let accessory, name, type, bundleID, app: String
        let data: [String]
        let forgetRecords: Int, forgetNotes: Int     // -1：没得忘（没装螺壳记忆、配件不读写记忆）
        let memoryNeededBy: [String]
        var canForget: Bool { forgetRecords + forgetNotes > 0 }
    }
    static let all = "*"

    @Published var step: Step = .pick
    @Published var items: [Accessories.Manifest] = []
    @Published var chosen = ""                 // 配件标识，或 "*" = 全部
    @Published var plans: [Plan] = []
    @Published var deleteData = false         // 默认保留用户数据（DESIGN §8）
    @Published var forget = false             // 默认不动记忆
    @Published var busy = false
    var back: (() -> Void)?
    @Published var error: String?
    @Published var inputSourceBlocked: String?   // 还启用着的输入法名字
    @Published var removed: [String] = []
    @Published var errors: [String] = []
    @Published var doneText = ""
    @Published var kept: [String] = []          // 完成页上"还留着的"（issue #14）：没勾删除的数据、记忆里没忘掉的、记忆文件
    private let queue = DispatchQueue(label: "ai.luoke.core.uninstall")

    init() {
        // 螺壳记忆排最后：别的配件要忘记忆得靠它
        // 记忆在主控里时，~/Applications 里残留的旧螺壳记忆.app 不列（主控不让单独卸，列出来也删不掉；审查 2026-10-17）
        items = Accessories.installed().filter { !(Memory.bundled && $0.bundleID == Accessories.memoryBundleID) }.sorted { a, b in
            (a.bundleID == Accessories.memoryBundleID ? 1 : 0, a.name) < (b.bundleID == Accessories.memoryBundleID ? 1 : 0, b.name)
        }
    }

    var isAll: Bool { chosen == UninstallModel.all }
    var targets: [Accessories.Manifest] { isAll ? items : items.filter { $0.accessory == chosen } }

    /// 关掉卸载窗口；从哪个窗口来的回到哪个窗口
    func leave() {
        Uninstall.window?.close()
        if isAll && step == .done && errors.isEmpty { return }     // 全部卸载完了：螺壳马上退出，不再开设置
        back?()
    }

    func prepare() {
        busy = true; error = nil
        let list = targets
        queue.async {
            var plans: [Plan] = []
            do {
                for m in list {
                    let (_, o) = try HubModel.shared.client.request("POST", "/uninstall", body: ["accessory": m.accessory, "dry_run": true], timeout: 150)
                    let f = o["forget"] as? [String: Any]
                    plans.append(Plan(accessory: m.accessory, name: o["name"] as? String ?? m.name, type: o["type"] as? String ?? m.type,
                                      bundleID: m.bundleID, app: o["app"] as? String ?? m.app.path, data: o["data"] as? [String] ?? [],
                                      forgetRecords: f?["records"] as? Int ?? -1, forgetNotes: f?["notes"] as? Int ?? -1,
                                      memoryNeededBy: o["memory_needed_by"] as? [String] ?? []))
                }
                DispatchQueue.main.async { self.plans = plans; self.busy = false; self.step = .confirm }
            } catch {
                DispatchQueue.main.async { self.error = (error as? HubError)?.message ?? error.localizedDescription; self.busy = false }
            }
        }
    }

    func run() {
        // 输入法：输入源先停用；停不掉就请用户去移除，再回来点「卸载」
        // macOS 26 起第三方进程停用不了第三方输入法（只有系统设置能改那份列表），调 TISDisableInputSource 只会把本进程缓存改脏：不调，查到就拦
        let canDisable = ProcessInfo.processInfo.operatingSystemVersion.majorVersion < 26
        for p in plans where p.type == "input-method" {
            if canDisable && Uninstall.inputSourceEnabled(bundleID: p.bundleID) { Uninstall.disableInputSource(bundleID: p.bundleID) }
            if Uninstall.inputSourceEnabled(bundleID: p.bundleID) { inputSourceBlocked = p.name; return }
        }
        inputSourceBlocked = nil
        step = .working
        let plans = self.plans, data = deleteData, forget = self.forget, all = isAll
        for p in plans { Accessories.noRestart.insert(p.bundleID) }      // 主线程上记好"别重启"，后台只管等它们退出
        queue.async {
            var removed: [String] = [], errors: [String] = []
            for p in plans {
                Uninstall.terminate(bundleID: p.bundleID, markNoRestart: false)
                do {
                    let (_, o) = try HubModel.shared.client.request("POST", "/uninstall",
                                                                     body: ["accessory": p.accessory, "data": data, "forget": forget && p.canForget], timeout: 300)
                    removed += o["removed"] as? [String] ?? []
                    errors += (o["errors"] as? [String] ?? []).map { p.name + "：" + $0 }
                } catch {
                    errors.append(p.name + "：" + ((error as? HubError)?.message ?? error.localizedDescription))
                }
            }
            for p in plans where p.type == "input-method" {
                // 删完再核一次：同意列表里还有它，就照实说要去系统设置里移除（不只说"卸载完成"）
                if Uninstall.inputSourceEnabled(bundleID: p.bundleID) {
                    errors.append("\(p.name)：系统的输入源列表里还有它。请到「系统设置 → 键盘 → 文字输入 → 输入源 → 编辑…」里移除「螺壳」。")
                }
                // 输入法自己的临时目录和单实例锁（luoke-ime 的 uninstall.sh 也删这两样）
                let tmp = URL(fileURLWithPath: NSTemporaryDirectory())
                for name in ["luoke-ime", p.bundleID + ".lock"] {
                    let u = tmp.appendingPathComponent(name)
                    if FileManager.default.fileExists(atPath: u.path), (try? FileManager.default.removeItem(at: u)) != nil { removed.append(u.path) }
                }
            }
            var text = "卸载完成。"
            if all && errors.isEmpty {
                text = self.finishAll(removed: &removed, errors: &errors)
            }
            let kept = UninstallModel.keptItems(plans: plans, deletedData: data, forgot: forget, all: all)
            DispatchQueue.main.async {
                self.removed = removed; self.errors = errors; self.doneText = text; self.kept = kept; self.step = .done
                if all && errors.isEmpty { DispatchQueue.main.asyncAfter(deadline: .now() + 3) { NSApplication.shared.terminate(nil) } }
            }
        }
    }

    /// 卸载以后还留着什么、在哪（issue #14：之前完成页只列删了的）。记忆文件归用户、永远不随卸载删除（顶层 ADR 0014）。
    static func keptItems(plans: [Plan], deletedData: Bool, forgot: Bool, all: Bool) -> [String] {
        let home = FileManager.default.homeDirectoryForCurrentUser.path
        func short(_ p: String) -> String { p.hasPrefix(home + "/") ? "~" + p.dropFirst(home.count) : p }
        var out: [String] = []
        for p in plans {
            if !deletedData { out += p.data.map { "\(short($0))：\(p.name) 的数据，重新安装后接着用" } }
            if p.canForget && !forgot { out.append("记忆里 \(p.name) 写的 \(p.forgetRecords) 条记录、\(p.forgetNotes) 条理解") }
        }
        if plans.contains(where: { $0.type == "input-method" }) {
            // luoke-ime#15：同一个登录会话里卸了再装，系统的输入法服务常常还绑着旧副本
            out.append("以后要重装螺壳输入法：先清空废纸篓、注销并重新登录一次，更稳")
        }
        if all || plans.contains(where: { $0.forgetRecords >= 0 }) {
            let lh = ProcessInfo.processInfo.environment["LUOKE_HOME"] ?? (home + "/.luoke")
            out.append("\(short(lh))/mem.db：你的记忆，卸载从不删它" + (all ? "" : "；菜单「记忆」里能看、能删"))
        }
        return out
    }

    /// 「卸载全部」的收尾：停主控进程、删环境目录（模型、引擎、状态、日志）、注销登录项、清设置、把主控 .app 移到废纸篓。记忆文件不动。
    private func finishAll(removed: inout [String], errors: inout [String]) -> String {
        DispatchQueue.main.sync { HubModel.shared.stopHub() }
        Memory.stopAll(hubApp: Bundle.main.bundleURL)      // 记忆界面服务在主控 .app 里跑：删之前请它退出
        let fm = FileManager.default
        if fm.fileExists(atPath: AppInfo.envDir.path) {
            do { try fm.removeItem(at: AppInfo.envDir); removed.append(AppInfo.envDir.path) } catch { errors.append("环境目录：" + error.localizedDescription) }
        }
        if SMAppService.mainApp.status != .notRegistered {
            do { try SMAppService.mainApp.unregister() } catch { errors.append("登录项：" + error.localizedDescription) }
        }
        if let bid = Bundle.main.bundleIdentifier { UserDefaults.standard.removePersistentDomain(forName: bid) }
        if CommandLineTool.installed {                       // 命令行工具的链接（只删我们放的）
            CommandLineTool.uninstall()
            removed.append(CommandLineTool.link.path)
        }
        let me = Bundle.main.bundleURL
        let sem = DispatchSemaphore(value: 0)
        var trashError: String?
        AppPlacement.trash(me) { e in trashError = e?.localizedDescription; sem.signal() }
        _ = sem.wait(timeout: .now() + 20)
        if let e = trashError { errors.append("螺壳：" + e); return "配件已卸载，但螺壳没能移到废纸篓，请手动删除。" }
        removed.append(me.path)
        return "全部卸载完成：螺壳已移到废纸篓，3 秒后退出。记忆文件 ~/.luoke/mem.db 保留着，不需要可以自己删。"
    }
}

struct UninstallView: View {
    @ObservedObject var m: UninstallModel

    var body: some View {
        LKPage {
            switch m.step {
            case .pick: pick
            case .confirm: confirm
            case .working:
                VStack(spacing: 12) {
                    ProgressView().controlSize(.small)
                    Text("正在卸载").font(.system(size: 14)).foregroundStyle(Color.lkMuted)
                }
                .frame(maxWidth: .infinity).padding(.top, 140)
            case .done: done
            }
        } footer: {
            footer
        }
        .frame(width: 560, height: 460)
    }

    // MARK: 选

    private var pick: some View {
        VStack(alignment: .leading, spacing: 0) {
            LKTitle(title: "要卸载哪个")
            LKGroup {
                ForEach(Array(m.items.enumerated()), id: \.offset) { i, it in
                    if i > 0 { Divider().overlay(Color.lkBorder) }
                    choiceRow(id: it.accessory, title: it.name, detail: nil) { LKAppIcon(accessory: it.accessory) }
                }
                if !m.items.isEmpty { Divider().overlay(Color.lkBorder) }
                choiceRow(id: UninstallModel.all, title: "全部", detail: "所有应用、模型，以及螺壳本身") { LKAppIcon(accessory: "luoke-core") }
            }
            if let e = m.error { LKNote(icon: "exclamationmark.triangle", text: e, kind: .danger).padding(.top, 12) }
        }
    }

    private func choiceRow<Icon: View>(id: String, title: String, detail: String?, @ViewBuilder icon: () -> Icon) -> some View {
        let on = m.chosen == id
        return Button { m.chosen = id } label: {
            HStack(spacing: 12) {
                icon()
                VStack(alignment: .leading, spacing: 2) {
                    Text(title).font(.system(size: 14, weight: .medium))
                    if let d = detail { Text(d).font(.system(size: 13)).foregroundStyle(Color.lkMuted) }
                }
                Spacer()
                Image(systemName: on ? "largecircle.fill.circle" : "circle").font(.system(size: 16))
                    .foregroundStyle(on ? Color.lkPrimary : Color.lkInput)
            }
            .padding(.horizontal, 16).padding(.vertical, 12).contentShape(Rectangle())
        }
        .buttonStyle(.plain)
    }

    // MARK: 确认

    private var confirm: some View {
        VStack(alignment: .leading, spacing: 0) {
            LKTitle(title: m.isAll ? "卸载全部" : "卸载\(m.plans.first?.name ?? "")",
                    sub: m.isAll ? "所有应用、模型和引擎都会删掉，记忆文件保留。" : nil)
            VStack(spacing: 10) {
                ForEach(m.plans, id: \.accessory) { p in
                    VStack(alignment: .leading, spacing: 6) {
                        HStack(spacing: 10) {
                            LKAppIcon(accessory: p.accessory, size: 28)
                            Text(p.name).font(.system(size: 14, weight: .medium))
                        }
                        Text(p.app).font(.system(size: 12, design: .monospaced)).foregroundStyle(Color.lkMuted).textSelection(.enabled)
                        ForEach(p.data, id: \.self) { Text($0).font(.system(size: 12, design: .monospaced)).foregroundStyle(Color.lkMuted) }
                        if !p.memoryNeededBy.isEmpty {
                            LKNote(icon: "exclamationmark.triangle", text: p.memoryNeededBy.joined(separator: "、") + "还在用记忆，记忆文件不动。", kind: .warning)
                        }
                    }
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(14).background(Color.lkCard).clipShape(.rect(cornerRadius: 12))
                    .overlay(RoundedRectangle(cornerRadius: 12).stroke(Color.lkBorder))
                }
            }
            let hasData = m.plans.contains { !$0.data.isEmpty }, canForget = m.plans.contains { $0.canForget }
            if hasData || canForget {
                LKGroup {
                    if hasData {
                        LKSettingRow(icon: "folder", title: "同时删除数据") {
                            Toggle("", isOn: $m.deleteData).toggleStyle(.switch).controlSize(.small).tint(.lkPrimary).labelsHidden()
                        }
                    }
                    if hasData && canForget { Divider().overlay(Color.lkBorder) }
                    if canForget {
                        // 顶层 ADR 0015 第 7 条（2026-10-16 晚定）：用户确认或否定过的理解都留着；条数照 forget-tool --dry-run 给的
                        LKSettingRow(icon: "brain", title: "同时从记忆里忘掉它的 \(m.plans.reduce(0) { $0 + max($1.forgetRecords, 0) }) 条记录、\(m.plans.reduce(0) { $0 + max($1.forgetNotes, 0) }) 条理解") {
                            Toggle("", isOn: $m.forget).toggleStyle(.switch).controlSize(.small).tint(.lkPrimary).labelsHidden()
                        }
                    }
                }
                .padding(.top, 12)
            }
            if let name = m.inputSourceBlocked {
                // 文案与 luoke-ime 的添加引导一致（luoke-community 2026-10-16 核对）：列表里叫「螺壳」（英文系统「Luoke 螺壳」）
                VStack(alignment: .leading, spacing: 8) {
                    LKNote(icon: "keyboard", text: "「\(name)」的输入源还开着。请先在「系统设置 → 键盘 → 文字输入 → 输入源 → 编辑…」里移除「螺壳」，再点「卸载」。", kind: .warning)
                    Button { Uninstall.openInputSettings() } label: { Label("打开输入源设置", systemImage: "arrow.up.right.square") }
                        .buttonStyle(LKSecondaryButton())
                }
                .padding(.top, 12)
            }
        }
    }

    // MARK: 完成

    private var done: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack(spacing: 16) {
                if let k = LKAsset.image("k-peek.webp") { Image(nsImage: k).resizable().scaledToFit().frame(width: 56, height: 56) }
                LKTitle(title: m.errors.isEmpty ? "卸载好了" : "没有全部卸载", sub: m.doneText).padding(.bottom, -20)
            }
            .padding(.bottom, 20)
            VStack(alignment: .leading, spacing: 8) {
                ForEach(m.removed, id: \.self) { r in
                    HStack(alignment: .top, spacing: 10) {
                        LKStateIcon(state: .done)
                        Text("已删除 " + r).font(.system(size: 12, design: .monospaced)).foregroundStyle(Color.lkMuted).fixedSize(horizontal: false, vertical: true)
                    }
                }
                ForEach(m.errors, id: \.self) { e in
                    HStack(alignment: .top, spacing: 10) {
                        LKStateIcon(state: .fail)
                        Text(e).font(.system(size: 13)).fixedSize(horizontal: false, vertical: true)
                    }
                }
            }
            if !m.kept.isEmpty {
                Text("还留着").font(.system(size: 14, weight: .semibold)).padding(.top, 20).padding(.bottom, 8)
                LKGroup {
                    ForEach(Array(m.kept.enumerated()), id: \.offset) { i, k in
                        if i > 0 { Divider().overlay(Color.lkBorder) }
                        Text(k).font(.system(size: 13)).foregroundStyle(Color.lkMuted).fixedSize(horizontal: false, vertical: true)
                            .frame(maxWidth: .infinity, alignment: .leading).padding(.horizontal, 16).padding(.vertical, 10)
                    }
                }
            }
        }
    }

    // MARK: 底栏

    @ViewBuilder private var footer: some View {
        switch m.step {
        case .pick:
            Spacer()
            Button("取消") { Uninstall.window?.close() }.buttonStyle(LKSecondaryButton(ghost: true))
            Button("下一步") { m.prepare() }.buttonStyle(LKPrimaryButton()).keyboardShortcut(.defaultAction).disabled(m.chosen.isEmpty || m.busy)
        case .confirm:
            if m.back != nil {
                Button("取消") { m.leave() }.buttonStyle(LKSecondaryButton(ghost: true))
            } else {
                Button("返回") { m.step = .pick }.buttonStyle(LKSecondaryButton(ghost: true))
            }
            Spacer()
            Button(m.isAll ? "全部卸载" : "卸载") { m.run() }.buttonStyle(LKPrimaryButton(destructive: true))
        case .working:
            Spacer()
        case .done:
            Spacer()
            Button("完成") { m.leave() }.buttonStyle(LKPrimaryButton()).keyboardShortcut(.defaultAction)
        }
    }

}
