// 螺壳本体：菜单栏状态灯 + 设置页（V1 §1）。没有主窗口；首次运行流程在 FirstRun.swift。
import AppKit
import Network
import ServiceManagement
import SwiftUI
import UserNotifications

@main
struct LuokeCoreApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) var delegate

    var body: some Scene {
        // 菜单栏图标和菜单在 MenuBar.swift（NSStatusItem）。SwiftUI 至少要一个场景：留一个空的 Settings，⌘, 改走 SettingsWindow
        // （10-23：升级时安装器激活主控、走一遍 reopen，SwiftUI 把唯一的场景——设置——当窗口打开了，标题是"“螺壳”设置"，只有一行「主控启动中」）
        Settings { EmptyView() }
            .commands {
                CommandGroup(replacing: .appSettings) {
                    Button("设置…") { SettingsWindow.show() }.keyboardShortcut(",", modifiers: .command)
                }
            }
    }
}

final class AppDelegate: NSObject, NSApplicationDelegate, UNUserNotificationCenterDelegate {
    /// 点了主控自己发的通知（装好了、升级好了、没下下来）：见 DoneAttention.clicked
    func userNotificationCenter(_ center: UNUserNotificationCenter, didReceive response: UNNotificationResponse,
                                withCompletionHandler completionHandler: @escaping () -> Void) {
        let info = response.notification.request.content.userInfo
        DispatchQueue.main.async { DoneAttention.clicked(info) }
        completionHandler()
    }

    /// 主控在前台时也显示横幅（完成页可能被别的窗口挡着）
    func userNotificationCenter(_ center: UNUserNotificationCenter, willPresent notification: UNNotification,
                                withCompletionHandler completionHandler: @escaping (UNNotificationPresentationOptions) -> Void) {
        completionHandler([.banner, .sound])
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        if let i = CommandLine.arguments.firstIndex(of: "--render-ui"), i + 1 < CommandLine.arguments.count {
            UIRender.run(to: CommandLine.arguments[i + 1])      // 截图用：画完就退，不起主控
            exit(0)
        }
        // 开发版专用的测试入口：照「检查更新」的路子用一个本地 DMG 升级自己（ADR 0012 的换包在 16 GB 测试机上实测用）
        if AppInfo.isDev, let i = CommandLine.arguments.firstIndex(of: "--upgrade-from-dmg"), i + 2 < CommandLine.arguments.count {
            let dmg = URL(fileURLWithPath: CommandLine.arguments[i + 1]), tag = CommandLine.arguments[i + 2]
            DispatchQueue.global().async {
                do { try Updater.install(dmg: dmg, to: tag, notarized: false) {} } catch { NSLog("upgrade-from-dmg 失败：%@", error.localizedDescription); exit(1) }
            }
            return
        }
        UNUserNotificationCenter.current().delegate = self
        if Installer.isInstallerLaunch() {   // 以"安装<应用>.app"的身份、或在 DMG 里启动：装到位 / 交给已装的主控，然后退出
            Installer.run()
            return
        }
        if !Installer.isCanonicalCopy {
            // 认得的位置上另一份我们的（两处各有一份，例如登录项还指着 ~/Applications 那份）：交给装好的那份，不弹框
            if let cur = Installer.installed, Installer.isStrayCopy {
                AppPlacement.open(cur) { _ in NSApp.terminate(nil) }
                return
            }
            // 别的副本（旧版、构建目录里的）：只说明一声就退，不起主控进程
            let a = NSAlert()
            a.messageText = "这不是装好的螺壳"
            a.informativeText = "请打开「应用程序」文件夹里的「\(AppInfo.appName)」。"
            a.runModal()
            NSApp.terminate(nil)
            return
        }
        MenuBar.shared.install()
        Installer.retireStrayCopies()          // 两处各有一份（老用户又拖了一份进 /Applications）：另一份挪进 previous.noindex（ADR 0012）
        // 上一次换包的结果（ADR 0012）：没成的话现在跑的是脚本放回的旧版，"刚升级"的标记作废，照实说一声
        if let r = Swap.takeResult(), !r.ok {
            try? FileManager.default.removeItem(at: Installer.upgradeFile)
            let a = NSAlert()
            a.messageText = "升级没完成"
            a.informativeText = (r.why.contains("Operation not permitted")
                ? "macOS 的「App 管理」权限挡住了这次替换。到 系统设置 → 隐私与安全性 → App 管理 里打开「\(AppInfo.appName)」，再检查一次更新。"
                : r.why + "。") + "原来的螺壳照常能用。"
            DispatchQueue.main.async { a.runModal() }
        }
        CommandLineTool.repoint()               // 改名、换位置以后命令行链接跟上
        Updater.cleanupAfterUpgrade()          // 上一次经"检查更新"升级时挂的 DMG：卸下、删掉（首次设置没走完也做，luoke-core#28）
        let preselect = Installer.takePreselect()
        if FirstRun.needed() {
            _ = Installer.takeUpgrade()           // 首次运行没走完就被升级了：照首次运行走，标记作废
            FirstRun.show(preselect: preselect)   // 模型准备好之后（fetch 那一步）再起主控，引擎启动时才看得见模型
        } else {
            HubModel.shared.startHub()
            if let up = Installer.takeUpgrade() {   // 安装器刚把本体换成这一版：应用、引擎、模型也更新到清单的版本，然后再拉起常驻配件
                // 登录项按 bundle 记位置；改名换了路径（螺壳主控.app → 螺壳.app）以后重新登记一次。用户关掉过的（notRegistered）不动
                if SMAppService.mainApp.status != .notRegistered { try? SMAppService.mainApp.register() }
                UpgradeWindow.show(from: up.from, to: up.to, then: preselect)   // 应用的安装器顺带升级了螺壳：升级完到「应用」窗口里接着装它
            } else {
                Accessories.launchResident()   // 主控是唯一的登录项：常驻配件由它在登录时拉起（V1 §1）
                if let p = preselect { FirstRun.showAdd(accessory: p) }
            }
        }
        Accessories.watchResident()
    }

