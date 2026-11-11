// 收进螺壳.app、由本体当子进程起的应用（ADR 0022）：跟螺壳整体升级，在「应用」里停用、删数据。
// 这一份没带某个应用（开发版没给 LUOKE_SEARCH_SRC、LUOKE_MAIL_SRC）时它的 manifest 是 nil，这里什么都不做，装在 ~/Applications 的旧包照旧。
import Foundation

protocol BundledApp {
    static var accessory: String { get }
    static var manifest: Accessories.Manifest? { get }
    /// 主控起来以后（HubModel.startHub）、用户重新启用时。重复调不要紧
    static func start()
    /// 本体退出、用户停用、卸载：停掉，不再自动起。主线程上调，排在停主控之前，它要向主控报一声
    static func stop()
    /// 换装前（Installer）：这份程序不是这个进程起的，按路径认
    static func stopAll(hubApp: URL)
    /// 有自己的页面：「应用」窗口、菜单栏能打开它
    static var opens: Bool { get }
    static func open()
}

extension BundledApp {
    static var opens: Bool { false }
    static func open() {}

    /// 开着：主控记在 state.json 的 enabled（主控没起时也读得到），用户没选过时看清单，和 Hub.helpers_off 同一个算法
    static var enabled: Bool { BundledApps.enabled(accessory) }

    /// 记下开启，起程序（螺壳搜索要到权限以后才调，SearchActivation）
    static func enable(done: @escaping (String?) -> Void) {
        DispatchQueue.global().async {
            let r = Result { try HubModel.shared.client.request("POST", "/accessories/enable", body: ["accessory": accessory], timeout: 10) }
            DispatchQueue.main.async {
                switch r {
                case .success: start(); done(nil)
                case .failure(let e): done((e as? HubError)?.message ?? "\(e)")
                }
            }
        }
    }

    /// 用户点「停用」：停掉程序，再请主控记下、停它的空闲任务。和卸载无关，数据和记忆都不动，要用时点「开启」。
    /// 先停再记，和主控放掉它的模型的顺序一致（不然它退出前还可能把模型叫起来）；没记下就起回来，不然界面说开着、程序却没跑
    static func disable(done: @escaping (String?) -> Void) {
        disable(record: { _ = try HubModel.shared.client.request("POST", "/accessories/disable", body: ["accessory": accessory], timeout: 30) }, done: done)
    }

    /// `record` 是给测试的
    static func disable(record: @escaping () throws -> Void, done: @escaping (String?) -> Void) {
        stopping(record) { r in
            if case .failure(let e) = r { done((e as? HubError)?.message ?? "\(e)") } else { done(nil) }
        }
    }

    /// 停掉程序再请主控做事；主控没答应就起回来（主控已经记下停用时，start 看 enabled 不会起）
    static func stopping<T>(_ request: @escaping () throws -> T, done: @escaping (Result<T, Error>) -> Void) {
        DispatchQueue.global().async {
            DispatchQueue.main.sync { stop() }        // 要等它退出，最长几秒；先让界面把「正在停用」画出来
            let r = Result { try request() }
            DispatchQueue.main.async {
                if case .failure = r { start() }
                done(r)
            }
        }
    }
}

enum BundledApps {
    static let all: [BundledApp.Type] = [SearchHelper.self, MailHelper.self, ScanHelper.self]

    static func named(_ accessory: String) -> BundledApp.Type? { all.first { $0.accessory == accessory } }

