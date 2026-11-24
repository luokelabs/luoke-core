// 升级上来的用户没走过首次安装里开启搜索和扫描的那一步：没装过、也没选过的，补问一次，每个应用一次（BundledApps.afterRetire）。
// 老用户的螺壳搜索记成开着以后要重新允许的那张卡片也走这里。和开机时一样不在后台弹：等用户把螺壳叫到前面、
// 没有别的整窗或卡片（首次运行、检查更新、开启、要权限）时，一张一张问。问过的记在 UserDefaults，不碰 state.json
import AppKit

enum HelperOffer {
    static let offeredKey = "helperOffered"
    private static var queue: [(id: String, show: () -> Bool)] = []
    private static var timer: Timer?

    static func offered(_ accessory: String, defaults: UserDefaults = .standard) -> Bool {
        (defaults.stringArray(forKey: offeredKey) ?? []).contains(accessory)
    }

    static func markOffered(_ accessories: [String], defaults: UserDefaults = .standard) {
        var s = defaults.stringArray(forKey: offeredKey) ?? []
        for a in accessories where !s.contains(a) { s.append(a) }
        defaults.set(s, forKey: offeredKey)
    }

    /// 首次安装完成页上那个「开启」：搜索是开启的卡片，扫描是要完全磁盘访问的那张，点了开启才记开着
    static func offer(_ accessory: String) {
        switch accessory {
        case SearchHelper.accessory:
            guard SearchHelper.mayRun else { return }
            add(accessory) { SearchActivation.show() }
        case ScanHelper.accessory:
            guard PermissionWindow.item(app: accessory, id: "fda") != nil else { return }      // 没有这一项时 show 直接当用户点了开启
            add(accessory) { PermissionWindow.show(app: accessory, id: "fda", enabling: true, done: { ScanHelper.enable { _ in } }) }
        default: break
        }
    }

    /// show 答开了没有（主窗口里另一块正忙着时没开，过一会儿再试）
    static func add(_ id: String, _ show: @escaping () -> Bool) {
        guard !queue.contains(where: { $0.id == id }) else { return }
        queue.append((id, show))
        pump()
        if timer == nil, !queue.isEmpty { timer = Timer.scheduledTimer(withTimeInterval: 2, repeats: true) { _ in pump() } }
    }

    private static var busy: Bool { FirstRun.isOpen || UpgradeWindow.isOpen || SearchActivation.isOpen || PermissionWindow.isOpen }

    private static func pump() {
        guard let next = queue.first else { timer?.invalidate(); timer = nil; return }
        guard NSApp.isActive, !busy, next.show() else { return }
        queue.removeFirst()
        markOffered([next.id])
        if queue.isEmpty { timer?.invalidate(); timer = nil }
    }
}