    /// 用户在主控运行时又打开了它（Dock / 启动台），或者某个应用的安装器交过预选来：有预选就接着走加装（安装器通常只激活我们，预选由状态定时器取走；这里兜底）
    /// 没有预选时（#19、#20）：有开着的窗口（首次运行、升级、卸载……）就叫到前面；都没有就弹主控的小窗口，菜单栏图标找不到也有入口
    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        // 一律答 false：要开的窗口这里都自己开了；答 true 会让 SwiftUI 再按默认做法开它的场景（设置）
        if let p = Installer.takePreselect() {
            if FirstRun.needed() { FirstRun.show(preselect: p) } else { FirstRun.showAdd(accessory: p) }
            return false
        }
        if AppWindows.bringBack() { return false }
        if !Installer.isInstallerLaunch() && Installer.isCanonicalCopy && !FirstRun.needed() { HomeWindow.show() }
        return false
    }

    /// luoke://memory：各工具打开记忆界面（主控没在跑时系统会拉起主控，再把链接交过来）
    func application(_ application: NSApplication, open urls: [URL]) {
        if Installer.isInstallerLaunch() || !Installer.isCanonicalCopy { return }
        for u in urls { _ = Memory.handle(u) }
    }

    func applicationWillTerminate(_ notification: Notification) {
        HubModel.shared.stopHub()
    }
}

/// 状态灯和菜单用的模型：每 3 秒问一次主控的 /status。
final class HubModel: ObservableObject {
    static let shared = HubModel()
    @Published var status: HubStatus?
    @Published var hubError: String?
    @Published var hubExit: Int32?
    let process = HubProcess()
    let client = HubClient(socketPath: AppInfo.socketPath)
    @Published var updateFound: Updater.Found?     // 每天一次的自动检查找到的更新（只提示，菜单里显示）
    private var updateTimer: Timer?
    private var timer: Timer?
    private let queue = DispatchQueue(label: "ai.luoke.core.status")
    /// 网络按流量计费（手机热点等）或开了低数据模式：不自动补下，只提示，等用户点「重试」（DESIGN §457，10-27 定进 beta1.1）。
    /// LUOKE_TEST_METERED=1 当作是这种网络，只给实测用（改系统的低数据模式要动系统设置）
    @Published var meteredNetwork = ProcessInfo.processInfo.environment["LUOKE_TEST_METERED"] == "1"
    private let pathMonitor = NWPathMonitor()

