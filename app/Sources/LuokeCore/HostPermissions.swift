// 螺壳名下的权限（顶层 ADR 0021 的 11-06 修订）：组件不向系统要，本体在自己的窗口里问用户，再向系统要。
// 带主窗口的这一份不另开窗口：问用户的是盖在主窗口上的一张卡片（11-10：螺壳只有一个窗口）。
// 包里的程序在系统眼里都是螺壳，所以只查本体自己；查到的报给权限页和卡片（AgentCore）。
import AppKit
import Darwin
import Foundation
import Photos
import SwiftUI

enum HostPermissions {
    /// 打开 TCC 数据库要完全磁盘访问，macOS 不弹框：和螺壳扫描同一个查法。nil 是查不出
    static func fullDisk(home: String = NSHomeDirectory()) -> Bool? {
        for path in [home + "/Library/Application Support/com.apple.TCC/TCC.db", "/Library/Application Support/com.apple.TCC/TCC.db"] {
            let fd = open(path, O_RDONLY | O_NONBLOCK | O_CLOEXEC)
            if fd >= 0 { close(fd); return true }
            if errno == EPERM || errno == EACCES { return false }
        }
        return nil
    }

    static func state(_ status: PHAuthorizationStatus) -> String {
        switch status {
        case .authorized, .limited: return "granted"
        case .notDetermined: return "ask"
        default: return "denied"
        }
    }

    /// 权限 id（工具清单里 `owner: "luoke"` 的）→ granted、denied、ask。查不出的不报
    static func current() -> [String: String] {
        var out = ["photos": state(PHPhotoLibrary.authorizationStatus(for: .readWrite))]
        if let fda = fullDisk() { out["fda"] = fda ? "granted" : "denied" }
        return out
    }

    /// 系统设置里这一项的那一页
    static func settingsURL(_ id: String) -> URL? {
        let pane = id == "fda" ? "Privacy_AllFiles" : "Privacy_Photos"
        return URL(string: "x-apple.systempreferences:com.apple.settings.PrivacySecurity.extension?\(pane)")
    }
}

/// 授权框只能紧跟在用户的操作后面：不是他刚在螺壳里点过、按过键，就不做会弹框的事
enum UserAction {
    static func recent(within seconds: TimeInterval = 10) -> Bool {
        guard NSWorkspace.shared.frontmostApplication?.processIdentifier == getpid() else { return false }
        return min(CGEventSource.secondsSinceLastEventType(.combinedSessionState, eventType: .leftMouseUp),
                   CGEventSource.secondsSinceLastEventType(.combinedSessionState, eventType: .keyDown)) < seconds
    }
}

/// 要一项螺壳名下的权限：应用的清单说要什么、为什么，用户同意了本体才向系统要。开启应用时、点了卡片或权限页时都走这里
enum PermissionWindow {
    struct Item: Equatable {
        let app: String
        let appName: String
        let id: String
        let title: String
        let facts: [String]
    }

    static var window: NSWindow?
    private static var model: PermissionModel?
    static let hostID = "permission"
    /// 问用户的那张卡片（窗口）开着
    static var isOpen: Bool { MainWindow.showing(hostID) || window?.isVisible == true }

    private static func listed(app: String, resources: URL) -> (name: String?, permissions: [[String: Any]]) {
        let file = resources.appendingPathComponent(app.replacingOccurrences(of: "luoke-", with: "")).appendingPathComponent("luoke-accessory.json")
        guard let data = try? Data(contentsOf: file), let o = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let caps = o["capabilities"] as? [String: Any], let list = caps["permissions"] as? [[String: Any]] else { return (nil, []) }
        return (o["name"] as? String, list.filter { $0["kind"] as? String == "system" && $0["owner"] as? String == "luoke" })
    }

    /// 这个应用清单里声明的、螺壳名下的系统权限
    static func declared(app: String, resources: URL = AppInfo.resources) -> [String] {
        listed(app: app, resources: resources).permissions.compactMap { $0["id"] as? String }
    }

