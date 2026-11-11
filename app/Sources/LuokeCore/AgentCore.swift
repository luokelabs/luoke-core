// 主窗口和 agent 核心并进本体（luoke-core#68，beta 2 精简版）：
// - 主控起来以后起 agent 核心（自带的 Python 跑 Resources/agent 里的 luoke_agent），看住它；本体退出时它跟着退；
// - 主窗口是 luoke-agent 的 LuokeWindow：画今天、应用页和设置；模型、隐私、添加应用、升级是本体的界面，画在主窗口里（11-09 负责人定），不另开窗口；
// - 螺壳只有一个窗口（11-10 负责人定）：本体别的界面也都画在主窗口里，不另弹窗口、不弹框（MainWindow.present）——
//   首次运行、升级占整个窗口；检查更新、更新说明、卸载、删除数据占一页；要权限、开启螺壳搜索、「升级没完成」是盖在页上的卡片；
//   记忆、邮件助手打开的是主窗口里它们的页，不开浏览器；存诊断信息的框挂在主窗口上。没带主窗口的这一份照旧开各自的小窗口；
//   安装器（螺壳还没装好、正在换包）那时没有主窗口，照旧是它自己的一个窗口；
// - 菜单栏菜单加急事、打开螺壳、暂停提醒一小时；核心要发的系统通知以「螺壳」的名义发，点了打开主窗口。
// 两边怎么接见 luoke-agent 的 docs/界面接口.md §14。编的时候没带 luoke-agent（app/Package.swift 的 LUOKE_AGENT_SRC）就都没有，
// 别处只经 MainWindow 用它，不用写 #if。
import AppKit
import SwiftUI

#if canImport(LuokeWindow)
import Combine
import LuokeWindow
import UserNotifications
#endif

/// 别处用主窗口和 agent 核心的入口；这一份没带主窗口时什么都不做
enum MainWindow {
    /// 这一份带了主窗口和 agent 核心
    static var available: Bool {
        #if canImport(LuokeWindow)
        return AgentProcess.bundled
        #else
        return false
        #endif
    }

    /// 主控起来以后（HubModel.startHub）：起 agent 核心、连上它。重复调不要紧
    static func startCore() {
        #if canImport(LuokeWindow)
        guard available else { return }
        MainActor.assumeIsolated { AgentCore.shared.start() }
        #endif
    }

    /// 螺壳的权限可能变了：重新查一遍，报给权限页和卡片
    static func permissionsChanged() {
        #if canImport(LuokeWindow)
        guard available else { return }
        MainActor.assumeIsolated { AgentCore.shared.reportPermissions() }
        #endif
    }

    /// 本体退出：停 agent 核心
    static func stop() {
        #if canImport(LuokeWindow)
        guard available else { return }
        MainActor.assumeIsolated { AgentCore.shared.stop() }
        #endif
    }

    /// 打开主窗口（菜单「打开螺壳」、再次打开螺壳、点了 agent 的通知）
    static func show() {
        presenter?.show()
    }

    /// 用户自己打开了螺壳（不是登录时系统拉起的），可首次运行、升级还没走完：等它们走完再开主窗口（luoke-core#112）
    static var openAfterSetup = false

    /// 首次运行、升级走完了：用户打开螺壳时该开的主窗口现在开，停在「今天」
    static func showAfterSetup() {
        guard openAfterSetup else { return }
        openAfterSetup = false
        presenter?.show()
        presenter?.showToday()
    }

    // MARK: 本体的界面都画在主窗口里（负责人 11-10：螺壳只有一个窗口，不另弹窗口、不弹框）

    /// 测试换成假的；平时带了主窗口就是主窗口，没带是 nil（调用方照旧开自己的小窗口）
    static var presenterOverride: HostPresenter?
    static var presenter: HostPresenter? {
        if let p = presenterOverride { return p }
        #if canImport(LuokeWindow)
        return available ? AgentPresenter.shared : nil
        #else
        return nil
        #endif
    }

    /// 把本体的一块界面画进主窗口（主窗口没开就打开），答画上去了没有（HostShown）：这一份没带主窗口答 noWindow，调用方照旧开自己的小窗口；
    /// 同一处有一块正忙着答 busy，调用方当没开。
    /// 同一个 id 再来一次就换成新的。canLeave：用户要离开这一块时问一声，答 false 就留着；
    /// closed：用户自己离开了它（换页、关掉卡片），或者被同一处的另一块换掉；lockClose：它开着时主窗口不能关（升级）
    @discardableResult
    static func present<V: View>(_ id: String, title: String, style: HostStyle, canLeave: (() -> Bool)? = nil,
                                 closed: (() -> Void)? = nil, lockClose: Bool = false, @ViewBuilder _ content: () -> V) -> HostShown {
        guard let p = presenter else { return .noWindow }
        let view = AnyView(content().environment(\.lkHost, style.form))
        return p.present(HostScreen(id: id, title: title, style: style, view: view, canLeave: canLeave, closed: closed, lockClose: lockClose)) ? .shown : .busy
    }