    /// 主控进程起不来或退出了（不算刚启动、socket 还没出来的那几秒）
    var hubDown: Bool { hubExit != nil || startFailed }
    private var startFailed = false
    private var startedAt = Date()
    /// 连不上：进程起不来、退出了，或者起来 30 秒还连不上（刚启动 socket 还没出来的那几秒算启动中）
    var unreachable: Bool { hubDown || (status == nil && hubError != nil && Date().timeIntervalSince(startedAt) > 30) }

    /// 第一次启动，或进程退出后"重试"：重新拉起主控进程；定时器只留一份
    func startHub() {
        process.onExit = { [weak self] code in
            DispatchQueue.main.async { self?.hubExit = code }
        }
        hubExit = nil
        startedAt = Date()
        do {
            try process.start()
            hubError = nil
            startFailed = false
        } catch {
            hubError = "螺壳进程起不来：\(error.localizedDescription)"
            startFailed = true
        }
        if pathMonitor.pathUpdateHandler == nil && ProcessInfo.processInfo.environment["LUOKE_TEST_METERED"] != "1" {
            pathMonitor.pathUpdateHandler = { [weak self] path in
                let metered = path.isExpensive || path.isConstrained
                DispatchQueue.main.async { self?.meteredNetwork = metered }
            }
            pathMonitor.start(queue: queue)
        }
        timer?.invalidate()
        updateTimer?.invalidate()
        timer = Timer.scheduledTimer(withTimeInterval: 3, repeats: true) { [weak self] _ in
            self?.refresh()
            if let p = Installer.takePreselect() { FirstRun.showAdd(accessory: p) }   // 某个应用的安装器把预选交过来了（它只激活我们，不走 reopen）
        }
        refresh()
        updateTimer = Timer.scheduledTimer(withTimeInterval: 3600, repeats: true) { [weak self] _ in
            Updater.autoCheckIfDue { f in self?.updateFound = f }
        }
        DispatchQueue.main.asyncAfter(deadline: .now() + 60) { [weak self] in Updater.autoCheckIfDue { f in self?.updateFound = f } }
    }

    /// 菜单「重新启动螺壳」：出错（进程退出、连不上、引擎熔断）后的唯一出路；熔断只在主控重启时清掉
    func restartHub() {
        process.onExit = nil
        status = nil
        hubError = nil
        DispatchQueue.global().async { [weak self] in
            self?.process.stop()
            DispatchQueue.main.async { self?.startHub() }
        }
    }

    func stopHub() {
        timer?.invalidate()
        process.stop()
    }

    private var refreshing = false      // 上一次 /status 还没回来就跳过这一轮（luoke-core#4：3 秒定时器 + 10 秒超时会在串行队列里越排越多）
    func refresh() {
        guard !refreshing else { return }
        refreshing = true
        queue.async { [weak self] in
            guard let self = self else { return }
            let result = Result { try self.client.status() }
            DispatchQueue.main.async {
                self.refreshing = false
                switch result {
                case .success(let st):
                    self.status = st
                    self.hubError = nil
                    self.autoDownload(st)
                case .failure(let e):
                    self.status = nil
                    self.hubError = (e as? HubError)?.message ?? e.localizedDescription
                }
            }
        }
    }

