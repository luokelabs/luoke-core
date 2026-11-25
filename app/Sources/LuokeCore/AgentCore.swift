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
import ServiceManagement
import UserNotifications
#endif

/// 主窗口左下角那一行（和 logo 共存，有事才出现）要说的事：主控这边按轻重排好，只给最要紧的一件（luoke-agent 的
/// `WindowModel.sidebarNotice`，agent-private#126）。连不上 agent 核心、还在准备这两种 agent 自己判断，排在这些前面。
/// 短话四个字以内（那里只有一百来点宽，「模型在下载」五个字就截断了）
struct CoreNotice: Equatable {
    enum Kind: Equatable { case error, paused, downloading, update }
    /// 点了做什么：设置 · 通用 / 系统设置的「电池」/ 只是知道了（自己会好的，没什么要改）/ 重新启动螺壳 / 检查更新
    enum Action: Equatable { case settings, batterySettings, dismiss, restart, update }
    let kind: Kind
    let text: String
    /// 点开以后的一两句话：为什么、在哪改（11-12 定；agent-private#131）
    var detail: String? = nil
    /// 按钮上的字；nil 用主窗口默认的「去设置里改」
    var actionLabel: String? = nil
    var action: Action = .settings

    /// 出错了 > 已暂停 > 下载中 > 有新版本
    static func top(status: HubStatus?, unreachable: Bool, updateFound: Bool) -> CoreNotice? {
        if unreachable || status?.state == .error {
            return CoreNotice(kind: .error, text: "出错了", detail: "螺壳的模型没在运行，生成、整理这些先做不了。重新启动螺壳一般就好了。",
                              actionLabel: "重新启动螺壳", action: .restart)
        }
        if let st = status {
            if st.backgroundState != "running" { return paused(st.backgroundReasons) }
            if st.downloading { return CoreNotice(kind: .downloading, text: "下载中") }
        }
        if updateFound { return CoreNotice(kind: .update, text: "有新版本", action: .update) }
        return nil
    }

    /// 后台为什么暂停（主控 /status 的 background.reasons），几个原因同时在时说最该先处理的那个
    static func paused(_ reasons: [String]) -> CoreNotice {
        if reasons.contains("battery_no_large_model") {
            return CoreNotice(kind: .paused, text: "已暂停",
                              detail: "在用电池，按设置不运行大模型：后台的整理和要用大模型的事先不做，接上电源就接着做。想在电池上也用，在「设置 · 通用」的「用电池时」里改。")
        }
        if reasons.contains("battery") {
            return CoreNotice(kind: .paused, text: "已暂停",
                              detail: "正在用电池，后台的整理先不做，免得费电；接上电源就接着做。想在电池上也做，在「设置 · 通用」的「用电池时」里选「照常运行」。")
        }
        if reasons.contains("low_power") {
            return CoreNotice(kind: .paused, text: "已暂停", detail: "这台 Mac 开着低电量模式，后台的整理先不做；关掉低电量模式就接着做。想在低电量模式下也做，在「设置 · 通用」里关掉「低电量模式时暂停」。",
                              actionLabel: "打开电池设置", action: .batterySettings)
        }
        if reasons.contains("thermal") {
            return CoreNotice(kind: .paused, text: "已暂停", detail: "这台 Mac 有点热，后台的整理先歇一会儿，凉下来会自己接着做。",
                              actionLabel: "知道了", action: .dismiss)
        }
        if reasons.contains("memory") {
            return CoreNotice(kind: .paused, text: "已暂停", detail: "内存有点紧，后台的整理先等等，别的程序腾出内存后会自己接着做。",
                              actionLabel: "知道了", action: .dismiss)
        }
        return CoreNotice(kind: .paused, text: "已暂停", detail: "后台的整理暂时停着，条件好了会自己接着做。", actionLabel: "知道了", action: .dismiss)
    }
}

