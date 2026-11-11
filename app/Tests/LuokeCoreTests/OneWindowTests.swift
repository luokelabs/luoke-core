import AppKit
import SwiftUI
import XCTest
@testable import LuokeCore
#if canImport(LuokeWindow)
import LuokeWindow
#endif

/// 螺壳只有一个窗口（11-10）：带主窗口的这一份，本体的界面都画在主窗口里，不另开窗口、不弹框。
/// 剩下的 NSWindow、NSAlert、runModal 要么在同一个函数里先交给主窗口（交出去了就 return），要么是旁边写明了的例外
/// （主窗口本身、安装器那几分钟、不是装好的那份螺壳）。
final class OneWindowSourceTests: XCTestCase {
    private static let sources = URL(fileURLWithPath: #filePath)
        .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
        .appendingPathComponent("Sources/LuokeCore")

    /// 先交给主窗口的写法：交出去了就不往下开窗口
    private static let routes = ["MainWindow.present(", "MainWindow.presenter", "MainWindow.showApps()", "MainWindow.showSettings()",
                                 "MainWindow.sheetParent()", "UpgradeNotDone.show("]
    /// 整个文件都在没有主窗口的时候跑：安装器（螺壳还没装好、正在换包）
    private static let installerOnly: Set<String> = ["Installer.swift"]
    private static let opensWindow = try! NSRegularExpression(pattern: #"NSWindow\(|NSPanel\(|NSAlert\(|\.runModal\("#)
    private static let funcStart = try! NSRegularExpression(
        pattern: #"^\s*(?:@\w+\s+)*(?:(?:private|fileprivate|public|internal|static|override|final|mutating|nonisolated|class)\s+)*func\s"#)

    private func matches(_ re: NSRegularExpression, _ s: String) -> Bool {
        re.firstMatch(in: s, range: NSRange(s.startIndex..., in: s)) != nil
    }

    func testEveryWindowGoesThroughTheMainWindowFirst() throws {
        let files = try FileManager.default.contentsOfDirectory(at: Self.sources, includingPropertiesForKeys: nil).filter { $0.pathExtension == "swift" }
        XCTAssertGreaterThan(files.count, 20, "找不到本体的源码：\(Self.sources.path)")
        var seen = 0
        for file in files where !Self.installerOnly.contains(file.lastPathComponent) {
            let lines = try String(contentsOf: file, encoding: .utf8).components(separatedBy: "\n")
            for (i, line) in lines.enumerated() where matches(Self.opensWindow, line) {
                let code = line.components(separatedBy: "//").first ?? line
                guard matches(Self.opensWindow, code) else { continue }      // 只在注释里提到
                seen += 1
                // 写明了的例外：这一行或上面六行里有「一个窗口的例外」（弹框的 NSAlert 和它的 runModal 隔几行）
                if lines[max(0, i - 6)...i].contains(where: { $0.contains("一个窗口的例外") }) { continue }
                // 同一个函数里，前面先交给了主窗口
                var j = i
                while j > 0 && !matches(Self.funcStart, lines[j]) { j -= 1 }
                let before = lines[j..<i].joined(separator: "\n")
                XCTAssertTrue(Self.routes.contains { before.contains($0) },
                              "\(file.lastPathComponent):\(i + 1) 开窗口或弹框之前没先交给主窗口：\(line.trimmingCharacters(in: .whitespaces))")
            }
        }
        XCTAssertGreaterThan(seen, 10, "一处开窗口的都没找到：查法坏了")
    }
}

/// 假的主窗口：记下交过来的界面
final class FakePresenter: HostPresenter {
    var presented: [(id: String, style: HostStyle, title: String)] = []
    var views: [AnyView] = []
    var dismissed: [String] = []
    var shows = 0
    var toolPages: [(app: String, page: String?, args: [String: String])] = []
    var locks: [(id: String, locked: Bool)] = []
    /// 有一块正忙着、走不开：交过来的都不接
    var busy = false
    private var current: [String: HostScreen] = [:]

    func present(_ s: HostScreen) -> Bool {
        guard !busy else { shows += 1; return false }
        presented.append((s.id, s.style, s.title)); views.append(s.view); current[s.id] = s
        return true
    }
    func lockClose(_ id: String, _ locked: Bool) { locks.append((id, locked)) }
    func dismiss(_ id: String) { dismissed.append(id); current[id] = nil }
    func showing(_ id: String) -> Bool { current[id] != nil }
    /// 用户离开了这一块（点了侧栏）
    func leave(_ id: String) { current.removeValue(forKey: id)?.closed?() }
    func endSetup(_ id: String, keepOpen: Bool) { dismiss(id) }
    func show() { shows += 1 }
    func showToday() {}
    func sheetParent() -> NSWindow? { nil }
    func openToolPage(app: String, page: String?, args: [String: String]) { toolPages.append((app, page, args)) }
}

@MainActor
final class OneWindowRoutingTests: XCTestCase {
    private var fake = FakePresenter()

    override func setUp() {
        fake = FakePresenter()
        MainWindow.presenterOverride = fake
    }

    override func tearDown() {
        MainWindow.presenterOverride = nil
    }

    func testReopenWithoutTheMenuBarIconOpensTheMainWindow() {
        HomeWindow.show()
        XCTAssertEqual(fake.shows, 1)
        XCTAssertNil(HomeWindow.window, "不该另开螺壳的小窗口")
    }

    func testReleaseNotesAreAPageInTheMainWindow() {
        ReleaseNotesWindow.show(tag: "v0.0.0-test")
        XCTAssertEqual(fake.presented.last?.id, ReleaseNotesWindow.hostID)
        XCTAssertEqual(fake.presented.last?.style, .page)
        XCTAssertNil(ReleaseNotesWindow.window)
        ReleaseNotesWindow.close()
        XCTAssertEqual(fake.dismissed, [ReleaseNotesWindow.hostID])
    }

    func testUpgradeNotDoneIsACardNotAnAlert() {
        XCTAssertTrue(UpgradeNotDone.show("原来的螺壳照常能用。"))
        XCTAssertEqual(fake.presented.last?.id, UpgradeNotDone.hostID)
        XCTAssertEqual(fake.presented.last?.style, .card)
    }

    /// 记忆在主窗口的「记忆」页里打开，不起浏览器的那个界面
    func testMemoryOpensItsPageInTheMainWindow() {
        Memory.open()
        Memory.open(note: "12")
        Memory.open(view: "recent")
        XCTAssertTrue(Memory.handle(URL(string: "luoke://memory?note=7")!))
        XCTAssertTrue(Memory.handle(URL(string: "luoke://memory?view=people")!))
        XCTAssertTrue(Memory.handle(URL(string: "luoke://memory?view=nope")!))          // 不认的 view：首页
        XCTAssertEqual(fake.toolPages.map(\.app), Array(repeating: "luoke-mem", count: 6))
        XCTAssertEqual(fake.toolPages.map(\.page), [nil, "item", "kept", "item", "people", nil])
        XCTAssertEqual(fake.toolPages.map(\.args), [[:], ["id": "12"], ["view": "recent"], ["id": "7"], [:], [:]])
    }

    /// 没带主窗口时起记忆界面的参数：people 要带着的记忆认得（luoke-mem 0.36.1 起），不然打开首页
    func testMemoryEntryGetsOnlyTheViewsItKnows() {
        let entry = ["-m", "luoke_mem", "app"]
        XCTAssertEqual(Memory.entryArguments(entry, note: nil, view: nil, version: "0.36.1"), ["-B", "-m", "luoke_mem", "app"])
        XCTAssertEqual(Memory.entryArguments(entry, note: "7", view: "people", version: "0.36.1"), ["-B", "-m", "luoke_mem", "app", "--note", "7"])
        XCTAssertEqual(Memory.entryArguments(entry, note: nil, view: "recent", version: "0.16.0"), ["-B", "-m", "luoke_mem", "app", "--view", "recent"])
        XCTAssertEqual(Memory.entryArguments(entry, note: nil, view: "people", version: "0.36.1"), ["-B", "-m", "luoke_mem", "app", "--view", "people"])
        XCTAssertEqual(Memory.entryArguments(entry, note: nil, view: "people", version: "0.40.0"), ["-B", "-m", "luoke_mem", "app", "--view", "people"])
        XCTAssertEqual(Memory.entryArguments(entry, note: nil, view: "people", version: "0.36.0"), ["-B", "-m", "luoke_mem", "app"])
        XCTAssertEqual(Memory.entryArguments(entry, note: nil, view: "people", version: "0.9.0"), ["-B", "-m", "luoke_mem", "app"])
        XCTAssertEqual(Memory.entryArguments(entry, note: nil, view: "people", version: nil), ["-B", "-m", "luoke_mem", "app"])
    }

    /// 检查更新画在主窗口的一页里：不查更新的那一版（本机版、开发版）不在页标题下面再写一遍「检查更新」；
    /// 自己的小窗口照旧写，别的状态照旧写现在怎样
    func testCheckUpdatePageDoesNotRepeatItsTitle() {
        let m = UpdateModel(found: nil, autoCheck: false)
        m.phase = .info("这是开发版螺壳，没有发布渠道，不查更新。")
        XCTAssertEqual(UpdateView.title(m, inPage: true), "")
        XCTAssertEqual(UpdateView.title(m, inPage: false), "检查更新")
        m.phase = .latest
        XCTAssertEqual(UpdateView.title(m, inPage: true), m.appsWithUpdates.isEmpty ? "已经是最新的" : "螺壳已经是最新的")
    }

    /// 画在主窗口的一页、整个窗口里时跟着主窗口的大小；卡片和自己的小窗口照原来的大小
    func testPagesFillTheWindowCardsKeepTheirSize() {
        let sized = NSSize(width: 560, height: 460)
        func size(_ v: AnyView) -> NSSize { NSHostingView(rootView: v).fittingSize }
        XCTAssertEqual(size(AnyView(Color.red.lkWindowSize(width: 560, height: 460))), sized, "自己的小窗口：原来的大小")
        for style in [HostStyle.page, .takeover, .card] {
            XCTAssertEqual(MainWindow.present("probe", title: "probe", style: style) { Color.red.lkWindowSize(width: 560, height: 460) }, .shown)
            let got = size(fake.views.last!)
            if style == .card { XCTAssertEqual(got, sized, "卡片照原来的大小") } else { XCTAssertNotEqual(got, sized, "\(style) 要跟着主窗口") }
        }
    }

    /// 主窗口里同一处有一块正忙着（正在卸载、正问着系统）：交过去的没画上去，调用方知道没开，不另开窗口、不登记等待
    func testABusyMainWindowIsNotTakenAsShown() {
        fake.busy = true
        XCTAssertEqual(MainWindow.present("probe", title: "probe", style: .page) { EmptyView() }, .busy)
        var done = 0
        XCTAssertFalse(SearchActivation.show { done += 1 }, "开启螺壳搜索的卡片没开")
        XCTAssertFalse(MainWindow.showing(SearchActivation.hostID))
        XCTAssertNil(SearchActivation.window, "不另开窗口")
        let before = Updater.current
        Updater.show()
        XCTAssertTrue(Updater.current === before, "没开的那一页不算开着的那一次")
        XCTAssertNil(Updater.window)
        ReleaseNotesWindow.show(tag: "v0.0.0-test")
        XCTAssertNil(ReleaseNotesWindow.window)
        XCTAssertTrue(fake.presented.isEmpty)
        XCTAssertEqual(done, 0)
        // 忙完了：照常开
        fake.busy = false
        XCTAssertTrue(SearchActivation.show { done += 1 })
        XCTAssertTrue(MainWindow.showing(SearchActivation.hostID))
        SearchActivation.close()
    }

    /// 开启螺壳搜索问着系统时锁住主窗口的关闭按钮，问完放开
    func testSearchActivationLocksTheWindowWhileAsking() {
        XCTAssertTrue(SearchActivation.show())
        SearchActivation.holdOpen(true)
        SearchActivation.holdOpen(false)
        XCTAssertEqual(fake.locks.map(\.id), [SearchActivation.hostID, SearchActivation.hostID])
        XCTAssertEqual(fake.locks.map(\.locked), [true, false])
        SearchActivation.close()
    }

    /// 没在升级时离开「检查更新」再打开：每次都是新的一次（现查），旧的那次算关了
    func testReopeningCheckUpdateStartsAFreshOne() throws {
        Updater.show()
        let first = try XCTUnwrap(Updater.current)
        XCTAssertTrue(Updater.shows(first))
        fake.leave(Updater.hostID)
        XCTAssertFalse(Updater.shows(first))
        Updater.show()
        let second = try XCTUnwrap(Updater.current)
        XCTAssertFalse(first === second)
        XCTAssertFalse(Updater.shows(first), "旧的那次不再算开着")
        XCTAssertTrue(Updater.shows(second))
        Updater.close()
    }

    /// 升级在跑时（下载、核对时）离开再打开：放回来的还是那一次，不另起一次（两次会碰同一份安装包、staged 和升级标记）。
    /// 它走到要点「输入密码并升级」时，看到开着的就是自己，照常等用户点
    func testReopeningCheckUpdateWhileUpgradingBringsTheSameOneBack() throws {
        Updater.show()
        let first = try XCTUnwrap(Updater.current)
        first.upgrading = true
        defer { first.upgrading = false }
        fake.leave(Updater.hostID)
        XCTAssertFalse(Updater.shows(first))
        Updater.show()
        XCTAssertTrue(Updater.current === first, "放回来的是还在跑的那一次")
        XCTAssertTrue(Updater.shows(first))
        XCTAssertEqual(fake.presented.filter { $0.id == Updater.hostID }.count, 2)
        // 跑完了：再打开就是新的一次
        first.upgrading = false
        fake.leave(Updater.hostID)
        Updater.show()
        XCTAssertFalse(Updater.current === first)
        Updater.close()
    }

    /// 同一次升级不起第二遍（点了两下、重试时上一遍还没退出）
    func testAnUpgradeRunsOnceAtATime() {
        let found = Updater.Found(tag: "internal-0000", page: "https://example.invalid", manifest: [:])
        let m = UpdateModel(found: found, autoCheck: false)
        m.upgrading = true
        m.upgrade()
        XCTAssertEqual(m.phase, .found, "还在跑的那一遍没退出：这一下不起")
    }
}

#if canImport(LuokeWindow)
/// 主窗口这一边怎么接本体的界面：用不开真窗口的 AgentCore，只看主窗口里画的是哪一块、在哪一页
@MainActor
final class MainWindowHostTests: XCTestCase {
    private let mail = [InstalledApp(accessory: "luoke-mail", version: "1")]

    private func screen(_ id: String, _ style: HostStyle, canLeave: (() -> Bool)? = nil, closed: (() -> Void)? = nil,
                        lockClose: Bool = false) -> HostScreen {
        HostScreen(id: id, title: id, style: style, view: AnyView(EmptyView()), canLeave: canLeave, closed: closed, lockClose: lockClose)
    }

    /// 主线程上排着的先跑完（应用列表变了以后，记着的页在下一轮才去）
    private func drainMain(after delay: TimeInterval = 0) {
        let e = expectation(description: "main")
        if delay == 0 { DispatchQueue.main.async { e.fulfill() } } else { DispatchQueue.main.asyncAfter(deadline: .now() + delay) { e.fulfill() } }
        wait(for: [e], timeout: 5)
    }

    /// 同一处有一块走不开：不换上去，答没接，原来那块还在；别处的照常
    func testABusyPlaceTurnsOthersAway() {
        let core = AgentCore(windowless: true)
        var working = true
        XCTAssertTrue(core.present(screen("uninstall", .page, canLeave: { !working })))
        XCTAssertFalse(core.present(screen("check-update", .page)))
        XCTAssertEqual(core.model.hostPage?.id, "uninstall")
        XCTAssertTrue(core.present(screen("permission", .card)), "卡片不受那一页影响")
        working = false
        XCTAssertTrue(core.present(screen("check-update", .page)))
        XCTAssertEqual(core.model.hostPage?.id, "check-update")
    }

    /// 关闭按钮：开启螺壳搜索问着系统时锁上、问完放开；升级锁着的不跟着放开；撤掉的那一块同时放开
    func testCloseLocks() {
        let core = AgentCore(windowless: true)
        core.lockClose("search-activation", true)
        XCTAssertFalse(core.closeLocked, "没画在主窗口里的不管")
        core.present(screen("search-activation", .card))
        core.lockClose("search-activation", true)
        XCTAssertTrue(core.closeLocked)
        core.lockClose("search-activation", false)
        XCTAssertFalse(core.closeLocked)
        core.present(screen("upgrade", .takeover, lockClose: true))
        core.lockClose("search-activation", true)
        core.lockClose("search-activation", false)
        XCTAssertTrue(core.closeLocked, "升级还锁着")
        core.dismiss("upgrade")
        XCTAssertFalse(core.closeLocked)
        core.lockClose("search-activation", true)
        core.dismiss("search-activation")
        XCTAssertFalse(core.closeLocked)
    }

    /// 首次运行、升级的整窗盖着时打开的一页：整窗走完停在这一页，不回「今天」，也不算用户离开了它
    func testAPageOpenedUnderSetupIsShownAfterIt() {
        let core = AgentCore(windowless: true)
        var left = 0
        core.present(screen("first-run", .takeover))
        core.present(screen("check-update", .page, closed: { left += 1 }))
        XCTAssertEqual(core.model.hostTakeover?.id, "first-run", "整窗盖在上面")
        core.endSetup("first-run", keepOpen: true)
        core.showToday()
        XCTAssertEqual(core.model.hostPage?.id, "check-update")
        XCTAssertEqual(left, 0)
        // 没有整窗时照旧：回「今天」就是离开了这一页
        core.showToday()
        XCTAssertNil(core.model.hostPage)
        XCTAssertEqual(left, 1)
        XCTAssertEqual(core.model.page, .today)
    }

    /// 冷启动时核心还没列出应用：要去的应用页（和它的哪一页、参数）先记着，列出来再去
    func testAToolPageWaitsForTheAppList() {
        let first = AgentCore(windowless: true)
        first.openToolPage(app: "luoke-mail", page: nil, args: [:])
        XCTAssertEqual(first.model.page, .today)
        first.model.setInstalledApps(mail)
        drainMain()
        XCTAssertEqual(first.model.page, .mail)

        let item = AgentCore(windowless: true)
        item.openToolPage(app: "luoke-mail", page: "rules", args: ["id": "3"])
        item.model.setInstalledApps(mail)
        drainMain()
        XCTAssertEqual(item.model.page, .mail)
        XCTAssertEqual(item.model.appTab, "rules")

        let listed = AgentCore(windowless: true)
        listed.model.setInstalledApps(mail)
        listed.openToolPage(app: "luoke-mail", page: nil, args: [:])
        XCTAssertEqual(listed.model.page, .mail, "已经列出来的当场去")
    }

    /// 用户这期间自己换了页、开了本体的一页，或者等太久还没列出来：记着的不再去
    func testAToolPageGivesUp() {
        let moved = AgentCore(windowless: true)
        moved.openToolPage(app: "luoke-mail", page: nil, args: [:])
        moved.model.go(.scan)
        moved.model.setInstalledApps(mail)
        drainMain()
        XCTAssertEqual(moved.model.page, .scan)

        let covered = AgentCore(windowless: true)
        covered.openToolPage(app: "luoke-mail", page: nil, args: [:])
        covered.present(screen("check-update", .page))
        covered.model.setInstalledApps(mail)
        drainMain()
        XCTAssertEqual(covered.model.hostPage?.id, "check-update")
        XCTAssertEqual(covered.model.page, .today)

        let wait = AgentCore.toolPageWait
        AgentCore.toolPageWait = 0.01
        defer { AgentCore.toolPageWait = wait }
        let slow = AgentCore(windowless: true)
        slow.openToolPage(app: "luoke-mail", page: nil, args: [:])
        drainMain(after: 0.2)
        slow.model.setInstalledApps(mail)
        drainMain()
        XCTAssertEqual(slow.model.page, .today)
    }
}
#endif