    /// 这个应用的清单里写的这一项；不是螺壳带的应用、没声明这一项就是 nil：没声明的不能申请
    static func item(app: String, id: String, resources: URL = AppInfo.resources) -> Item? {
        let (name, list) = listed(app: app, resources: resources)
        guard let p = list.first(where: { $0["id"] as? String == id }) else { return nil }
        return Item(app: app, appName: name ?? app, id: id, title: p["title"] as? String ?? id, facts: p["facts"] as? [String] ?? [])
    }

    /// 两层各缺什么，下一步做什么：先问用户让不让这个应用用（窗口），再看系统给没给螺壳
    enum Step: Equatable { case window, ready, askSystem, settings }

    static func step(state: String?, confirmed: Bool) -> Step {
        guard confirmed else { return .window }
        switch state {
        case "granted": return .ready
        case "ask": return .askSystem
        case "denied": return .settings
        default: return .window     // 查不出：窗口里说清楚，让用户决定
        }
    }

    /// 窗口、卡片、页面上要这一项的按钮。缺什么接着要，用户不用回来再点；都有了调 `ready`，用户没给就不调
    static func ask(app: String, id: String, ready: (() -> Void)? = nil) {
        guard !isOpen, let item = item(app: app, id: id) else { show(app: app, id: id, ready: ready); return }
        switch step(state: HostPermissions.current()[id], confirmed: BundledApps.permissionsOk()[app]?[id] != nil) {
        case .window:
            show(app: app, id: id, ready: ready)
        case .ready:
            ready?()
        case .askSystem, .settings:
            model?.stop()
            let m = PermissionModel(item: item, enabling: false, done: nil)
            m.whenReady(ready)
            model = m
            m.continueConfirmed()
        }
    }

    /// 权限页上螺壳那一行，系统还没问过：只问系统，不问用户，不动各应用的「允许」
    static func askSystem(id: String) {
        guard id == "photos" else { return }        // 只有照片能直接问系统
        DispatchQueue.global().async {
            _ = SearchActivation.askPhotos()
            DispatchQueue.main.async {
                MainWindow.permissionsChanged()
                _ = AppWindows.bringBack()
            }
        }
    }

    /// 用户在权限页上关掉了这一项：开着的窗口是照同意过画的，只剩「完成」，留着下次扳开就确认不了；也不用再等系统设置
    static func withdrawn(app: String, id: String) {
        guard let m = model, m.item.app == app, m.item.id == id else { return }
        close()
    }

    /// enabling：开启应用时，done 是用户点了开启（权限给没给都算开，没给就少做一些）。窗口关了不调。
    /// 答开了没有：主窗口里另一张卡片正忙着（正问着系统）时没开、什么都不等（答 false），ready、done 都不会来，调用方自己说一声
    @discardableResult
    static func show(app: String, id: String, enabling: Bool = false, ready: (() -> Void)? = nil, done: (() -> Void)? = nil) -> Bool {
        guard let item = item(app: app, id: id) else { done?(); return true }
        if isOpen, model?.item == item {
            if let ready { model?.whenReady(ready) }
            if let w = window, w.isVisible { Uninstall.bringFront(w) } else { MainWindow.show() }
            return true
        }
        let m = PermissionModel(item: item, enabling: enabling, done: done)
        m.whenReady(ready)
        // 带主窗口：盖在主窗口上的卡片。用户关掉它（不是点按钮）和原来关窗口一样：不再等，done 不调
        switch MainWindow.present(hostID, title: enabling ? "开启\(item.appName)" : item.title, style: .card,
                                  closed: { m.stop(); MainWindow.permissionsChanged() }, { PermissionView(model: m) }) {
        case .busy: return false
        case .shown:
            model?.stop()
            model = m
            m.watch()
            return true
        case .noWindow: break
        }
        model?.stop()
        window?.close()
        model = m
        let host = NSHostingController(rootView: PermissionView(model: m))
        // 不用 .preferredContentSize：见 SearchActivation
        let w = NSWindow(contentViewController: host)
        w.title = enabling ? "开启\(item.appName)" : item.title
        w.styleMask = [.titled, .closable]
        w.setContentSize(host.view.fittingSize)
        w.center()
        w.isReleasedWhenClosed = false
        window = w
        AppWindows.track(w)
        Uninstall.bringFront(w)
        m.watch()
        return true
    }