/// 右边栏的任务队列和生成量（11-12 和 luoke-agent 商定）：主控 /status 的 queue、output 换成给人看的样子，
/// AgentCore 再填进 luoke-agent 的 TaskRow、OutputStats（这里不依赖 LuokeWindow，好测）
struct RailTask: Equatable {
    enum Wait: Equatable { case soon, time(Date), power, thermal, memory, paused, quiet, busy }
    enum State: Equatable { case running(fraction: Double?), queued(Wait?) }
    let id: String
    let tool: String?          // 应用编号（luoke-mail……）；本机接口是 nil
    let title: String
    let state: State

    /// 名字不带应用名：右边栏每行下面已经写了是哪个应用（11-12 截图里「螺壳邮件助手的后台整理」重复又被截断）
    static func rows(_ st: HubStatus?) -> [RailTask] {
        guard let st else { return [] }
        var out: [RailTask] = []
        for r in st.queueRunning {
            let tool = r["tool"] as? String
            let title: String
            switch r["kind"] as? String {
            case "endpoint": title = "本机接口 · " + ModelName.display(r["name"] as? String ?? "")
            case "job": title = (r["title"] as? String) ?? "后台任务"
            default: title = "在用模型"
            }
            out.append(RailTask(id: r["id"] as? String ?? UUID().uuidString, tool: tool, title: title,
                                state: .running(fraction: (r["fraction"] as? NSNumber)?.doubleValue)))
        }
        for q in st.queueWaiting {
            let tool = q["tool"] as? String
            let w = q["wait"] as? [String: Any]
            let wait: Wait?
            switch w?["kind"] as? String {
            case "soon": wait = .soon
            case "time": wait = (w?["at"] as? NSNumber).map { .time(Date(timeIntervalSince1970: $0.doubleValue)) }
            case "power": wait = .power
            case "thermal": wait = .thermal
            case "memory": wait = .memory
            case "paused": wait = .paused
            case "quiet": wait = .quiet
            case "busy": wait = .busy
            default: wait = nil
            }
            out.append(RailTask(id: q["id"] as? String ?? UUID().uuidString, tool: tool,
                                title: (q["title"] as? String) ?? "后台任务", state: .queued(wait)))
        }
        return out
    }