    /// 画着的这一块这会儿要不要锁住主窗口的关闭按钮（开启螺壳搜索问着系统的时候）。它没画在主窗口里时不管；
    /// 几块都锁着时，都解开了才能关
    static func lockClose(_ id: String, _ locked: Bool) { presenter?.lockClose(id, locked) }

    /// 撤掉这一块（它自己的按钮：完成、取消、以后再说）。不调 closed
    static func dismiss(_ id: String) { presenter?.dismiss(id) }

    /// 这一块正画在主窗口里（窗口关着时它也还在，再开窗口还是它）
    static func showing(_ id: String) -> Bool { presenter?.showing(id) ?? false }

    /// 首次运行、升级的整窗走完了：撤掉。keepOpen 是 false、主窗口又是为它开的，主窗口跟着关（和原来关掉那个窗口一样）
    static func endSetup(_ id: String, keepOpen: Bool) { presenter?.endSetup(id, keepOpen: keepOpen) }

    /// 主窗口（打开它）：存文件的框挂在它上面，不另弹
    static func sheetParent() -> NSWindow? { presenter?.sheetParent() }

    /// 记忆画在主窗口的「记忆」页里，不再开浏览器：note 是一条理解的编号（记忆的 item 页），view 是 recent（「记下的 › 最近记下的」）
    /// 或 people（「人」）
    static func openMemory(note: String?, view: String?) {
        if let n = note { presenter?.openToolPage(app: "luoke-mem", page: "item", args: ["id": n]) }
        else if view == "recent" { presenter?.openToolPage(app: "luoke-mem", page: "kept", args: ["view": "recent"]) }
        else if view == "people" { presenter?.openToolPage(app: "luoke-mem", page: "people", args: [:]) }
        else { presenter?.openToolPage(app: "luoke-mem", page: nil, args: [:]) }
    }

    /// 应用自己的页（邮件助手……）画在主窗口里，不再开浏览器
    static func openApp(_ accessory: String) { presenter?.openToolPage(app: accessory, page: nil, args: [:]) }

    /// 点了的通知是 agent 核心发的：打开主窗口，简报的通知在「今天」最上面打开简报，别的回「今天」，答 true；不是的答 false
    static func notificationClicked(_ info: [AnyHashable: Any]) -> Bool {
        #if canImport(LuokeWindow)
        guard available, AgentNotification.clicked(info).isAgent else { return false }
        // 简报的通知：「今天」最上面打开那段简报；别的通知回「今天」（LuokeWindow 的 openNotification）
        MainActor.assumeIsolated {
            AgentCore.shared.show()
            AgentCore.shared.model.openNotification(info)
        }
        return true
        #else
        return false
        #endif
    }

    /// 菜单栏菜单里 agent 这边的几项：急事（有才列）、打开螺壳、暂停提醒一小时
    static func menuItems() -> [NSMenuItem] {
        #if canImport(LuokeWindow)
        guard available else { return [] }
        return MainActor.assumeIsolated { AgentCore.shared.menuItems() }
        #else
        return []
        #endif
    }

    /// 「应用」画在主窗口的「添加应用」页里：打开主窗口、翻到那一页，答那一页的 AppsModel；
    /// 这一份没带主窗口答 nil，照旧开「应用」窗口
    static func showApps() -> AppsModel? {
        #if canImport(LuokeWindow)
        guard available else { return nil }
        return MainActor.assumeIsolated { AgentCore.shared.showApps() }
        #else
        return nil
        #endif
    }

    /// 「设置」画在主窗口的「设置」里：打开主窗口、翻到设置（停在上次看的标签），答 true；
    /// 这一份没带主窗口答 false，照旧开「螺壳设置」窗口
    static func showSettings() -> Bool {
        #if canImport(LuokeWindow)
        guard available else { return false }
        MainActor.assumeIsolated { AgentCore.shared.showSettings() }
        return true
        #else
        return false
        #endif
    }

    /// 有急事没处理：菜单栏图标多一个小点
    static var wantsDot: Bool {
        #if canImport(LuokeWindow)
        guard available else { return false }
        return MainActor.assumeIsolated { StatusMenuItems.hasDot(AgentCore.shared.model) }
        #else
        return false
        #endif
    }
}

/// 本体的一块界面画在主窗口的哪儿：一页（右边内容区，侧栏还在；主窗口给标题、滚动，没有「返回」）、
/// 一张卡片（盖在整个窗口上，底下压暗；主窗口给标题、×）、整个窗口（首次运行、升级）
enum HostStyle {
    case page, card, takeover

    /// 界面照哪种画（LKPage、升级卡片、小卡片据此去掉主窗口已经给了的标题、滚动、边距）
    var form: LKHostForm {
        switch self {
        case .page: return .page
        case .card: return .card
        case .takeover: return .takeover
        }
    }
}

/// MainWindow.present 画上去了没有
enum HostShown {
    /// 画进主窗口了
    case shown
    /// 同一处有一块正忙着、走不开（正在卸载、正在删数据、正问着系统）：没画，主窗口叫到前面、停在忙着的那一块。
    /// 调用方当没开：不登记等待，等着回话的那边要知道没开；也不另开小窗口（只有一个窗口）
    case busy
    /// 这一份没带主窗口：调用方照旧开自己的小窗口
    case noWindow
}

