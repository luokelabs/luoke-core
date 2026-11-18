// 螺壳本体：菜单栏状态灯 + 设置页（V1 §1）。没有主窗口；首次运行流程在 FirstRun.swift。
import AppKit
import Network
import ServiceManagement
import SwiftUI
import UserNotifications

/// 入口。SwiftUI 启动时会挑一个场景当初始窗口显示（AppWindowsController.showInitialWindow，在交给 AppDelegate 之前）；
/// 螺壳唯一的场景是凑数的 Settings，于是每次启动都多出一个空的「“螺壳”设置」窗口（11-17 查到，开发实例上每次都有）。
/// macOS 15 起可以让它启动时不显示（.defaultLaunchBehavior(.suppressed)），但 SceneBuilder 里不能按 #available 分支，
/// 所以按系统版本选两个 App 之一；macOS 14 上没有这个修饰符，靠 PlaceholderSettings 出现即关
@main
enum LuokeMain {
    static func main() {
        LuokeScenes.runHelperModes()
        if #available(macOS 15, *) { LuokeCoreApp.main() } else { LuokeCoreAppMacOS14.main() }
    }
}

@available(macOS 15, *)
struct LuokeCoreApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) var delegate
    var body: some Scene { LuokeScenes.placeholder.defaultLaunchBehavior(.suppressed) }
}

struct LuokeCoreAppMacOS14: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) var delegate
    var body: some Scene { LuokeScenes.placeholder }
}

enum LuokeScenes {
    /// 不起界面的几种用法：认出来就原地做完，不往下起 App
    static func runHelperModes() {
        // 本体、主控起包外的程序时经这里带上 disclaim 换成它（Spawn.swift）：不起界面，原地换掉
        if CommandLine.arguments.count >= 3, CommandLine.arguments[1] == Spawn.disclaimFlag {
            Spawn.execDisclaimed(Array(CommandLine.arguments.dropFirst(2)))
        }
        // 替扫描弹密码框的子进程（ScanElevate.swift）：本体用干净的环境起它，不起界面，弹完就退
        if CommandLine.arguments.count == 2, CommandLine.arguments[1] == ScanElevate.childFlag { ScanElevate.child() }
        // 主控跑工具的空闲任务（ToolLauncher.swift）：起工具、核签名、等它退出，不起界面
        if CommandLine.arguments.count >= 2, CommandLine.arguments[1] == ToolLauncher.flag {
            ToolLauncher.main(Array(CommandLine.arguments.dropFirst(2)))
        }
    }

    /// 菜单栏图标和菜单在 MenuBar.swift（NSStatusItem）。SwiftUI 至少要一个场景：留一个空的 Settings，只为挂顶部菜单；⌘, 改走 SettingsWindow。
    /// 它不该以窗口出现：启动时不显示（见 LuokeMain），reopen 一律答 false（10-23 升级时 reopen 打开过它；螺壳只有一个窗口，11-10）
    static var placeholder: some Scene {
        Settings { EmptyView() }
            .commands {
                CommandGroup(replacing: .appSettings) {
                    Button("设置") { SettingsWindow.show() }.keyboardShortcut(",", modifiers: .command)
                }
                MainWindowCommands()      // 主窗口的「前往」、收起侧栏（这一份没带主窗口时是空的）
            }
    }
}

/// 凑数的 Settings 场景万一还是出现了（macOS 14 上启动时没法不显示它；以后 SwiftUI 再换打开的路），一出现就关掉、
/// 不参与窗口恢复，并在 hub.log 记下调用栈。macOS 15 起正常启动不会走到这里（LuokeMain）
enum PlaceholderSettings {
    /// SwiftUI 给 Settings 场景窗口的标识（defaults 里存位置的键也是它）
    static let id = "com_apple_SwiftUI_Settings_window"

    static func isPlaceholder(_ w: NSWindow) -> Bool {
        w.identifier?.rawValue == id || w.frameAutosaveName == id
    }