    static func close() {
        model?.stop()
        MainWindow.dismiss(hostID)
        window?.close()
        MainWindow.permissionsChanged()
    }
}

/// 等着做的活。用户走开了就不算数：过几天才给权限，不该把那一下的活翻出来
struct ExpiringWork {
    private var work: (() -> Void)?
    private let until: Date

    init(_ work: (() -> Void)?, limit: TimeInterval = 600, now: Date = Date()) {
        self.work = work
        until = now.addingTimeInterval(limit)
    }

    /// 只拿得到一次，过了时限拿不到
    mutating func take(now: Date = Date()) -> (() -> Void)? {
        defer { work = nil }
        return now < until ? work : nil
    }
}

/// 用户同意了才放行，只在这里确认一次：点了「开启」「允许」「打开系统设置」就是同意，记下；
/// 点「跳过」「以后再说」不记，应用开着也用不了这一项
final class PermissionModel: ObservableObject {
    let item: PermissionWindow.Item
    let enabling: Bool
    private var done: (() -> Void)?
    private var timer: Timer?
    private var ranFirst = false
    private var noWindow = false
    /// 按钮的活在等这一项：用户同意了、系统也给了才调，只调一次
    func whenReady(_ work: (() -> Void)?) { waiting = ExpiringWork(work) }
    private var waiting = ExpiringWork(nil)
    @Published var granted = false
    @Published var asking = false
    /// 用户在这个窗口里同意了：系统的授权可能还没给
    @Published var agreed = false
    /// 系统不再弹框了（拒绝过），或者这一项本来就没有弹框：要去系统设置
    @Published var needsSettings = false
    /// 同意正在记，记完才往下走
    @Published var recording = false
    /// 同意没记下：窗口留着，用户再点一次
    @Published var failed: String?

    init(item: PermissionWindow.Item, enabling: Bool, done: (() -> Void)?) {
        self.item = item
        self.enabling = enabling
        self.done = done
        refresh()
    }

    private func refresh() {
        let state = HostPermissions.current()[item.id]
        granted = state == "granted"
        needsSettings = item.id != "photos" || state == "denied"     // 只有照片能在这里直接问系统
    }

    /// 用户去系统设置时一直查，回来不用再点。给了就用上：这一下紧跟着用户在系统设置里的操作
    func watch() {
        timer = Timer.scheduledTimer(withTimeInterval: 1, repeats: true) { [weak self] _ in
            guard let self else { return }
            let was = self.granted
            self.refresh()
            if self.granted && !was {
                MainWindow.permissionsChanged()
                if self.agreed { self.firstUse() }
                self.settled()
                if self.noWindow { self.stop() }
            }
        }
    }

    /// 用户确认过、系统这层还缺：不用再看一遍说明，没有窗口。去系统设置时一样每秒查：macOS 给完完全磁盘访问会退出重开螺壳，
    /// 等回到前台就晚了
    func continueConfirmed() {
        agreed = true
        noWindow = true
        if needsSettings {
            watch()
            if let u = HostPermissions.settingsURL(item.id) { NSWorkspace.shared.open(u) }
        } else {
            askSystem()
        }
    }

    /// 两层都有了：等着的活接着做
    private func settled() {
        guard agreed, granted, let run = waiting.take() else { return }
        BundledApps.whenUp(item.app, run)
    }

    func stop() {
        timer?.invalidate()
        timer = nil
    }

    /// 记下了才算同意：没记下就停在窗口里说一声，不往下走
    private func agree(then next: @escaping () -> Void) {
        guard !agreed else { next(); return }
        guard !recording else { return }
        recording = true
        failed = nil
        BundledApps.setPermission(item.app, item.id, on: true) { why in
            self.recording = false
            if let why {
                BundledApps.note("\(self.item.appName)：确认没记下：\(why)")
                self.failed = "没记下，再试一次。"
                return
            }
            self.agreed = true
            MainWindow.permissionsChanged()
            self.turnOn()
            next()
        }
    }