    /// 没有用户的选择时开不开：清单的 default_on，没写就是关（要系统权限的等用户开启）。清单的位置和主控 bundled_helpers 一样
    static func defaultOn(_ accessory: String, resources: URL = AppInfo.resources) -> Bool {
        let file = resources.appendingPathComponent(accessory.replacingOccurrences(of: "luoke-", with: "")).appendingPathComponent("luoke-accessory.json")
        guard let data = try? Data(contentsOf: file), let o = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return false }
        return o["default_on"] as? Bool == true
    }

    static func enabled(_ accessory: String) -> Bool { isEnabled(accessory, defaultOn: defaultOn(accessory)) }

    /// 用户开启了没有：state.json 的 enabled，没选过看 defaultOn（不是 BundledApp 的工具也用，ToolHost）
    static func isEnabled(_ accessory: String, defaultOn: Bool, state: URL = AppInfo.envDir.appendingPathComponent("state.json")) -> Bool {
        guard let data = try? Data(contentsOf: state), let o = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return defaultOn }
        return (o["enabled"] as? [String: Bool])?[accessory] ?? defaultOn
    }

    /// 用户确认过的：state.json 的 permissions_ok，{"配件": {"权限 id": 确认的时间}}。没写的就是没放行
    static func permissionsOk(state: URL = AppInfo.envDir.appendingPathComponent("state.json")) -> [String: [String: String]] {
        guard let data = try? Data(contentsOf: state), let o = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return [:] }
        return o["permissions_ok"] as? [String: [String: String]] ?? [:]
    }

    /// 清单里声明了、用户没确认的：起工具时告诉它这几项不能用。这是约定，系统并不拦它
    static func notConfirmed(declared: [String], ok: [String: String]) -> [String] {
        declared.filter { ok[$0] == nil }
    }

    static func offValue(_ accessory: String) -> String? {
        let off = notConfirmed(declared: PermissionWindow.declared(app: accessory), ok: permissionsOk()[accessory] ?? [:])
        return off.isEmpty ? nil : off.joined(separator: ",")
    }

    static func offVariable(_ accessory: String) -> String? {
        offValue(accessory).map { "LUOKE_PERMISSIONS_OFF=" + $0 }
    }

    /// 记下用户确认了这一项，或者关掉了。主控只收清单里声明过的
    static func record(_ accessory: String, _ id: String, on: Bool, done: @escaping (String?) -> Void = { _ in }) {
        DispatchQueue.global().async {
            let r = Result { try HubModel.shared.client.request("POST", "/accessories/permission", body: ["accessory": accessory, "permission": id, "on": on], timeout: 10) }
            DispatchQueue.main.async {
                switch r {
                case .success: done(nil)
                case .failure(let e): done((e as? HubError)?.message ?? "\(e)")
                }
            }
        }
    }

    /// 用户同意了或收回了某个应用用这一项。记下，起它时算数：扫描每件事都是新进程；搜索常驻，重起一下，当场算数
    static func setPermission(_ accessory: String, _ id: String, on: Bool, done: @escaping (String?) -> Void) {
        record(accessory, id, on: on) { why in
            if why == nil, accessory == SearchHelper.accessory { SearchHelper.permissionChanged() }
            done(why)
        }
    }

    /// 这个应用接得住活了：刚改的权限要重起它时，等它起来
    static func whenUp(_ accessory: String, _ then: @escaping () -> Void) {
        if accessory == SearchHelper.accessory { SearchHelper.whenUp(then) } else { then() }
    }

    /// 用户刚开启或刚确认：清单里写了 first_use 的任务马上跑，权限第一次用到就紧跟着这一下
    static func firstUse(_ accessory: String) {
        DispatchQueue.global().async {
            do { _ = try HubModel.shared.client.request("POST", "/accessories/first-use", body: ["accessory": accessory], timeout: 10) }
            catch { note("\(accessory)：第一次任务没起来：\(error)") }
        }
    }

    /// 开发版、实验室构建：有自己的数据，本机装着的旧包不归它们管
    static var developmentBuild: Bool {
        let id = Bundle.main.bundleIdentifier ?? ""
        return id.hasSuffix(".dev") || id.hasSuffix(".lab")
    }

    /// 收进来以前单独装的 .app：先结束，再挪进废纸篓。grace 是手上的活要做完的时间，数据不动。返回有没有旧的
    @discardableResult
    static func retireApps(_ ids: Set<String>, label: String, grace: TimeInterval) -> Bool {
        let fm = FileManager.default
        var hadOne = false
        for dir in [fm.homeDirectoryForCurrentUser.appendingPathComponent("Applications"), URL(fileURLWithPath: "/Applications")] {
            let found = (try? fm.contentsOfDirectory(at: dir, includingPropertiesForKeys: nil)) ?? []
            for app in found where app.pathExtension == "app" && ids.contains(AppPlacement.bundleID(app) ?? "") {
                let pids = Procs.pids().filter { Procs.inside(Procs.exePath($0), app) }
                hadOne = true
                HelperProcess.end(pids, grace: grace)
                do {
                    try AppPlacement.trash(app)
                    note("\(label)：旧的 \(app.path) 挪进了废纸篓，结束了 \(pids.count) 个进程")
                } catch {
                    note("\(label)：旧的 \(app.path) 挪不走：\(error.localizedDescription)")
                }
            }
        }
        return hadOne
    }

    /// 写进 hub.log（诊断信息带它），系统日志也留一份。主控也往这个文件追加；不用 FD.appendLog：它会把主控开着的文件改名
    static func note(_ text: String, log: URL = AppInfo.envDir.appendingPathComponent("logs/hub.log")) {
        NSLog("%@", text)
        try? FileManager.default.createDirectory(at: log.deletingLastPathComponent(), withIntermediateDirectories: true)
        let fd = open(log.path, O_WRONLY | O_APPEND | O_CREAT | O_CLOEXEC | O_NOFOLLOW, 0o644)
        guard fd >= 0 else { return }
        defer { close(fd) }
        let f = DateFormatter()
        f.locale = Locale(identifier: "en_US_POSIX")
        f.dateFormat = "yyyy-MM-dd HH:mm:ss"
        let line = Data((f.string(from: Date()) + " " + Diagnostics.redacted(text) + "\n").utf8)
        _ = line.withUnsafeBytes { write(fd, $0.baseAddress, $0.count) }
    }
}