/// 画进主窗口的一块本体界面（MainWindow.present）
struct HostScreen {
    let id: String
    let title: String
    let style: HostStyle
    let view: AnyView
    var canLeave: (() -> Bool)? = nil
    var closed: (() -> Void)? = nil
    var lockClose = false
}

/// 把本体的界面画进主窗口的那一方：带主窗口时是主窗口（AgentPresenter），测试里换成假的
protocol HostPresenter: AnyObject {
    /// 画上去了答 true；同一处有一块正忙着、没换上去答 false（主窗口叫到前面）
    func present(_ s: HostScreen) -> Bool
    func lockClose(_ id: String, _ locked: Bool)
    func dismiss(_ id: String)
    func showing(_ id: String) -> Bool
    func endSetup(_ id: String, keepOpen: Bool)
    func show()
    func showToday()
    func sheetParent() -> NSWindow?
    /// 应用的页：page 为 nil 时是它的第一页
    func openToolPage(app: String, page: String?, args: [String: String])
}

/// 顶部菜单里主窗口的「前往」和收起侧栏（主窗口开着时螺壳是普通 App，有顶部菜单）
struct MainWindowCommands: Commands {
    var body: some Commands {
        #if canImport(LuokeWindow)
        LuokeCommands(model: AgentCore.shared.model) { AgentCore.shared.show() }
        #else
        EmptyCommands()
        #endif
    }
}

#if canImport(LuokeWindow)

/// 本体的界面画进主窗口：都在主线程上调（菜单、按钮、主线程上的回调）
final class AgentPresenter: HostPresenter {
    static let shared = AgentPresenter()

    func present(_ s: HostScreen) -> Bool { MainActor.assumeIsolated { AgentCore.shared.present(s) } }
    func lockClose(_ id: String, _ locked: Bool) { MainActor.assumeIsolated { AgentCore.shared.lockClose(id, locked) } }
    func dismiss(_ id: String) { MainActor.assumeIsolated { AgentCore.shared.dismiss(id) } }
    func showing(_ id: String) -> Bool { MainActor.assumeIsolated { AgentCore.shared.showing(id) } }
    func endSetup(_ id: String, keepOpen: Bool) { MainActor.assumeIsolated { AgentCore.shared.endSetup(id, keepOpen: keepOpen) } }
    func show() { MainActor.assumeIsolated { AgentCore.shared.show() } }
    func showToday() { MainActor.assumeIsolated { AgentCore.shared.showToday() } }
    func sheetParent() -> NSWindow? { MainActor.assumeIsolated { AgentCore.shared.sheetParent() } }
    func openToolPage(app: String, page: String?, args: [String: String]) {
        MainActor.assumeIsolated { AgentCore.shared.openToolPage(app: app, page: page, args: args) }
    }
}

/// 主窗口给的顶部让开（占整个窗口时红黄绿的高度）转给本体的界面（它们没带主窗口时也要编得过，只认 lkTopInset）
struct TopInsetBridge: View {
    let content: AnyView
    @Environment(\.lkHostTopInset) private var inset
    var body: some View { content.environment(\.lkTopInset, inset) }
}

extension HostStyle {
    var windowStyle: HostPage.Style {
        switch self {
        case .page: return .page
        case .card: return .card
        case .takeover: return .takeover
        }
    }
}

/// 主窗口、agent 核心的连接、给 LuokeWindow 的本体接口（WindowHost）
@MainActor
final class AgentCore: NSObject, NSWindowDelegate, WindowHost {
    static let shared = AgentCore()

    let model: WindowModel
    /// 测试用：不开真的窗口，只看主窗口里画什么、去哪一页
    private let windowless: Bool
    private(set) var connection: AgentConnection?
    private let process = AgentProcess()
    private var window: NSWindow?
    private var windowOpen = false
    private var started = false
    private var subs: Set<AnyCancellable> = []
    /// 主窗口「添加应用」页上本体的应用目录
    private lazy var apps: AppsModel = {
        let m = AppsModel()
        m.$rows.dropFirst().sink { [weak self] _ in DispatchQueue.main.async { MainActor.assumeIsolated { self?.refreshApps() } } }.store(in: &subs)   // 装了、升了：侧栏的版本和「升级」跟着变
        return m
    }()

    override convenience init() { self.init(windowless: false) }

    init(windowless: Bool) {
        self.windowless = windowless
        model = windowless ? WindowModel() : WindowModel(defaults: .standard)
        super.init()
        model.features = .beta2      // beta 2 精简版：只画今天、应用页、「设置 · 提醒」「设置 · 权限」（顶部「前往」菜单也一样）
        model.host = self
        // 应用列表变了（冷启动时核心刚列出应用）：记着要去的应用页现在去。等这一轮改完 apps 再看
        model.$apps.dropFirst().sink { [weak self] _ in DispatchQueue.main.async { MainActor.assumeIsolated { self?.openPendingToolPage() } } }.store(in: &subs)
        // 用户这期间换了页：记着的就不去了
        model.$page.dropFirst().sink { [weak self] _ in MainActor.assumeIsolated { self?.pendingToolPage = nil } }.store(in: &subs)
    }