    /// 首次运行走过以后还缺模型（换档时下载失败、模型目录坏了、升级后要核哈希的旧模型）：自动补下，失败就隔一阵再试
    /// （审查 2026-10-17：之前只有首次运行会调 /downloads，换档下载失败没有入口）。首次运行窗口开着时不插手。
    private var lastAutoDownload = Date.distantPast
    private var autoDownloadTries = 0
    func autoDownload(_ st: HubStatus) {
        guard !FirstRun.needed(), FirstRun.window?.isVisible != true, UpgradeWindow.window?.isVisible != true, Manifests.hasRelease,
              !st.neededDownloads.isEmpty, !st.downloading, !meteredNetwork else { return }
        let wait = min(1800.0, 30.0 * pow(2.0, Double(autoDownloadTries)))   // 30 s、1 分、2 分……最多半小时
        guard Date().timeIntervalSince(lastAutoDownload) > wait else { return }
        lastAutoDownload = Date()
        autoDownloadTries += 1
        retryDownloads()
    }

    func retryDownloads() {
        queue.async { [weak self] in
            _ = try? self?.client.request("POST", "/downloads", body: [:])
            self?.refresh()
        }
    }

    func setPowerMode(_ mode: String) { update(["power_mode": mode]) }
    func setBackgroundOnBattery(_ on: Bool) { update(["background_on_battery": on]) }
    @Published var tierError: String?
    func setTier(_ tier: String) {
        // 换到要新下底座的档前先看磁盘（审查 2026-10-17）
        let base = ((Manifests.recipe()?["assignment"] as? [String: Any])?[tier] as? [String: Any])?["base"] as? String
        if let b = base, !Manifests.modelInstalled(b) {
            let need = Manifests.itemSize(b) + (1 << 30)
            let free = (try? URL(fileURLWithPath: NSHomeDirectory()).resourceValues(forKeys: [.volumeAvailableCapacityForImportantUsageKey]).volumeAvailableCapacityForImportantUsage) ?? 0
            if free < need {      // 不弹窗，写在这一行下面（10-23）
                tierError = "磁盘空间不够：换这一档要下载 \(Diagnostics.humanSize(Manifests.itemSize(b)))，现在只剩 \(Diagnostics.humanSize(free))。"
                return
            }
        }
        tierError = nil
        update(["tier": tier == (status?.memoryTier ?? tier) ? NSNull() : tier])    // 选回内存算的档就清掉设置
    }

    private func update(_ patch: [String: Any]) {
        queue.async { [weak self] in
            _ = try? self?.client.settings(patch)
            self?.refresh()
        }
    }

    /// 菜单栏海螺的状态（luoke-brand ui/DESIGN.md §12，按这个顺序判断）：offline 主控没连上、error 引擎有问题、
    /// 名字就是 Resources/ui/menubar 里的图（offline、loaded、busy-50、unloaded、error）
    /// 菜单栏图标（10-23 定）：虚线 = 启动中；实心 = 准备好了；半满 = 在干活（不动）；
    /// 空心 = 后台暂停，交给它的后台活不会跑（在用电池也算，除非设置里打开了）；实心带圆点 = 出错，要人处理
    var lightState: String {
        if unreachable { return "error" }
        guard let st = status else { return "offline" }
        if st.breaker != nil || !st.engineAlive { return "error" }
        if !st.engineReady { return "offline" }
        if st.job != nil || st.interactive { return "busy-50" }
        if st.backgroundState != "running" { return "unloaded" }
        return "loaded"
    }
}

/// 设置窗口（品牌规范的设置行：左图标、标题 + 说明、右控件；分组卡片）
struct SettingsView: View {
    @EnvironmentObject var model: HubModel
    @State private var loginItem = SMAppService.mainApp.status == .enabled
    @State private var loginError: String?

    var body: some View {
        LKPage {
            if let st = model.status { sections(st) }
            else { LKNote(icon: "exclamationmark.triangle", text: model.hubError ?? "螺壳启动中", kind: model.hubError == nil ? .info : .danger) }
        } footer: {
            Spacer()
            Button("完成") { NSApp.keyWindow?.close() }.buttonStyle(LKPrimaryButton())
        }
        .frame(width: 560, height: 680)
    }

    private func header(_ t: String) -> some View {
        Text(t).font(.system(size: 11, weight: .medium)).foregroundStyle(Color.lkMuted).padding(.top, 20).padding(.bottom, 8)
    }