    static func guardAgainstShowing() {
        // 成为主键窗口时同步发通知：调用栈里有打开它的那条路
        NotificationCenter.default.addObserver(forName: NSWindow.didBecomeKeyNotification, object: nil, queue: nil) { n in
            if let w = n.object as? NSWindow { closeIfPlaceholder(w, via: "becomeKey") }
        }
        // 不当主键窗口就出现的（例如窗口恢复）：看可见性
        NotificationCenter.default.addObserver(forName: NSWindow.didChangeOcclusionStateNotification, object: nil, queue: nil) { n in
            if let w = n.object as? NSWindow, w.occlusionState.contains(.visible) { closeIfPlaceholder(w, via: "visible") }
        }
        for w in NSApp.windows where w.isVisible { closeIfPlaceholder(w, via: "launch") }   // 启动前已经恢复出来的
    }

    private static func closeIfPlaceholder(_ w: NSWindow, via: String) {
        guard isPlaceholder(w) else { return }
        w.isRestorable = false
        let stack = Thread.callStackSymbols.prefix(24).joined(separator: "\n  ")
        BundledApps.note("空的设置窗口被打开（\(via)），关掉。调用栈：\n  \(stack)")
        DispatchQueue.main.async { w.close() }
    }
}

final class AppDelegate: NSObject, NSApplicationDelegate, UNUserNotificationCenterDelegate {
    /// 点了主控自己发的通知（装好了、升级好了、没下下来）：见 DoneAttention.clicked
    func userNotificationCenter(_ center: UNUserNotificationCenter, didReceive response: UNNotificationResponse,
                                withCompletionHandler completionHandler: @escaping () -> Void) {
        let info = response.notification.request.content.userInfo
        DispatchQueue.main.async {
            if MainWindow.notificationClicked(info) { return }     // agent 核心发的：打开主窗口，简报的在「今天」最上面打开简报
            if MailHelper.notificationClicked(info) { return }     // 邮件助手的：打开它的页面
            DoneAttention.clicked(info)
        }
        completionHandler()
    }