    func start() {
        guard !started else { return }
        started = true
        process.onProblem = { [weak self] text in self?.model.hostNotice = text }
        let c = AgentConnection(socketPath: AgentProcess.socketPath, model: model)
        c.feed.onNotify = { $0.post() }      // 时效性通知 beta 2 不加（luoke-core#68）
        c.start()
        c.watchPresence()
        connection = c
        process.start()

        // 通知授权：不在这里问。授权框只跟在用户的操作后面：首次运行的那一行，或「今天」里的「打开通知」
        reportNotifications()
        for (name, front) in [(NSApplication.didBecomeActiveNotification, true), (NSApplication.didResignActiveNotification, false)] {
            NotificationCenter.default.addObserver(forName: name, object: nil, queue: .main) { [weak self] _ in
                MainActor.assumeIsolated {
                    self?.reportWindow()
                    if front { self?.reportNotifications(); self?.refreshApps(); self?.reportPermissions() }
                }
            }
        }

        // 主控的状态：侧栏底部的灯和模型名
        HubModel.shared.$status.receive(on: RunLoop.main).sink { [weak self] st in
            MainActor.assumeIsolated {
                guard let self else { return }
                self.model.engineReady = st?.engineReady ?? false
                self.model.modelName = ModelName.display(st?.loaded ?? st?.base ?? "—")
            }
        }.store(in: &subs)
        // 有没有急事：菜单栏图标的小点
        model.$urgent.receive(on: RunLoop.main).sink { _ in MainActor.assumeIsolated { MenuBar.shared.refreshIcon() } }.store(in: &subs)
        refreshApps()
        reportPermissions()
    }

    func stop() {
        connection?.stop()
        process.stop()
    }

    // MARK: 窗口

    func show() {
        openedFor = nil         // 用户自己要主窗口：首次运行、升级走完也不关
        bringUp()
    }

    private func bringUp() {
        if windowless { windowOpen = true; return }
        let w = window ?? makeWindow()
        refreshApps()
        if !w.isVisible { AppWindows.track(w) }      // 开着时是普通 App（程序坞、⌘Tab、顶部菜单），最后一个窗口关了改回只在菜单栏
        windowOpen = true
        Uninstall.bringFront(w)
        reportWindow()
    }