    private func info(_ label: String, _ value: String) -> some View {
        HStack(alignment: .top) {
            Text(label).font(.system(size: 14))
            Spacer(minLength: 16)
            Text(value).font(.system(size: 13)).foregroundStyle(Color.lkMuted).multilineTextAlignment(.trailing).textSelection(.enabled)
        }
        .padding(.horizontal, 16).padding(.vertical, 10)
    }

    private var divider: some View { Divider().overlay(Color.lkBorder) }

    @ViewBuilder private func sections(_ st: HubStatus) -> some View {
        Text("设置").font(.system(size: 20, weight: .semibold))
        header("通用")
        LKGroup {
            LKSettingRow(icon: "gauge.with.dots.needle.50percent", title: "能耗模式") {
                LKSegmented(options: [("full", "全速"), ("balanced", "均衡"), ("quiet", "静音")],
                            selection: Binding(get: { st.powerMode }, set: { model.setPowerMode($0) }))
            }
            divider
            LKSettingRow(icon: "battery.75percent", title: "使用电池时仍运行后台") {
                Toggle("", isOn: Binding(get: { st.backgroundOnBattery }, set: { model.setBackgroundOnBattery($0) }))
                    .toggleStyle(.switch).controlSize(.small).tint(.lkPrimary).labelsHidden()
            }
            divider
            LKSettingRow(icon: "power", title: "登录时启动螺壳",
                         detail: loginError.map { "没设置上：\($0)" }
                            ?? (SMAppService.mainApp.status == .requiresApproval ? "要在「系统设置 › 通用 › 登录项」里允许一次。" : nil)) {
                Toggle("", isOn: Binding(get: { loginItem }, set: { on in
                    loginError = nil
                    do { if on { try SMAppService.mainApp.register() } else { try SMAppService.mainApp.unregister() } }
                    catch { loginError = error.localizedDescription; NSLog("登录项：%@", error.localizedDescription) }
                    loginItem = SMAppService.mainApp.status == .enabled
                }))
                .toggleStyle(.switch).controlSize(.small).tint(.lkPrimary).labelsHidden()
            }
        }
        if !st.downloads.isEmpty || !st.neededDownloads.isEmpty {
            header("下载")
            LKGroup {
                ForEach(Array(st.downloads.keys.sorted().enumerated()), id: \.offset) { i, n in
                    if i > 0 { divider }
                    let d = st.downloads[n] ?? [:]
                    let state = d["state"] as? String ?? "?"
                    let done = (d["done"] as? NSNumber)?.int64Value ?? 0, total = (d["total"] as? NSNumber)?.int64Value ?? 0
                    info(n, state == "done" ? "完成" : (state == "failed" ? "没下下来：" + (d["error"] as? String ?? "") :
                                (state == "downloading" && total > 0 ? "\(Int(Double(done) / Double(total) * 100))%" : "排队中")))     // 排队的也有总数：之前显示成 0%（#24）
                }
                if !st.neededDownloads.isEmpty && !st.downloading {
                    divider
                    LKSettingRow(icon: "arrow.down.circle", title: "还缺：" + st.neededDownloads.joined(separator: "、"),
                                 detail: model.meteredNetwork ? "当前网络按流量计费或开启了低数据模式，不会自动下载。" : nil) {
                        Button { model.retryDownloads() } label: { Label("重试", systemImage: "arrow.clockwise") }.buttonStyle(LKSecondaryButton())
                    }
                }
            }
        }
        header("模型")
        LKGroup {
            let choices = Manifests.tierChoices(memGB: Diagnostics.memGB())
            if choices.count > 1 {
                LKSettingRow(icon: "internaldrive", title: "占多少空间",
                             detail: choices.map { "\($0.label) \(ModelName.display($0.base)) 约 \(String(format: "%.0f", $0.sizeGB)) GB" }.joined(separator: "，")
                                + "。换了会下载另一个，下完自动切换。") {
                    Picker("", selection: Binding(get: { st.pendingTier ?? st.tier }, set: { model.setTier($0) })) {     // 选了还没换过去的，显示选的那个
                        ForEach(choices, id: \.tier) { c in Text(c.label).tag(c.tier) }
                    }
                    .labelsHidden().fixedSize()
                }
                if let e = model.tierError {
                    Text(e).font(.system(size: 12)).foregroundStyle(Color.lkDanger)
                        .fixedSize(horizontal: false, vertical: true).padding(.horizontal, 14).padding(.bottom, 10)
                }
                divider
            }
            info("底座模型", ModelName.display(st.base ?? "—"))      // 硬件档、引擎、本机试跑在诊断信息里
            if let p = st.pendingTier {     // luoke-core#25：换档等着底座，界面上要看得出来
                divider
                info("换档", "\(p) GB 档的底座" + (st.downloading ? "正在下载" : "还没下好") + "，下好后自动切换")
            }
            divider
            StorageView()
            if !st.localAllowed.isEmpty {
                divider
                info("本机放行", st.localAllowed.map { "\($0["accessory"] as? String ?? "?") \($0["version"] as? String ?? "?")（\($0["by"] as? String ?? "?")）" }.joined(separator: "、"))
                Text("这些版本还没进发布评测，是试用期里项目会话放行的；螺壳换成包含它们的新版后自动失效。")
                    .font(.system(size: 12)).foregroundStyle(Color.lkMuted).padding(.horizontal, 16).padding(.bottom, 10)
            }
        }
        header("高级")
        EndpointView()
        // 不常用、会删东西的放最下面（10-23：卸载、导出诊断信息不放在一打开就点得到的地方）
        header("关于")
        LKGroup {
            if let rel = st.release {
                LKSettingRow(icon: "info.circle", title: "螺壳版本") {
                    Text(rel.hasPrefix("local-") ? "\(rel)（本机版，没上传）" : rel).font(.system(size: 13)).foregroundStyle(Color.lkMuted).textSelection(.enabled)
                }
                divider
            }
            LKSettingRow(icon: "square.and.arrow.up", title: "导出诊断信息") {
                Button("导出") { Diagnostics.export(model: model) }.buttonStyle(LKSecondaryButton())
            }
            divider
            LKSettingRow(icon: "trash", title: "卸载螺壳") {
                Button("卸载") { SettingsWindow.window?.close(); Uninstall.show(preselect: UninstallModel.all, back: { SettingsWindow.show() }) }.buttonStyle(LKPrimaryButton(destructive: true))
            }
        }
    }
}