    /// 「11 月 12 日」；不是今年的带上年份。主控给的是 YYYY-MM-DD，认不出就原样
    static func since(_ ymd: String?, now: Date = Date()) -> String? {
        guard let ymd else { return nil }
        let p = ymd.split(separator: "-").compactMap { Int($0) }
        guard p.count == 3 else { return ymd }
        let year = Calendar.current.component(.year, from: now)
        return (p[0] == year ? "" : "\(p[0]) 年 ") + "\(p[1]) 月 \(p[2]) 日"
    }
}

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

    /// 应用的某一页（luoke://app 链接）：page 为 nil 是它的第一页，args 原样交给那一页
    static func openToolPage(app: String, page: String?, args: [String: String]) {
        presenter?.openToolPage(app: app, page: page, args: args)
    }

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
        // 「应用管理」页由主窗口照 appsSnapshot() 画：行、正在做的、做完那一句、能换回的上一版，变了就让它重读
        Publishers.MergeMany(m.$rows.map { _ in () }.eraseToAnyPublisher(), m.$phase.map { _ in () }.eraseToAnyPublisher(),
                             m.$progress.map { _ in () }.eraseToAnyPublisher(), m.$fraction.map { _ in () }.eraseToAnyPublisher(),
                             m.$previous.map { _ in () }.eraseToAnyPublisher(), m.$imeAdded.map { _ in () }.eraseToAnyPublisher())
            .dropFirst(6).receive(on: RunLoop.main)
            .sink { [weak self] in MainActor.assumeIsolated { self?.model.hostAppsChanged() } }.store(in: &subs)
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
        // 进「应用管理」页：本体的应用目录重新列一遍（页面是窗口画的，没有本体的 onAppear 了）
        model.$page.dropFirst().filter { $0 == .apps }.receive(on: RunLoop.main)
            .sink { [weak self] _ in MainActor.assumeIsolated { self?.apps.load() } }.store(in: &subs)
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
        // 左下角那一行：出错、暂停、下载、有新版本，只给最要紧的一件。点「有新版本」开检查更新那一页，别的去「设置 · 通用」
        let hub = HubModel.shared
        hub.$status.combineLatest(hub.$hubError, hub.$hubExit, hub.$updateFound).receive(on: RunLoop.main).sink { [weak self] st, _, _, found in
            MainActor.assumeIsolated { self?.showNotice(CoreNotice.top(status: st, unreachable: hub.unreachable, updateFound: found != nil)) }
        }.store(in: &subs)
        // 右边栏：任务队列和生成了多少 token（RailTask 换好，这里只套 LuokeWindow 的类型）；没变不重设
        hub.$status.receive(on: RunLoop.main).sink { [weak self] st in
            MainActor.assumeIsolated { self?.showRail(st) }
        }.store(in: &subs)
        // 「设置」由主窗口照 settingsSnapshot() 画：主控状态、更新、换档前挡下的那一句、计费网络变了就重算；进「设置」时把本机接口、权限读一遍
        Publishers.MergeMany(hub.$status.map { _ in () }.eraseToAnyPublisher(), hub.$updateFound.map { _ in () }.eraseToAnyPublisher(),
                             hub.$tierError.map { _ in () }.eraseToAnyPublisher(), hub.$meteredNetwork.map { _ in () }.eraseToAnyPublisher())
            .receive(on: RunLoop.main)
            .sink { [weak self] in MainActor.assumeIsolated { self?.settingsChanged() } }.store(in: &subs)
        model.$page.filter { $0 == .prefs }.receive(on: RunLoop.main)
            .sink { [weak self] _ in MainActor.assumeIsolated { self?.enterSettings() } }.store(in: &subs)
        // 有没有急事：菜单栏图标的小点
        model.$urgent.receive(on: RunLoop.main).sink { _ in MainActor.assumeIsolated { MenuBar.shared.refreshIcon() } }.store(in: &subs)
        refreshApps()
        reportPermissions()
    }

    private func showRail(_ st: HubStatus?) {
        let (tasks, stats) = Self.rail(st)
        if model.tasks != tasks { model.tasks = tasks }
        if model.outputStats != stats { model.outputStats = stats }
    }

    /// 主控的 /status → 右边栏的任务行和生成量（截图工具也用）
    static func rail(_ st: HubStatus?) -> ([TaskRow], OutputStats?) {
        let tasks = RailTask.rows(st).map { t -> TaskRow in
            let tool = toolIcon(t.tool)
            let state: TaskRow.State
            switch t.state {
            case .running(let f): state = .running(fraction: f, note: nil)
            case .queued(let w):
                let wait: TaskRow.Wait?
                switch w {
                case .soon?: wait = .soon
                case .time(let d)?: wait = .time(d)
                case .power?: wait = .power
                case .thermal?: wait = .thermal
                case .memory?: wait = .memory
                case .paused?: wait = .paused
                case .quiet?: wait = .quiet
                case .busy?: wait = .busy
                case nil: wait = nil
                }
                state = .queued(wait: wait)
            }
            return TaskRow(id: t.id, tool: tool, title: t.title, state: state)
        }
        let stats = st.map { OutputStats(tasksToday: nil, tokensToday: $0.tokensToday, tokensTotal: $0.tokensTotal,
                                          since: RailTask.since($0.tokensSince)) }
        return (tasks, stats)
    }

    /// 左下角那一行（CoreNotice）交给主窗口；没变就不重画
    private func showNotice(_ n: CoreNotice?) {
        guard let n else {
            if model.sidebarNotice != nil { model.sidebarNotice = nil }
            return
        }
        let kind: SidebarNotice.Kind
        switch n.kind {
        case .error: kind = .error
        case .paused: kind = .paused
        case .downloading: kind = .downloading
        case .update: kind = .update
        }
        // 暂停的原因换了（拔电源 → 过热）说的话也跟着换：比到 detail
        if let cur = model.sidebarNotice, cur.kind == kind, cur.text == n.text, cur.detail == n.detail { return }
        var open: (@MainActor () -> Void)? = nil      // nil：去「设置 · 通用」
        switch n.action {
        case .settings: open = nil
        case .update: open = { Updater.show() }
        case .restart: open = { HubModel.shared.restartHub() }
        case .dismiss: open = {}                       // 自己会好的：按钮只是收起这段话
        case .batterySettings:
            open = { if let u = URL(string: "x-apple.systempreferences:com.apple.Battery-Settings.extension") { NSWorkspace.shared.open(u) } }
        }
        model.sidebarNotice = SidebarNotice(id: "core.\(n.kind)", kind: kind, text: n.text, detail: n.detail, actionLabel: n.actionLabel, open: open)
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
        // 标题栏归 LuokeWindow：隐藏标题、透明，加一个空的统一工具栏，红黄绿在 52 高的顶行正中，带字的 logo 在它右边（agent-private#120）
        MainWindowChrome.apply(to: w)
        w.contentView = NSHostingView(rootView: MainWindowView(model: model))
        w.isReleasedWhenClosed = false
        w.delegate = self
        w.center()
        w.setFrameAutosaveName("LuokeMainWindow")
        MainWindowHost.enforce(on: w)
        window = w
        return w
    }

    /// 最小 860×600 在这里卡（MainWindowHost：contentMinSize 会被 NSHostingView 改写）
    func windowWillResize(_ sender: NSWindow, to frameSize: NSSize) -> NSSize { MainWindowHost.clamp(frameSize, in: sender) }

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
                    self?.notificationAuth = (status, alerts)
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

    /// 打开主窗口到「设置」：菜单栏的「设置」、⌘, 经 SettingsWindow.show 到这里，tab 为 nil 停在上次看的标签；卸载页的「取消」「完成」回「通用」
    func showSettings(tab: PrefsTab? = nil) {
        show()
        model.go(.prefs, tab: tab?.rawValue)
    }

    func openModelSettings() { showSettings(tab: .model) }     // 窗口只在没有 settingsSnapshot 时调；留着不另开窗口

    func askPermission(app: String, id: String) { PermissionWindow.ask(app: app, id: id) }
    func askPermission(app: String, id: String, ready: @escaping @MainActor () -> Void) {
        PermissionWindow.ask(app: app, id: id) { MainActor.assumeIsolated { ready() } }
    }
    func askSystem(id: String) { PermissionWindow.askSystem(id: id) }

    func askNotifications() {
        let center = UNUserNotificationCenter.current()
        center.getNotificationSettings { before in
            if before.authorizationStatus == .denied {      // 以前关掉了：系统不会再问，直接去系统设置里打开
                DispatchQueue.main.async { MainActor.assumeIsolated { Self.openNotificationSettings() } }
                return
            }
            center.requestAuthorization(options: [.alert, .sound]) { [weak self] _, _ in
                center.getNotificationSettings { after in
                    DispatchQueue.main.async {
                        MainActor.assumeIsolated {
                            // 系统没弹询问（还是「没决定」：这份构建不让问，例如 ad-hoc 签名的开发构建）：点了不能什么都不发生
                            // （负责人 11-14：「打开通知」点了没用），打开系统设置里螺壳的通知那一页
                            if after.authorizationStatus == .notDetermined { Self.openNotificationSettings() }
                            self?.reportNotifications()
                        }
                    }
                }
            }
        }
    }

    /// 系统设置里螺壳自己的通知那一页
    static func openNotificationSettings() {
        let id = Bundle.main.bundleIdentifier ?? "ai.luoke.core"
        if let u = URL(string: "x-apple.systempreferences:com.apple.Notifications-Settings.extension?id=\(id)") { NSWorkspace.shared.open(u) }
    }

    func setPermission(app: String, id: String, on: Bool) {
        if !on { PermissionWindow.withdrawn(app: app, id: id) }
        BundledApps.setPermission(app, id, on: on) { [weak self] why in
            if let why { self?.model.hostNotice = why }
            self?.reportPermissions()
        }
    }
    func openPrivacySettings() { showSettings(tab: .privacy) } // 同上
    /// 打开主窗口到「添加应用」页（菜单栏的「应用」、「去升级应用」、卸载完回来都经 AppsWindow.show 到这里）
    func showApps() -> AppsModel {
        let showing = window != nil && model.page == .apps  // 已经在这一页（窗口关了再开也算）：页面不会再出现一次，在这里 load
        apps.resetDone()
        show()
        model.go(.apps)
        if showing { apps.load() }                          // 否则页面出现时 load（AppsView 的 onAppear），一次进页面只 load 一遍
        return apps
    }

    func openAddApps() { _ = showApps() }       // 窗口只在没有 appsSnapshot 时调；留着不另开窗口

    /// 「应用管理」页的数据（docs/界面接口.md §16）：窗口照它画，动作回到下面几个方法
    func appsSnapshot() -> HostApps? {
        let rows = AppsSnapshot.entries(apps).map(Self.hostRow)
        let notice = AppsSnapshot.notice(phase: apps.phase, imeAdded: apps.imeAdded, rows: apps.rows).map { n in
            HostAppsNotice(text: n.text, ok: n.ok, action: n.keyboardSettingsFor.map { ("打开键盘设置", $0, HostAppAction.openKeyboardSettings) })
        }
        return HostApps(rows: rows, notice: notice)
    }

    static func hostRow(_ e: AppEntry) -> HostAppRow {
        let state: HostAppRow.State
        switch e.state {
        case .installed: state = .installed
        case .disabled: state = .disabled
        case .installable: state = .installable
        case .update(let v): state = .update(to: v)
        case .unavailable(let why): state = .unavailable(reason: why)
        case .working(let label, let f): state = .working(label: label, progress: f)
        }
        let actions: [HostAppAction] = e.actions.map {
            switch $0 {
            case .enable: return .enable
            case .disable: return .disable
            case .restart: return .restart
            case .deleteData: return .deleteData
            case .uninstall: return .uninstall
            case .rollback(let v): return .rollback(to: v)
            case .openKeyboardSettings: return .openKeyboardSettings
            }
        }
        return HostAppRow(id: e.id, tool: toolIcon(e.id), name: e.name, tagline: e.tagline, version: e.version,
                          origin: e.poweredBy ? .poweredBy : .luoke, state: state, bundled: e.bundled, facts: e.facts, actions: actions)
    }

    static func toolIcon(_ accessory: String?) -> ToolIcon {
        switch accessory {
        case "luoke-mail": return .mail
        case "luoke-scan": return .scan
        case "luoke-mem": return .mem
        case "luoke-search": return .search
        case "luoke-ime": return .ime
        default: return .core
        }
    }

    private func appRow(_ id: String) -> AppsModel.Row? {
        if apps.rows.isEmpty { apps.load() }
        return apps.rows.first { $0.id == id }
    }

    /// 侧栏、应用管理页上的「升级」：就在这一行里升。螺壳自己（设置 · 通用的「升级」）走检查更新那一页：下载、核对、输密码
    func upgrade(app: String) {
        if app == ToolIcon.core.accessory { Updater.show(); return }
        guard let r = appRow(app), r.canUpdate, !apps.busy else { apps.load(); return }
        apps.update(r)
    }

    func install(app: String) {
        guard let r = appRow(app), r.installed == nil, r.unavailable == nil, !apps.busy else { return }
        apps.install(r)
    }

    /// 卸载走本体自己的卸载页（列出要删的、勾选、确认），做完回「应用管理」
    func uninstall(app: String) {
        Uninstall.show(preselect: app, back: { [weak self] in _ = self?.showApps() })
    }

    func perform(app: String, action: HostAppAction) {
        if action == .uninstall { uninstall(app: app); return }
        if action == .openKeyboardSettings { FirstRunModel(preselect: nil).openKeyboardSettings(); return }
        guard let r = appRow(app), !apps.busy else { return }
        switch action {
        case .enable: apps.enable(r)
        case .disable: apps.disable(r)
        case .restart: apps.restartInputMethod(r)
        case .rollback: apps.rollback(r)
        case .deleteData: DeleteData.show(accessory: r.id, name: r.name)
        case .uninstall, .openKeyboardSettings: break
        }
    }

    // MARK: 设置（docs/界面接口.md §16）：通用、模型、隐私与权限里本体管的那几块，窗口照 settingsSnapshot() 画

    /// 窗口每次重画都来读：存着，主控的状态、更新、本机接口、登录项变了才重算
    private var settingsCache: HostSettings?
    /// 本机接口（GET /endpoint）：进「设置」时读一次，开关、换密钥以后再读
    private var endpointInfo: [String: Any] = [:]
    private var endpointError: String?
    private var loginError: String?
    private var notificationAuth: (status: String, alerts: Bool)?
    private var checkingUpdates = false

    /// 算设置的东西变了：在「设置」页上就让窗口重读，不在就等进来时再算
    private func settingsChanged() {
        settingsCache = nil
        if model.page == .prefs { model.hostSettingsChanged() }
    }

    /// 进「设置」：本机接口、通知授权这些不随主控状态推过来的，重新读一遍
    private func enterSettings() {
        endpoint(nil)
        reportNotifications()
        settingsChanged()
    }

    func settingsSnapshot() -> HostSettings? {
        if let s = settingsCache { return s }
        let s = buildSettings()
        settingsCache = s
        return s
    }

    private func buildSettings() -> HostSettings {
        let hub = HubModel.shared
        let st = hub.status
        let login: HostLoginItem
        if let e = loginError {
            login = .error("没设置上：" + e)
        } else {
            switch SMAppService.mainApp.status {
            case .enabled: login = .on
            case .requiresApproval: login = .requiresApproval
            default: login = .off
            }
        }
        let release = st?.release ?? Updater.myRelease
        let last = UserDefaults.standard.double(forKey: Updater.lastKey)
        let general = HostGeneral(loginItem: login, currentVersion: release.isEmpty ? "开发版" : SettingsSnapshot.versionText(release),
                                  latestVersion: hub.updateFound?.tag, lastChecked: last > 0 ? Date(timeIntervalSince1970: last) : nil,
                                  powerMode: HostPowerMode(rawValue: st?.powerMode ?? "") ?? .balanced,
                                  batteryMode: HostBatteryMode(rawValue: st?.batteryMode ?? "") ?? .light,
                                  autoCheck: Updater.autoEnabled, pauseOnLowPower: st?.pauseOnLowPower)

        let recipe = Manifests.recipe() ?? [:]
        let choices = Manifests.tierChoices(memGB: Diagnostics.memGB())
        let tiers = st.map { st in
            SettingsSnapshot.tiers(choices: choices, recipe: recipe, current: st.tier, pending: st.pendingTier, downloads: st.downloads,
                                   needed: st.neededDownloads, tierError: hub.tierError.map { (hub.tierErrorTier ?? "", $0) },
                                   installed: Manifests.modelInstalled,
                                   sizeGB: { n in
                                       let b = Manifests.itemSize(n)
                                       return b > 0 ? Double(b) / 1e9 : SettingsSnapshot.sizeGB(recipe, n)
                                   })
        } ?? []
        let catalog = SettingsSnapshot.catalog(choices: choices, recipe: recipe)
        let e = endpointInfo
        let api = HostLocalAPI(enabled: e["enabled"] as? Bool ?? false, address: "127.0.0.1",
                               port: e["port"] as? Int ?? e["want_port"] as? Int ?? 0, models: e["models"] as? [String] ?? [],
                               hasKey: !(e["key"] as? String ?? "").isEmpty, error: endpointError)
        let models = HostModels(tiers: tiers.map(Self.hostTier), catalog: catalog.map(Self.hostModelRow), metered: hub.meteredNetwork, advanced: api)
        let privacy = HostPrivacy(dataLine: "模型在这台 Mac 上跑，数据只存在这台 Mac 上，没有遥测。", systemChecks: systemChecks())
        return HostSettings(general: general, models: models, privacy: privacy)
    }

    static func hostTier(_ t: TierEntry) -> HostTier {
        let state: HostTier.State
        switch t.state {
        case .inUse: state = .inUse
        case .switching(let p): state = .switching(progress: p)
        case .available(let need): state = .available(needGB: need)
        case .failed(let why): state = .failed(reason: why)
        }
        // title 不填：名字由主窗口照 kind 写（ADR 0028）
        return HostTier(id: t.id, title: "", model: t.model, sizeGB: t.sizeGB, state: state, kind: t.kind == .full ? .full : .lighter,
                        current: t.current, canRetry: t.canRetry)
    }

    static func hostModelRow(_ c: CatalogEntry) -> HostModelRow {
        // purpose 不填：用途由主窗口照 base、accessories 写（ADR 0028）
        HostModelRow(name: c.name, purpose: "", params: c.params, quant: c.quant, sizeGB: c.sizeGB,
                     base: c.base.map { $0 == .full ? .full : .lighter }, accessories: c.accessories)
    }

    /// 系统权限检查：通知（主动提醒要用），和开着的应用声明过的、螺壳名下的系统权限（查不出的不列）
    private func systemChecks() -> [HostSystemCheck] {
        var out: [HostSystemCheck] = []
        if let n = notificationAuth {
            let ok = ["authorized", "provisional", "ephemeral"].contains(n.status) && n.alerts
            out.append(HostSystemCheck(key: HostSystemCheck.notifications, name: "通知", ok: ok, detail: ok ? nil : "主动提醒要用"))
        }
        let states = HostPermissions.current()
        var users: [String: [String]] = [:]
        for b in BundledApps.all where b.enabled {
            for id in PermissionWindow.declared(app: b.accessory) { users[id, default: []].append(b.manifest?.name ?? b.accessory) }
        }
        for (id, apps) in users.sorted(by: { $0.key < $1.key }) {
            guard let s = states[id] else { continue }
            let name = id == "fda" ? "完全磁盘访问" : (id == "photos" ? "照片" : id)
            // key 和应用清单里权限的 id 同一套：主窗口按它和螺壳级权限去重
            out.append(HostSystemCheck(key: id, name: name, ok: s == "granted", detail: apps.joined(separator: "、") + "要用"))
        }
        return out
    }

    func apply(_ change: HostSettingChange) {
        let hub = HubModel.shared
        switch change {
        case .loginItem(let on):
            loginError = nil
            do { if on { try LoginItem.register() } else { try LoginItem.unregister() } }
            catch { loginError = error.localizedDescription; NSLog("登录项：%@", error.localizedDescription) }
            settingsChanged()
        case .powerMode(let m): hub.setPowerMode(m.rawValue)
        case .batteryMode(let m): hub.setBatteryMode(m.rawValue)
        case .pauseOnLowPower(let on): hub.setPauseOnLowPower(on)
        case .tier(let t): hub.setTier(t)
        case .localAPI(let on): endpoint { _ = try $0.request("PUT", "/settings", body: ["endpoint_enabled": on]) }
        case .autoCheckUpdates(let on):        // 设置 · 通用「每天自动检查更新」（luoke-agent 11-16）
            Updater.autoEnabled = on
            settingsChanged()
        }
    }

    /// 本机接口：做一件事（可以没有），再把 /endpoint 读回来；做成了说一句 said
    private func endpoint(_ work: ((HubClient) throws -> Void)?, said: String? = nil) {
        let client = HubModel.shared.client
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            var err: String?
            do { try work?(client) } catch { err = (error as? HubError)?.message ?? error.localizedDescription }
            let fresh = try? client.request("GET", "/endpoint").1
            DispatchQueue.main.async {
                MainActor.assumeIsolated {
                    guard let self else { return }
                    if let fresh { self.endpointInfo = fresh }
                    if work != nil { self.endpointError = err }
                    if err == nil, let said { self.model.show(said) }
                    self.settingsChanged()
                }
            }
        }
    }

    func copyLocalAPIKey() {
        guard let key = endpointInfo["key"] as? String, !key.isEmpty else { endpoint(nil); return }
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(key, forType: .string)
        model.show("密钥已复制")
    }

    func rotateLocalAPIKey() {
        endpoint({ _ = try $0.request("POST", "/endpoint/key") }, said: "换了新密钥，用旧密钥的程序要重新填")
    }

    func retryDownloads() { HubModel.shared.retryDownloads() }

    /// 「检查更新」：就在这一行里查，查完写「有新版本 …」或「已是最新 · 今天 … 检查过」；升级再走检查更新那一页。
    /// 本机版、开发版不从网上查：打开那一页说清楚
    func checkForUpdates() {
        switch Updater.channel {
        case .local, .none: Updater.show(); return
        case .internalBuild, .beta: break
        }
        guard !checkingUpdates else { return }
        checkingUpdates = true
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            let r = Result { try Updater.check() }
            DispatchQueue.main.async {
                MainActor.assumeIsolated {
                    guard let self else { return }
                    self.checkingUpdates = false
                    switch r {
                    case .success(let f):
                        UserDefaults.standard.set(Date().timeIntervalSince1970, forKey: Updater.lastKey)
                        HubModel.shared.updateFound = f
                        self.model.show(f.map { "有新版本 \($0.tag)" } ?? "已是最新")
                    case .failure(Updater.Failure.needsMacOS(let tag, let v)):
                        UserDefaults.standard.set(Date().timeIntervalSince1970, forKey: Updater.lastKey)
                        HubModel.shared.updateFound = nil
                        self.model.show(Updater.Failure.needsMacOS(tag: tag, macOS: v).localizedDescription)
                    case .failure(let e):
                        self.model.show("没查成：" + e.localizedDescription)
                    }
                    self.settingsChanged()
                }
            }
        }
    }

    func exportDiagnostics() { Diagnostics.export(model: HubModel.shared) }

    /// 卸载螺壳走本体自己的卸载页（列出要删的、勾选、确认），取消回「设置 · 通用」
    func uninstallLuoke() {
        Uninstall.show(preselect: UninstallModel.all, back: { [weak self] in self?.showSettings(tab: .general) })
    }

    func openMemory() { Memory.open() }

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

    /// 用户在一个应用的页上点了（窗口交给 agent 之前）：只有搜索的页上的这一下，才让「添加」弹选文件夹的框
    func appPageTapped(app: String, page: String) {
        if app == SearchHelper.accessory { SearchFolderPick.searchTapped() }
    }

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
    /// 数据目录：~/.luoke/agent（原来「螺壳 agent.app」用的就是这里，接着用；开发版、开发实例也用它）；
    /// 普通的实验室构建用自己环境目录下的 agent（AppInfo.usesRealData）
    static var home: URL {
        AppInfo.usesRealData ? luokeHome.appendingPathComponent("agent", isDirectory: true)
                             : AppInfo.envDir.appendingPathComponent("agent", isDirectory: true)
    }
    /// 记忆文件，和 Memory.open 的规矩一样：普通的实验室构建没给 LUOKE_HOME 就不读本机的（给一个不存在的文件，核心先不读记忆）
    static var memory: URL {
        if !AppInfo.usesRealData && ProcessInfo.processInfo.environment["LUOKE_HOME"] == nil { return home.appendingPathComponent("mem.db") }
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
    /// 连着几次撞上数据目录锁（退出码 3）：先每 3 秒看一次（多半是刚停掉的螺壳的核心还没退完），10 次以后改成每分钟
    private var lockedTries = 0
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
        case 3:             // 这个数据目录已经有一个核心在跑：不抢它的。刚停掉的螺壳的核心要几秒才退完，先勤看几次，再隔一分钟看
            if Date().timeIntervalSince(startedAt) >= 60 { lockedTries = 0 }     // 稳定跑过一阵以后才撞上的：重新从勤看开始
            lockedTries += 1
            onProblem?(lockedTries <= 10 ? "正在接上螺壳的核心…"
                                         : "另一个螺壳的核心在用这份数据（多半是另一个螺壳还开着）：退出它以后，这里就接上了。")
            retry(after: lockedTries <= 10 ? 3 : 60)
        default:
            lockedTries = 0
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