    /// 主控在前台时也显示横幅（完成页可能被别的窗口挡着）
    func userNotificationCenter(_ center: UNUserNotificationCenter, willPresent notification: UNNotification,
                                withCompletionHandler completionHandler: @escaping (UNNotificationPresentationOptions) -> Void) {
        completionHandler([.banner, .sound])
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        PlaceholderSettings.guardAgainstShowing()
        if let i = CommandLine.arguments.firstIndex(of: "--render-ui"), i + 1 < CommandLine.arguments.count {
            UIRender.run(to: CommandLine.arguments[i + 1])      // 截图用：画完就退，不起主控
            exit(0)
        }
        if let i = CommandLine.arguments.firstIndex(of: "--check-window"), i + 1 < CommandLine.arguments.count {
            #if canImport(LuokeWindow)
            AgentCore.checkWindow(to: CommandLine.arguments[i + 1])     // 打包后的自检：画一遍主窗口就退，不起主控
            #endif
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
            // 一个窗口的例外：这一份不是装好的螺壳，不开主窗口，这时只有这一个框
            let a = NSAlert()
            a.messageText = "这不是装好的螺壳"
            a.informativeText = "请打开「应用程序」文件夹里的「\(AppInfo.appName)」。"
            a.runModal()
            NSApp.terminate(nil)
            return
        }
        // 正式版、开发版、开发实例用同一个环境目录（AppInfo.envDir）：另一个螺壳已经开着，这一个什么都不起，把那一个叫到前面就退。
        // 两个同时开，主控抢 socket，邮件助手、agent 抢同一份数据
        if AppInfo.hubAnswering(at: AppInfo.socketPath) {
            BundledApps.note("另一个螺壳开着（\(AppInfo.socketPath) 有主控在应答），\(AppInfo.appName) 不起，退出")
            let me = ProcessInfo.processInfo.processIdentifier
            if let other = NSWorkspace.shared.runningApplications.first(where: {
                $0.processIdentifier != me && ($0.bundleIdentifier ?? "").hasPrefix("ai.luoke.core") }), let url = other.bundleURL {
                NSWorkspace.shared.openApplication(at: url, configuration: NSWorkspace.OpenConfiguration()) { _, _ in
                    DispatchQueue.main.async { NSApp.terminate(nil) }
                }
            } else {
                NSApp.terminate(nil)
            }
            return
        }
        MenuBar.shared.install()
        Installer.retireStrayCopies()         // 两处各有一份（老用户又拖了一份进 /Applications）：另一份挪进 previous.noindex（ADR 0012）
        // 上一次换包的结果（ADR 0012）：没成的话现在跑的是脚本放回的旧版，"刚升级"的标记作废，照实说一声
        if let r = Swap.takeResult(), !r.ok {
            try? FileManager.default.removeItem(at: Installer.upgradeFile)
            let text = (r.why.contains("Operation not permitted")
                ? "macOS 的「App 管理」权限挡住了这次替换。到 系统设置 → 隐私与安全性 → App 管理 里打开「\(AppInfo.appName)」，再检查一次更新。"
                : r.why + "。") + "原来的螺壳照常能用。"
            // 带主窗口：盖在主窗口上的一张卡片，不弹框（11-10：只有一个窗口）
            if !UpgradeNotDone.show(text) {
                let a = NSAlert()
                a.messageText = UpgradeNotDone.title
                a.informativeText = text
                DispatchQueue.main.async { a.runModal() }
            }
        }
        CommandLineTool.repoint()               // 改名、换位置以后命令行链接跟上
        Updater.cleanupAfterUpgrade()          // 上一次经"检查更新"升级时挂的 DMG：卸下、删掉（首次设置没走完也做，luoke-core#28）
        // 用户自己打开螺壳就开主窗口；登录时自动启动、点通知或链接拉起的不开。首次运行、升级的窗口先出，走完再开（#112）
        let byUser = Self.openedByUser(notification)
        let preselect = Installer.takePreselect()
        if FirstRun.needed() {
            _ = Installer.takeUpgrade()           // 首次运行没走完就被升级了：照首次运行走，标记作废
            MainWindow.openAfterSetup = byUser
            FirstRun.show(preselect: preselect)   // 模型准备好之后（fetch 那一步）再起主控，引擎启动时才看得见模型
        } else {
            HubModel.shared.startHub()
            if let up = Installer.takeUpgrade() {   // 安装器刚把本体换成这一版：应用、引擎、模型也更新到清单的版本，然后再拉起常驻配件
                LoginItem.repoint()     // 登记找不到了、或换了位置才重新登记；每登记一次系统就弹一次「已添加登录项」（#130）
                MainWindow.openAfterSetup = byUser && preselect == nil
                UpgradeWindow.show(from: up.from, to: up.to, then: preselect)   // 应用的安装器顺带升级了螺壳：升级完到「应用」窗口里接着装它
            } else {
                Accessories.launchResident()   // 主控是唯一的登录项：常驻配件由它在登录时拉起（V1 §1）
                if let p = preselect { FirstRun.showAdd(accessory: p) }
                else if byUser { MainWindow.show() }
            }
        }
        Accessories.watchResident()
    }

    /// 这次启动是用户自己打开的（访达、启动台、程序坞，或安装器、升级换包后打开的）。不算：系统按登录项拉起的；
    /// 为打开链接（luoke://memory）、点通知、恢复上次的窗口而起的（launchIsDefault 是 false）。只在 applicationDidFinishLaunching 里准
    static func openedByUser(_ launch: Notification) -> Bool {
        let isDefault = launch.userInfo?[NSApplication.launchIsDefaultUserInfoKey] as? Bool ?? true
        return isDefault && !byLoginItem()
    }

    /// 正在处理的启动（或再次打开）事件带着「按登录项拉起」的标记
    static func byLoginItem() -> Bool {
        let e = NSAppleEventManager.shared().currentAppleEvent
        return e?.paramDescriptor(forKeyword: AEKeyword(keyAEPropData))?.enumCodeValue == OSType(keyAELaunchedAsLogInItem)
    }