/// 存储页：每个模型占多少、谁在用（V1 §1）。开发版只列模型目录里的东西和大小，删除按钮留到安装器那一块。
struct StorageView: View {
    var entries: [(String, String)] {
        let fm = FileManager.default
        let items = (try? fm.contentsOfDirectory(atPath: AppInfo.modelDir)) ?? []
        return items.sorted().map { name in
            let url = URL(fileURLWithPath: AppInfo.modelDir).appendingPathComponent(name)
            return (name, Diagnostics.humanSize(Diagnostics.directorySize(url)))
        }
    }

    var body: some View {
        if entries.isEmpty {
            Text("还没有模型").font(.system(size: 13)).foregroundStyle(Color.lkMuted).padding(.horizontal, 16).padding(.vertical, 10)
        } else {
            ForEach(Array(entries.enumerated()), id: \.offset) { i, e in
                if i > 0 { Divider().overlay(Color.lkBorder) }
                HStack {
                    Text(ModelName.display(e.0)).font(.system(size: 14))
                    Text("已下载").font(.system(size: 13)).foregroundStyle(Color.lkMuted)
                    Spacer()
                    Text(e.1).font(.system(size: 13)).foregroundStyle(Color.lkMuted).monospacedDigit()
                }
                .padding(.horizontal, 16).padding(.vertical, 10)
            }
        }
    }
}