    private func makeWindow() -> NSWindow {
        // 一个窗口的例外：这就是那个窗口
        let w = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1004, height: 760),
                         styleMask: [.titled, .closable, .miniaturizable, .resizable, .fullSizeContentView],
                         backing: .buffered, defer: false)
        w.title = AppInfo.appName
        w.titleVisibility = .hidden
        w.titlebarAppearsTransparent = true       // 红黄绿落在侧栏左上角留出的位置（LuokeWindow 的 WindowControls）
        w.contentMinSize = NSSize(width: 860, height: 600)
        w.contentView = NSHostingView(rootView: MainWindowView(model: model))
        w.isReleasedWhenClosed = false
        w.delegate = self
        w.center()
        w.setFrameAutosaveName("LuokeMainWindow")
        window = w
        return w
    }

    func windowWillClose(_ notification: Notification) {
        windowOpen = false                         // 关窗时还会来一次 resignKey，之后都按关了算
        reportWindow()
    }
    func windowDidBecomeKey(_ notification: Notification) { reportWindow() }
    func windowDidResignKey(_ notification: Notification) { reportWindow() }
    func windowDidMiniaturize(_ notification: Notification) { reportWindow() }
    func windowDidDeminiaturize(_ notification: Notification) { reportWindow() }

    /// 开着：没关（最小化也算开着）。在最前面：用户正看着它（核心据此决定当场说还是攒着）
    private func reportWindow() {
        guard let c = connection else { return }
        let w = windowOpen ? window : nil
        let open = w.map { $0.isVisible || $0.isMiniaturized } ?? false
        let front = open && w!.isKeyWindow && !w!.isMiniaturized && NSApp.isActive
        c.reportWindow(open: open, front: front)
    }

    private func reportNotifications() {
        UNUserNotificationCenter.current().getNotificationSettings { [weak self] s in
            let status = AgentNotification.authName(s.authorizationStatus)
            let alerts = s.alertSetting != .disabled
            DispatchQueue.main.async {
                MainActor.assumeIsolated {
                    self?.connection?.reportNotifications(status: status, alerts: alerts)
                    self?.model.notificationsNotAsked = s.authorizationStatus == .notDetermined
                }
            }
        }
    }

    /// 查螺壳名下的权限，报给核心（§10 `POST /host`）。核心撤掉已经给了的卡片；重连时窗口自己再报一遍
    func reportPermissions() {
        DispatchQueue.global(qos: .utility).async { [weak self] in
            let states = HostPermissions.current()
            let confirmed = BundledApps.permissionsOk()
            DispatchQueue.main.async {
                MainActor.assumeIsolated {
                    self?.model.reportHostPermissions(states.mapValues { HostPermission(rawValue: $0) }, confirmed: confirmed)
                }
            }
        }
    }

    /// 侧栏「应用」下面的：装着的应用、版本、发布清单里有没有新版（窗口照核心连着的工具列）
    private func refreshApps() {
        var updates: [String: String] = [:]
        for a in AppsWindow.updatable() { updates[a.id] = a.version }
        model.setInstalledApps(Accessories.installed().map { InstalledApp(accessory: $0.accessory, version: $0.version, update: updates[$0.accessory]) })
    }

    // MARK: 菜单栏

    func menuItems() -> [NSMenuItem] {
        StatusMenuItems.items(model: model) { [weak self] in self?.show() }
    }

    // MARK: WindowHost：模型、隐私、添加应用、升级都画在主窗口里

    /// 「设置」的模型、隐私：本体自己的那两块，画在主窗口的设置里（11-09 负责人定，不再另弹「螺壳设置」）
    func settingsContent(_ tab: PrefsTab) -> AnyView? {
        switch tab {
        case .model: return AnyView(HubModelSettings().environmentObject(HubModel.shared))
        case .privacy: return AnyView(HubPrivacySettings(uninstallBack: { [weak self] in self?.showSettings(tab: .privacy) }).environmentObject(HubModel.shared))
        default: return nil
        }
    }

    /// 打开主窗口到「设置」：菜单栏的「设置」、⌘, 经 SettingsWindow.show 到这里，tab 为 nil 停在上次看的标签；卸载页的「取消」「完成」回「隐私」
    func showSettings(tab: PrefsTab? = nil) {
        show()
        model.go(.prefs, tab: tab?.rawValue)
    }

    func openModelSettings() { showSettings(tab: .model) }     // 窗口只在 settingsContent 为 nil 时调；留着不另开窗口

    func askPermission(app: String, id: String) { PermissionWindow.ask(app: app, id: id) }
    func askPermission(app: String, id: String, ready: @escaping @MainActor () -> Void) {
        PermissionWindow.ask(app: app, id: id) { MainActor.assumeIsolated { ready() } }
    }
    func askSystem(id: String) { PermissionWindow.askSystem(id: id) }

    func askNotifications() {
        UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .sound]) { [weak self] _, _ in
            DispatchQueue.main.async { MainActor.assumeIsolated { self?.reportNotifications() } }
        }
    }

    func setPermission(app: String, id: String, on: Bool) {
        if !on { PermissionWindow.withdrawn(app: app, id: id) }
        BundledApps.setPermission(app, id, on: on) { [weak self] why in
            if let why { self?.model.hostNotice = why }
            self?.reportPermissions()
        }
    }
    func openPrivacySettings() { showSettings(tab: .privacy) } // 同上
    /// 「添加应用」页：本体的应用目录，装、升级、开启、停用、换回上一版都在这一页的行里
    func appsContent() -> AnyView? { AnyView(AppsView(m: apps, inWindow: true)) }

    /// 打开主窗口到「添加应用」页（菜单栏的「应用」、「去升级应用」、卸载完回来都经 AppsWindow.show 到这里）
    func showApps() -> AppsModel {
        let showing = window != nil && model.page == .apps  // 已经在这一页（窗口关了再开也算）：页面不会再出现一次，在这里 load
        apps.resetDone()
        show()
        model.go(.apps)
        if showing { apps.load() }                          // 否则页面出现时 load（AppsView 的 onAppear），一次进页面只 load 一遍
        return apps
    }

    func openAddApps() { _ = showApps() }       // 窗口只在 appsContent 为 nil 时调；留着不另开窗口
    func upgrade(app: String) { apps.load() }   // 窗口已经翻到「添加应用」页：那一行有「升级」

    // MARK: 本体的界面画进主窗口（检查更新、卸载、权限、首次运行、升级……，负责人 11-10：只有一个窗口）

    /// 画着的几块（id → 它的回调）
    private var screens: [String: HostScreen] = [:]
    /// 主窗口是为这一块开的（之前关着）：首次运行、升级走完、用户又没要主窗口时跟着关
    private var openedFor: String?
    /// 锁着主窗口关闭按钮的几块：升级（原来的窗口就没有关闭按钮）、开启螺壳搜索问着系统的时候。有一块锁着就关不了
    private var closeLocks: Set<String> = []
    var closeLocked: Bool { !closeLocks.isEmpty }
    /// 首次运行、升级的整窗盖着时打开的一页（菜单栏的「检查更新」）：画在整窗底下，用户还没看过。
    /// 整窗走完停在这一页，不回「今天」（回「今天」就等于用户离开了它，检查更新会当成不升级）
    private var pageUnderSetup: String?

    /// 画上去了答 true；同一处有一块正忙着、没换上去答 false
    @discardableResult func present(_ s: HostScreen) -> Bool {
        let wasOpen = windowOpen && (window?.isVisible == true || window?.isMiniaturized == true)
        // 同一处原来画着别的：正做着、走不开的（正在卸载、正在删数据、正问着系统）不换，把它叫到前面；不然换掉，算用户离开了它
        if screens.values.contains(where: { $0.id != s.id && $0.style == s.style && $0.canLeave?() == false && showing($0.id) }) {
            bringUp()
            return false
        }
        for (id, old) in screens where id != s.id && old.style == s.style { forget(id)?.closed?() }
        screens[s.id] = s
        if s.style == .page { pageUnderSetup = model.hostTakeover != nil ? s.id : nil }
        model.present(HostPage(id: s.id, title: s.title, style: s.style.windowStyle, content: AnyView(TopInsetBridge(content: s.view))))
        bringUp()
        if !wasOpen { openedFor = s.id }
        if s.lockClose { closeLocks.insert(s.id) } else { closeLocks.remove(s.id) }
        updateClosable()
        return true
    }

    /// 画着的这一块锁上、解开主窗口的关闭按钮（MainWindow.lockClose）
    func lockClose(_ id: String, _ locked: Bool) {
        if locked {
            guard screens[id] != nil else { return }
            closeLocks.insert(id)
        } else {
            closeLocks.remove(id)
        }
        updateClosable()
    }

    private func updateClosable() {
        guard let w = window else { return }
        if closeLocks.isEmpty { w.styleMask.insert(.closable) } else { w.styleMask.remove(.closable) }
    }

    func dismiss(_ id: String) {
        _ = forget(id)
        model.dismissHost(id)
    }

    func showing(_ id: String) -> Bool {
        [model.hostPage, model.hostCard, model.hostTakeover].contains { $0?.id == id }
    }

    func endSetup(_ id: String, keepOpen: Bool) {
        let only = openedFor == id
        dismiss(id)
        // 整窗盖着时用户又打开了一页（检查更新）：主窗口留着，露出那一页
        let waiting = pageUnderSetup.map(showing) ?? false
        if !keepOpen && only && !waiting { window?.close() }
    }

    func showToday() {
        if let id = pageUnderSetup, model.hostTakeover == nil {
            pageUnderSetup = nil
            if showing(id) { return }      // 整窗盖着时打开的那一页还没看过：停在它上面
        }
        model.go(.today)
    }

    func sheetParent() -> NSWindow? {
        show()
        return window
    }

    /// 冷启动时核心还没列出应用（记忆只在核心列出来以后才有）：要去的应用页先记着，列出来再去（openPendingToolPage）
    private var pendingToolPage: (app: String, page: String?, args: [String: String], token: Int)?
    private var pendingToken = 0
    /// 等应用列出来最多等多久：过了还没有就算了，停在打开时的那一页（冷启动是「今天」）
    static var toolPageWait: TimeInterval = 30

    func openToolPage(app: String, page: String?, args: [String: String]) {
        show()
        pendingToolPage = nil
        if goToolPage(app: app, page: page, args: args) { return }
        pendingToken += 1
        let token = pendingToken
        pendingToolPage = (app, page, args, token)
        DispatchQueue.main.asyncAfter(deadline: .now() + Self.toolPageWait) { [weak self] in
            MainActor.assumeIsolated { if self?.pendingToolPage?.token == token { self?.pendingToolPage = nil } }
        }
    }

    /// 侧栏里有这个应用：去它的页（page 为 nil 是它的第一页），答 true；还没有答 false
    private func goToolPage(app: String, page: String?, args: [String: String]) -> Bool {
        guard let entry = model.apps.first(where: { $0.tool.accessory == app }) else { return false }
        if let page { model.openAgentPage(app: app, page: page, args: args) } else { model.go(entry.id) }
        return true
    }

    /// 应用列表变了：记着的应用页有了就去。这期间开了本体的一页、整窗（用户在看别的）就不去了
    private func openPendingToolPage() {
        guard let p = pendingToolPage else { return }
        guard model.hostPage == nil, model.hostTakeover == nil else { pendingToolPage = nil; return }
        if goToolPage(app: p.app, page: p.page, args: p.args) { pendingToolPage = nil }
    }

    /// 撤掉它的回调、解开关闭按钮；答原来记着的那一块
    private func forget(_ id: String) -> HostScreen? {
        if openedFor == id { openedFor = nil }
        if pageUnderSetup == id { pageUnderSetup = nil }
        if closeLocks.remove(id) != nil { updateClosable() }
        return screens.removeValue(forKey: id)
    }

    /// 用户要离开本体画的这一块（点侧栏、「前往」菜单）：卸载、开启搜索这些正做着时不让走
    func hostPageCanLeave(_ id: String) -> Bool { screens[id]?.canLeave?() ?? true }

    /// 用户离开了它（窗口已经不画了）
    func hostPageClosed(_ id: String) { forget(id)?.closed?() }

    /// 打包后的自检（`LuokeCore --check-window <目录>`，build_app.sh 调）：用示例应用画一遍主窗口的「今天」和「添加应用」，
    /// 画得出来就正常退出。11-04：internal-1104-2 的主窗口一画应用图标就崩（资源包只在出版机的编译目录里找得到），自检照用户的 Mac 那样跑
    static func checkWindow(to dir: String) {
        try? FileManager.default.createDirectory(atPath: dir, withIntermediateDirectories: true)
        let m = WindowModel()
        m.features = .beta2
        m.setInstalledApps(["luoke-core", "luoke-mail", "luoke-ime", "luoke-scan", "luoke-search"].map { InstalledApp(accessory: $0, version: "0") })
        for page in [Page.today, .apps] {
            m.go(page)
            UIRender.save(AnyView(MainWindowView(model: m)), scheme: .light, to: dir, name: "check-window-\(page.rawValue)", size: NSSize(width: 1004, height: 760))
        }
    }
}