    /// 用户在主控运行时又打开了它（Dock / 启动台），或者某个应用的安装器交过预选来：有预选就接着走加装（安装器通常只激活我们，预选由状态定时器取走；这里兜底）
    /// 没有预选时（#19、#20）：有开着的窗口（首次运行、升级、卸载……）就叫到前面；都没有就弹主控的小窗口，菜单栏图标找不到也有入口。
    /// 带主窗口的这一份只有主窗口：首次运行、升级也画在里面，叫回来的都是它（HomeWindow 也改开主窗口）
    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        // 一律答 false：要开的窗口这里都自己开了；答 true 会让 SwiftUI 再按默认做法开它的场景（设置）
        if Self.byLoginItem() { return false }      // 已经在跑，登录时系统又按登录项拉了一次：不是用户打开的（#112）
        if let p = Installer.takePreselect() {
            if FirstRun.needed() { FirstRun.show(preselect: p) } else { FirstRun.showAdd(accessory: p) }
            return false
        }
        let ready = !Installer.isInstallerLaunch() && Installer.isCanonicalCopy && !FirstRun.needed()
        // 带了主窗口（luoke-core#68）：再次打开螺壳就是打开它；首次运行、升级的窗口开着时先把它们叫回来，走完再开主窗口（#112）
        if ready && MainWindow.available && !FirstRun.isOpen && !UpgradeWindow.isOpen {
            MainWindow.show()
            return false
        }
        if !Installer.isInstallerLaunch() && Installer.isCanonicalCopy { MainWindow.openAfterSetup = true }
        // 首次运行、升级画在主窗口里，主窗口被关了：再开主窗口就回到它们（窗口关了它们还在）
        if FirstRun.showing || MainWindow.showing(UpgradeWindow.hostID) { MainWindow.show(); return false }
        if AppWindows.bringBack() { return false }
        if Installer.isInstallerLaunch() || !Installer.isCanonicalCopy { return false }
        // 首次运行没走完、窗口被关了：回到向导（之前什么都不做，菜单停在「启动中」，只有退出再开才回得去）
        if FirstRun.needed() { FirstRun.show() } else { HomeWindow.show() }
        return false
    }

    /// luoke://memory：各工具打开记忆界面（主控没在跑时系统会拉起主控，再把链接交过来）
    func application(_ application: NSApplication, open urls: [URL]) {
        if Installer.isInstallerLaunch() || !Installer.isCanonicalCopy { return }
        for u in urls { _ = Memory.handle(u) }
    }

    func applicationWillTerminate(_ notification: Notification) {
        BundledApps.all.forEach { $0.stop() }
        MainWindow.stop()
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
        let problem = CodeSigning.bundleProblem      // 它们带着螺壳的权限：包不对就都不起。检查更新还要留着
        if let problem {
            hubError = "\(problem)，请重新安装螺壳。"
            startFailed = true
        } else {
            do {
                try process.start()
                hubError = nil
                startFailed = false
            } catch {
                hubError = "螺壳进程起不来：\(error.localizedDescription)"
                startFailed = true
            }
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
        if problem == nil {
            MainWindow.startCore()     // 主控起来了：起 agent 核心、连上它（带了主窗口才有；重复调不要紧）
            BundledApps.all.forEach { $0.start() }      // 收进来的应用也由本体起（带了才有）
        }
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
        guard !FirstRun.needed(), !FirstRun.isOpen, !UpgradeWindow.isOpen, Manifests.hasRelease,
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
    func setBatteryMode(_ mode: String) { update(["battery_mode": mode]) }
    @Published var tierError: String?
    var tierErrorTier: String?      // tierError 说的是哪一档（主窗口的设置画在那一行上）
    func setTier(_ tier: String) {
        // 换到要新下底座的档前先看磁盘（审查 2026-10-17）
        let base = ((Manifests.recipe()?["assignment"] as? [String: Any])?[tier] as? [String: Any])?["base"] as? String
        if let b = base, !Manifests.modelInstalled(b) {
            let need = Manifests.itemSize(b) + (1 << 30)
            let free = (try? URL(fileURLWithPath: NSHomeDirectory()).resourceValues(forKeys: [.volumeAvailableCapacityForImportantUsageKey]).volumeAvailableCapacityForImportantUsage) ?? 0
            if free < need {      // 不弹窗，写在这一行下面（10-23）
                tierErrorTier = tier
                tierError = "磁盘空间不够：换这一档要下载 \(Diagnostics.humanSize(Manifests.itemSize(b)))，现在只剩 \(Diagnostics.humanSize(free))。"
                return
            }
        }
        tierErrorTier = nil
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
        if st.breaker != nil || (!st.engineAlive && !st.engineMissing) { return "error" }
        if !st.engineReady { return "offline" }
        if st.job != nil || st.interactive { return "busy-50" }
        if st.backgroundState != "running" { return "unloaded" }
        return "loaded"
    }
}

/// 设置窗口（品牌规范的设置行：左图标、标题 + 说明、右控件；分组卡片）
struct SettingsView: View {
    @EnvironmentObject var model: HubModel

    var body: some View {
        LKPage {
            Text("设置").font(.system(size: 20, weight: .semibold))
            HubModelSettings(about: false)
            HubPrivacySettings()
            HubAboutSettings(uninstallBack: { SettingsWindow.show() })
        } footer: {
            Spacer()
            Button("完成") { NSApp.keyWindow?.close() }.buttonStyle(LKPrimaryButton())
        }
        .frame(width: 560, height: 680)
    }
}

/// 设置里归主控的几块：只有没带主窗口的这一份用（菜单栏「设置…」的窗口把它们拼在一起）。带主窗口时本体给数据
/// （`AgentCore.settingsSnapshot`），「设置」页由主窗口画。
enum SettingsParts {
    static func header(_ t: String) -> some View {
        Text(t).font(.system(size: 11, weight: .medium)).foregroundStyle(Color.lkMuted).padding(.top, 20).padding(.bottom, 8)
    }

    /// mono：版本号、模型名、地址用等宽（规范 v2 §3）
    static func info(_ label: String, _ value: String, mono: Bool = false) -> some View {
        HStack(alignment: .top) {
            Text(label).font(.system(size: 14))
            Spacer(minLength: 16)
            Text(value).font(.system(size: 13, design: mono ? .monospaced : .default)).foregroundStyle(Color.lkMuted).multilineTextAlignment(.trailing).textSelection(.enabled)
        }
        .padding(.horizontal, 16).padding(.vertical, 10)
    }

    static var divider: some View { Divider().overlay(Color.lkBorder) }

    /// 主控还没起来时的那一句
    @ViewBuilder static func starting(_ model: HubModel) -> some View {
        LKNote(icon: "triangle-alert", text: model.hubError ?? "螺壳启动中", kind: model.hubError == nil ? .info : .danger)
    }
}

/// 「通用」那一块（标签的 rawValue 仍是 model）：通用（能耗、电池、登录时启动）、下载、模型、高级（默认收起：原名、本机接口）、关于、卸载
struct HubModelSettings: View {
    @EnvironmentObject var model: HubModel
    @State private var loginItem = SMAppService.mainApp.status == .enabled
    @State private var loginError: String?
    @State private var advanced: Bool
    /// 主窗口「设置 · 通用」最底下接着画关于和卸载；拼成一个窗口时（SettingsView）由外面放到最后
    let about: Bool
    let uninstallBack: (() -> Void)?
    static var sampleTiers: [Manifests.TierChoice]? = nil     // 截图用（--render-ui）

    init(about: Bool = true, uninstallBack: (() -> Void)? = nil, advanced: Bool = false) {
        self.about = about
        self.uninstallBack = uninstallBack
        _advanced = State(initialValue: advanced)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            if let st = model.status { sections(st) } else { SettingsParts.starting(model) }
            if about { HubAboutSettings(uninstallBack: uninstallBack) }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    private func header(_ t: String) -> some View { SettingsParts.header(t) }
    private func info(_ label: String, _ value: String, mono: Bool = false) -> some View { SettingsParts.info(label, value, mono: mono) }
    private var divider: some View { SettingsParts.divider }

    @ViewBuilder private func sections(_ st: HubStatus) -> some View {
        header("通用")
        LKGroup {
            LKSettingRow(icon: "gauge", title: "能耗模式") {
                LKSegmented(options: [("full", "全速"), ("balanced", "均衡"), ("quiet", "静音")],
                            selection: Binding(get: { st.powerMode }, set: { model.setPowerMode($0) }))
            }
            divider
            LKSettingRow(icon: "battery-medium", title: "用电池时") {
                LKSegmented(options: [("normal", "照常运行"), ("light", "少干活"), ("no_large_model", "不运行大模型")],
                            selection: Binding(get: { st.batteryMode }, set: { model.setBatteryMode($0) }))
            }
            divider
            LKSettingRow(icon: "power", title: "登录时启动螺壳",
                         detail: loginError.map { "没设置上：\($0)" }
                            ?? (SMAppService.mainApp.status == .requiresApproval ? "要在「系统设置 › 通用 › 登录项」里允许一次。" : nil)) {
                Toggle("", isOn: Binding(get: { loginItem }, set: { on in
                    loginError = nil
                    do { if on { try LoginItem.register() } else { try LoginItem.unregister() } }
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
                    LKSettingRow(icon: "circle-arrow-down", title: "还缺：" + st.neededDownloads.joined(separator: "、"),
                                 detail: model.meteredNetwork ? "当前网络按流量计费或开启了低数据模式，不会自动下载。" : nil) {
                        Button { model.retryDownloads() } label: { LKLabel("重试", icon: "rotate-cw") }.buttonStyle(LKSecondaryButton())
                    }
                }
            }
        }
        // 模型：给用户看的只有占多少空间、已经下了多少；模型的原名、清单放进「高级」（11-11 界面走查）
        header("模型")
        LKGroup {
            let choices = Self.sampleTiers ?? Manifests.tierChoices(memGB: Diagnostics.memGB())
            if choices.count > 1 {
                LKSettingRow(icon: "hard-drive", title: "占多少空间", detail: Self.tierDetail(choices)) {
                    LKSelect(options: choices.map { ($0.tier, $0.label) },     // 选了还没换过去的，显示选的那个
                             selection: Binding(get: { st.pendingTier ?? st.tier }, set: { model.setTier($0) }))
                }
                if let e = model.tierError {
                    Text(e).font(.system(size: 12)).foregroundStyle(Color.lkDangerText)
                        .fixedSize(horizontal: false, vertical: true).padding(.horizontal, 14).padding(.bottom, 10)
                }
                divider
            }
            if st.pendingTier != nil {     // luoke-core#25：换档等着底座，界面上要看得出来
                info("换档", (st.downloading ? "正在下载" : "还没下好") + "，下好后自动切换")
                divider
            }
            LKSettingRow(icon: "circle-arrow-down", title: "已下载") {
                Text(StorageView.total()).font(.system(size: 13)).foregroundStyle(Color.lkMuted).monospacedDigit()
            }
        }
        // 高级：给开发者的（模型原名、本机接口），默认收起。折叠行（规范 v2 §6）：整行能点，箭头在左、展开时转 90°，
        // 占的是其他行图标那一列
        LKGroup {
            Button { withAnimation(.easeOut(duration: 0.15)) { advanced.toggle() } } label: {
                HStack(spacing: 12) {
                    LKIcon("chevron-right").foregroundStyle(Color.lkMuted).rotationEffect(.degrees(advanced ? 90 : 0)).frame(width: 20)
                    Text("高级").font(.system(size: 14))
                    Spacer(minLength: 0)
                }
                .padding(.horizontal, 16).padding(.vertical, 12)
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .accessibilityLabel(advanced ? "收起高级" : "展开高级")
        }
        .padding(.top, 20).padding(.bottom, advanced ? 12 : 0)
        if advanced {
            LKGroup {
                info("底座模型", ModelName.display(st.base ?? "—"), mono: true)      // 硬件档、引擎、本机试跑在诊断信息里
                divider
                StorageView()
            }
            .padding(.bottom, 12)
            EndpointView()
        }
    }

    /// 「占多少空间」的说明：只说大小，不写模型原名（原名在「高级」里）
    static func tierDetail(_ choices: [Manifests.TierChoice]) -> String {
        choices.map { "\($0.label)约 \(String(format: "%.0f", $0.sizeGB)) GB" }.joined(separator: "，") + "。换了会下载另一个，下完自动切换。"
    }
}

/// 「隐私」那一块：推理只在本机、记忆（带了记忆时）。关于、卸载在「通用」页最底下（HubAboutSettings，11-11 界面走查）
struct HubPrivacySettings: View {
    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            SettingsParts.header("隐私")
            LKGroup {
                LKSettingRow(icon: "lock", title: "推理只在本机", detail: "模型在这台 Mac 上跑，没有遥测。") { EmptyView() }
                if Memory.bundled {
                    SettingsParts.divider
                    LKSettingRow(icon: "brain", title: "记忆") {
                        Button("打开") { Memory.open() }.buttonStyle(LKSecondaryButton())
                    }
                }
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }
}

/// 关于（版本、导出诊断信息）和卸载：卸载单独一组放在最底下，不和「导出」挨着（11-11 界面走查）。uninstallBack：卸载页点「取消」「完成」回哪
struct HubAboutSettings: View {
    @EnvironmentObject var model: HubModel
    var uninstallBack: (() -> Void)? = nil

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            SettingsParts.header("关于")
            LKGroup {
                if let rel = model.status?.release {
                    LKSettingRow(icon: "info", title: "螺壳版本") {
                        Text(rel.hasPrefix("local-") ? "\(rel)（本机版，没上传）" : rel).font(.system(size: 13, design: .monospaced)).foregroundStyle(Color.lkMuted).textSelection(.enabled)
                    }
                    SettingsParts.divider
                }
                LKSettingRow(icon: "share", title: "导出诊断信息") {
                    Button("导出") { Diagnostics.export(model: model) }.buttonStyle(LKSecondaryButton())
                }
            }
            LKGroup {
                LKSettingRow(icon: "trash-2", title: "卸载螺壳") {
                    Button("卸载") { SettingsWindow.window?.close(); Uninstall.show(preselect: UninstallModel.all, back: uninstallBack) }.buttonStyle(LKPrimaryButton(destructive: true))
                }
            }
            .padding(.top, 28)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }
}

/// 存储页：每个模型占多少、谁在用（V1 §1）。开发版只列模型目录里的东西和大小，删除按钮留到安装器那一块。
struct StorageView: View {
    static var sample: [(String, Int64)]? = nil     // 截图用（--render-ui），不读本机的模型目录

    static func sized() -> [(String, Int64)] {
        if let s = sample { return s }
        let items = (try? FileManager.default.contentsOfDirectory(atPath: AppInfo.modelDir)) ?? []
        return items.sorted().map { ($0, Diagnostics.directorySize(URL(fileURLWithPath: AppInfo.modelDir).appendingPathComponent($0))) }
    }

    /// 「模型」里那一行：一共下了多少
    static func total() -> String {
        let s = sized()
        return s.isEmpty ? "还没有模型" : Diagnostics.humanSize(s.reduce(0) { $0 + $1.1 })
    }

    var entries: [(String, String)] { Self.sized().map { ($0.0, Diagnostics.humanSize($0.1)) } }

    var body: some View {
        if entries.isEmpty {
            Text("还没有模型").font(.system(size: 13)).foregroundStyle(Color.lkMuted).padding(.horizontal, 16).padding(.vertical, 10)
        } else {
            ForEach(Array(entries.enumerated()), id: \.offset) { i, e in
                if i > 0 { Divider().overlay(Color.lkBorder) }
                HStack {
                    Text(ModelName.display(e.0)).font(.system(size: 13, design: .monospaced))
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
        var bundled = false       // 在本体里（BundledApps），app 是螺壳自己，没有单独的 .app
    }

    /// includeDisabled：连用户停用了的本体里的应用也列（「应用」窗口、卸载全部要看到它）
    static func installed(includeDisabled: Bool = false) -> [Manifest] {
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
        for b in BundledApps.all {                // 本体里那份盖过 ~/Applications 里还没迁走的旧包
            guard let m = b.manifest else { continue }
            out.removeAll { $0.bundleID == m.bundleID }
            if includeDisabled || b.enabled { out.append(m) }
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
        for m in installed() where m.type == "app" && wantsResident(m) && !isRunning(m.bundleID) && !noRestart.contains(m.bundleID) && !retired(m) {
            launch(m)
        }
    }

    /// 并进本体以后不再单独拉起的：旧的「螺壳 agent.app」（luoke-core#68，主窗口和 agent 核心在本体里；它还装着的话手动删）
    static func retired(_ m: Manifest) -> Bool {
        m.bundleID == AgentProcess.oldBundleID && MainWindow.available
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
                  m.type == "app", wantsResident(m), !retired(m) else { return }
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