/// 已装配件的清单（V1 §8）：扫 ~/Applications 和 ~/Library/Input Methods。
enum Accessories {
    struct Manifest {
        let accessory: String
        let bundleID: String
        let name: String
        let resident: Bool
        let type: String
        let app: URL
        let version: String
    }

    static func installed() -> [Manifest] {
        let home = FileManager.default.homeDirectoryForCurrentUser
        var out: [Manifest] = []
        for dir in [home.appendingPathComponent("Applications"), home.appendingPathComponent("Library/Input Methods")] {
            let items = (try? FileManager.default.contentsOfDirectory(at: dir, includingPropertiesForKeys: nil)) ?? []
            for app in items where app.pathExtension == "app" {
                let m = app.appendingPathComponent("Contents/Resources/luoke-accessory.json")
                guard let data = try? Data(contentsOf: m), let o = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                      o["contract"] as? Int == 1, let acc = o["accessory"] as? String, let bid = o["bundle_id"] as? String else { continue }
                out.append(Manifest(accessory: acc, bundleID: bid, name: o["name"] as? String ?? acc,
                                    resident: o["resident"] as? Bool ?? false, type: o["type"] as? String ?? "app", app: app,
                                    version: o["version"] as? String ?? ""))
            }
        }
        return out
    }

    /// 配件最近一次的常驻声明（主控持久化在 state.json；登录时主控可能还没起，直接读文件），没声明过的用清单默认值。
    static func wantsResident(_ m: Manifest) -> Bool {
        let stateURL = AppInfo.envDir.appendingPathComponent("state.json")
        if let data = try? Data(contentsOf: stateURL), let o = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
           let declared = (o["resident"] as? [String: Bool])?[m.accessory] {
            return declared
        }
        return m.resident
    }

    static let memoryBundleID = "ai.luoke.memory"
    /// 正在被卸载的配件：退出监听别再把它拉起来
    static var noRestart: Set<String> = []

    static func isRunning(_ bundleID: String) -> Bool {
        return !NSRunningApplication.runningApplications(withBundleIdentifier: bundleID).isEmpty
    }

    /// 拉起要常驻的配件，入口后加 --resident（系统拉起的配件和组件配件不在此列）。已在运行的不碰（再拉只会变成"再次打开"）。
    static func launchResident() {
        // 压着不重启的（升级中排队等换装、正在卸载的）不拉（luoke-top 10-23 核查：之前升级时每装好一项就把它们拉起来、再按路径结束）
        for m in installed() where m.type == "app" && wantsResident(m) && !isRunning(m.bundleID) && !noRestart.contains(m.bundleID) {
            launch(m)
        }
    }

    static func launch(_ m: Manifest) {
        AppPlacement.open(m.app, newInstance: true, background: true, args: ["--resident"])
    }

    // ---- 异常退出就重启（V1 §1）：配件 exiting 过的不算；十分钟内最多拉 3 次，免得循环 ----
    private static var restarts: [String: [Date]] = [:]
    private static var observer: NSObjectProtocol?

    static func watchResident() {
        observer = NSWorkspace.shared.notificationCenter.addObserver(forName: NSWorkspace.didTerminateApplicationNotification,
                                                                     object: nil, queue: .main) { note in
            guard let app = note.userInfo?[NSWorkspace.applicationUserInfoKey] as? NSRunningApplication,
                  let bid = app.bundleIdentifier, !noRestart.contains(bid), let m = installed().first(where: { $0.bundleID == bid }),
                  m.type == "app", wantsResident(m) else { return }
            let exited = HubModel.shared.status?.exited[m.accessory] ?? 0
            if Date().timeIntervalSince1970 - exited < 30 { return }     // 它自己说了要退出
            var times = (restarts[m.accessory] ?? []).filter { Date().timeIntervalSince($0) < 600 }
            if times.count >= 3 { NSLog("常驻配件 %@ 十分钟内退出 3 次，不再重启", m.accessory); return }
            times.append(Date())
            restarts[m.accessory] = times
            NSLog("常驻配件 %@ 异常退出，重启", m.accessory)
            launch(m)
        }
    }
}