#endif

/// agent 核心进程：自带的 Python 只读地起（-B -E -s），带 --parent-pid，本体没了它自己退出。
/// 意外退出就隔一会儿再起，间隔逐次加长（照 luoke-agent 原来的壳 app/Sources/Luoke/Core.swift）。
final class AgentProcess {
    /// 旧的「螺壳 agent.app」（#67 那几版单独出的，只在团队机器上有，手动删）：它在跑时不起核心，登录时也不再拉起它
    static let oldBundleID = "ai.luoke.agent"
    /// 核心的 Python 源码（build_app.sh 拷进来，预编译、随本体签名）
    static var coreDir: URL { AppInfo.resources.appendingPathComponent("agent", isDirectory: true) }
    static var bundled: Bool { FileManager.default.fileExists(atPath: coreDir.appendingPathComponent("luoke_agent/__main__.py").path) }

    private static var luokeHome: URL {
        if let h = ProcessInfo.processInfo.environment["LUOKE_HOME"] { return URL(fileURLWithPath: h, isDirectory: true) }
        return FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent(".luoke", isDirectory: true)
    }
    /// 数据目录：正式版 ~/.luoke/agent（原来「螺壳 agent.app」用的就是这里，接着用）；
    /// 开发版、实验室构建用自己环境目录下的 agent，不碰用户在用的那份
    static var home: URL {
        AppInfo.isDev || AppInfo.isLab ? AppInfo.envDir.appendingPathComponent("agent", isDirectory: true)
                                       : luokeHome.appendingPathComponent("agent", isDirectory: true)
    }
    /// 记忆文件，和 Memory.open 的规矩一样：实验室构建没给 LUOKE_HOME 就不读本机的（给一个不存在的文件，核心先不读记忆）
    static var memory: URL {
        if AppInfo.isLab && ProcessInfo.processInfo.environment["LUOKE_HOME"] == nil { return home.appendingPathComponent("mem.db") }
        return luokeHome.appendingPathComponent("mem.db")
    }
    static var socketPath: String { home.appendingPathComponent("run/agent.sock").path }