    /// 应用开着才有第一次；开启这一下做完窗口可能还留着，等用户去系统设置
    private func turnOn() {
        let then = done
        done = nil
        then?()
    }

    private func firstUse() {
        guard !ranFirst, enabling || BundledApps.enabled(item.app) else { return }     // 应用关着：确认只记下，不起任务
        ranFirst = true
        BundledApps.firstUse(item.app)
    }

    func ask() {
        agree {
            if self.needsSettings {
                if let u = HostPermissions.settingsURL(self.item.id) { NSWorkspace.shared.open(u) }
                return
            }
            self.askSystem()
        }
    }

    private func askSystem() {
        asking = true
        DispatchQueue.global().async {
            _ = SearchActivation.askPhotos()
            DispatchQueue.main.async {
                self.asking = false
                self.refresh()
                MainWindow.permissionsChanged()
                self.settled()
                // 系统的询问框关掉后，macOS 不一定把螺壳放回前面，用户就找不到窗口了。给了就不用再点「完成」
                if self.noWindow {
                    if self.granted { self.firstUse() }
                } else if self.granted && !self.enabling {
                    self.finish()
                }
                _ = AppWindows.bringBack()
            }
        }
    }

    /// 系统已经给了螺壳，这个应用用不用还是用户说了算
    func allow() {
        agree {
            self.settled()
            self.finish()
        }
    }

    /// 「跳过」「以后再说」「完成」：没同意的不记；应用开启了就起第一次，用不到这一项就少做一些
    func finish() {
        settled()
        turnOn()
        PermissionWindow.close()
        if enabling || agreed { firstUse() }
    }
}

struct PermissionView: View {
    @ObservedObject var model: PermissionModel
    @Environment(\.lkHost) private var host

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            HStack(spacing: 12) {
                LKAppIcon(accessory: model.item.app, size: 36)
                VStack(alignment: .leading, spacing: 2) {
                    // 主窗口的卡片上标题是主窗口画的（MainWindow.present 给的那句）
                    if host != .card { Text(model.enabling ? "开启\(model.item.appName)" : model.item.appName).font(.system(size: 18, weight: .semibold)) }
                    Text("需要「\(model.item.title)」").font(.system(size: 13)).foregroundStyle(Color.lkMuted)
                }
            }
            if !model.item.facts.isEmpty {
                LKGroup {
                    VStack(alignment: .leading, spacing: 6) {
                        ForEach(model.item.facts, id: \.self) { Text($0).font(.system(size: 13)).fixedSize(horizontal: false, vertical: true) }
                    }
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(.horizontal, 16).padding(.vertical, 10)
                }
            }
            if model.granted {
                LKBadge(text: "已授权", kind: .success)
            } else if model.needsSettings && model.agreed {
                Text("在系统设置里打开「\(AppInfo.appName)」").font(.system(size: 13))
            }
            if let failed = model.failed {
                Text(failed).font(.system(size: 13)).foregroundStyle(Color.lkMuted)
            }
            HStack {
                Spacer()
                if model.agreed {
                    if model.granted {
                        Button("完成") { model.finish() }.buttonStyle(LKPrimaryButton())
                    } else {
                        Button("完成") { model.finish() }.buttonStyle(LKSecondaryButton()).disabled(model.asking)
                    }
                } else {
                    Button(model.enabling ? "跳过" : "以后再说") { model.finish() }.buttonStyle(LKSecondaryButton()).disabled(model.asking || model.recording)
                }
                if model.granted {
                    if !model.agreed { Button(model.enabling ? "开启" : "允许") { model.allow() }.buttonStyle(LKPrimaryButton()).disabled(model.recording) }
                } else {
                    Button(model.needsSettings ? "打开系统设置" : "允许") { model.ask() }.buttonStyle(LKPrimaryButton()).disabled(model.asking || model.recording)
                }
            }
        }
        .lkCardBody()
    }
}