    /// nil：在跑；否则是窗口「今天」里要说的一句
    var onProblem: ((String?) -> Void)?
    private var process: Process?
    /// 给核心的通道：由本体起的工具（螺壳扫描）向它要连接（ToolHost.swift）。核心每起一次换一个
    private var host: ToolHost?
    private var quitting = false
    private var backoff: TimeInterval = 2
    private var startedAt = Date()
    private var oldWatch: NSObjectProtocol?

    func start() {
        guard process == nil, !quitting else { return }
        // 旧的「螺壳 agent.app」还在跑：它占着同一个数据目录。等它退出再起（核心自己也有锁，见 exited 的 3）
        if Self.oldAgentRunning() {
            onProblem?("旧的「螺壳 agent」还在运行：在菜单栏它的图标里退出它，再把它从「应用程序」里删掉，这里就接上了。")
            waitForOldToQuit()
            return
        }
        let fm = FileManager.default
        do {
            try fm.createDirectory(at: Self.home, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
            try fm.createDirectory(at: Self.memory.deletingLastPathComponent(), withIntermediateDirectories: true)
            try fm.createDirectory(at: AppInfo.envDir.appendingPathComponent("run"), withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
        } catch {
            onProblem?("螺壳的核心起不来：\(error.localizedDescription)")
            retry()
            return
        }
        let python = AppInfo.python
        var args = ["-B", "-E", "-s", "-m", "luoke_agent", "serve", "--home", Self.home.path, "--memory", Self.memory.path,
                    "--hub", AppInfo.socketPath, "--parent-pid", String(getpid())]
        if !AppInfo.isDev && !AppInfo.isLab { args.append("--memory-app") }    // 看依据时打开记忆（luoke://memory 只有正式版认领）
        // 开发版、实验室构建：核心也放行命令行的 tap、feed 和窗口演示。只看 bundle id、不看 LUOKE_LAB 环境变量，正式包里打不开
        if AppInfo.isDev || (Bundle.main.bundleIdentifier ?? "").hasSuffix(".lab") { args.append("--dev") }
        // 由本体起的工具（luoke-agent 界面接口 §15）：核心的标准输入是给它的通道。打进来的核心认得 --host-stdin 才给
        let host = FileManager.default.fileExists(atPath: Self.coreDir.appendingPathComponent("luoke_agent/hostlink.py").path)
            ? ToolHost() : nil
        if let host { args.append("--host-stdin") }
        // disclaim：核心不起扫描（扫描由本体经 ToolHost 起），它起的包外程序不借螺壳的授权（Spawn.swift）
        let p = Spawn.process(python, args)
        p.currentDirectoryURL = Self.coreDir                // -m 从这里找 luoke_agent 和 vendor/
        p.environment = ["PATH": "/usr/bin:/bin", "HOME": NSHomeDirectory(), "PYTHONDONTWRITEBYTECODE": "1",
                         "LUOKE_AGENT_PYTHON": python]      // 核心盯着它：本体换了版本就退出（75）要求重起
        p.standardInput = host?.agentEnd ?? FileHandle.nullDevice
        p.standardOutput = FileHandle.nullDevice
        p.standardError = Self.log()                        // 核心只往标准错误写元数据
        p.terminationHandler = { [weak self] proc in
            let code = proc.terminationStatus
            DispatchQueue.main.async { self?.exited(code) }
        }
        do {
            try p.run()
            process = p
            self.host = host
            host?.childStarted()
            startedAt = Date()
            onProblem?(nil)
        } catch {
            host?.stop()
            onProblem?("螺壳的核心起不来。")
            retry()
        }
    }

    private func exited(_ code: Int32) {
        process = nil
        host?.stop()
        host = nil
        if quitting { return }
        switch code {
        case 75:            // 核心见 Python 换了（本体升级、改名）：马上用新的那份起
            backoff = 2
            start()
        case 3:             // 这个数据目录已经有一个核心在跑：不抢它的，隔一分钟再看
            onProblem?("另一个螺壳的核心在用这份数据（多半是旧的「螺壳 agent」）：退出它以后，这里就接上了。")
            retry(after: 60)
        default:
            if Date().timeIntervalSince(startedAt) >= 60 { backoff = 2 }     // 稳定跑过一阵才停的：不接着上次的等待
            onProblem?("螺壳的核心停了，正在重新启动…")
            NSLog("agent 核心退出了（%d），%.0f 秒后重起", code, backoff)
            retry()
        }
    }

    private func retry(after fixed: TimeInterval? = nil) {
        let wait = fixed ?? backoff
        if fixed == nil { backoff = min(backoff * 2, 60) }
        DispatchQueue.main.asyncAfter(deadline: .now() + wait) { [weak self] in self?.start() }
    }

    /// 旧的「螺壳 agent.app」还在跑。已经退出、系统的列表里还没去掉的不算（不然它退出后再查一次还以为在跑，又去等一个不会再来的通知）
    static func oldAgentRunning() -> Bool {
        NSRunningApplication.runningApplications(withBundleIdentifier: oldBundleID).contains { !$0.isTerminated }
    }

    /// 等旧的退出：收到它退出的通知就接上；只靠通知会错过（通知来时它还在列表里、或者没收到），等的时候每 5 秒也查一次
    private func waitForOldToQuit() {
        guard oldWatch == nil else { return }
        oldWatch = NSWorkspace.shared.notificationCenter.addObserver(forName: NSWorkspace.didTerminateApplicationNotification,
                                                                     object: nil, queue: .main) { [weak self] note in
            guard let self, let app = note.userInfo?[NSWorkspace.applicationUserInfoKey] as? NSRunningApplication,
                  app.bundleIdentifier == Self.oldBundleID else { return }
            self.oldQuit()
        }
        pollOld()
    }

    private func pollOld() {
        DispatchQueue.main.asyncAfter(deadline: .now() + 5) { [weak self] in
            guard let self, self.oldWatch != nil else { return }      // 已经接上了
            if Self.oldAgentRunning() { self.pollOld() } else { self.oldQuit() }
        }
    }

    private func oldQuit() {
        guard let t = oldWatch else { return }
        NSWorkspace.shared.notificationCenter.removeObserver(t)
        oldWatch = nil
        DispatchQueue.main.asyncAfter(deadline: .now() + 1) { [weak self] in self?.start() }
    }

    /// 本体退出：只停自己起的这个进程（SIGTERM，3 秒不退再 SIGKILL）
    func stop() {
        quitting = true
        host?.stop()
        guard let p = process, p.isRunning else { return }
        p.terminate()
        let deadline = Date().addingTimeInterval(3)
        while p.isRunning && Date() < deadline { usleep(50_000) }
        if p.isRunning { kill(p.processIdentifier, SIGKILL) }
    }

    private static func log() -> FileHandle {
        let dir = AppInfo.envDir.appendingPathComponent("logs")
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        let path = dir.appendingPathComponent("agent.log").path
        if !FileManager.default.fileExists(atPath: path) {
            FileManager.default.createFile(atPath: path, contents: nil, attributes: [.posixPermissions: 0o600])
        }
        guard let h = FileHandle(forWritingAtPath: path) else { return FileHandle.nullDevice }
        h.seekToEndOfFile()
        return h
    }
}
