// 螺壳扫描收进本体（顶层 ADR 0022 的 11-05 修订；扫描 0.3.0 起）：程序在 Contents/Helpers（luoke-scan、luoke-scan-admin，和它起的
// clamscan、freshclam、sigtool、yara），库在 Contents/Frameworks，清单在 Resources/scan。跟螺壳一起升级、一起签名。
// 不常驻：agent 要用时由 ToolHost 起 `luoke-scan mcp --this-mac`，空闲任务由主控经 `螺壳 --run-tool` 起（ToolLauncher）。
// 默认关着，用户在「应用」里开启了才起（两处都看 enabled）。
// 这一份没带它（开发版没给 LUOKE_SCAN_SRC）时 manifest 是 nil，这里什么都不做。
import Foundation

enum ScanHelper: BundledApp {
    static let accessory = "luoke-scan"
    static let bundleID = "ai.luoke.scan"
    /// 扫描用的、在 Helpers 里的程序（换装前按路径结束；luoke-scan-admin 以 root 跑，不归这里）
    static let programs = ["luoke-scan", "clamscan", "freshclam", "sigtool", "yara"]
    private static var manifestURL: URL { AppInfo.resources.appendingPathComponent("scan/luoke-accessory.json") }

    private static var object: [String: Any]? {
        guard let data = try? Data(contentsOf: manifestURL) else { return nil }
        return try? JSONSerialization.jsonObject(with: data) as? [String: Any]
    }

    /// 清单。type 写 bundled：别处按 type == "app" 当 .app 处理的路子（打开、升级、常驻）都走不到它
    static var manifest: Accessories.Manifest? {
        guard HostedTool.find(accessory) != nil, let o = object, Manifests.macOSSatisfies(o["min_macos"] as? String) else { return nil }
        return Accessories.Manifest(accessory: accessory, bundleID: bundleID, name: o["name"] as? String ?? accessory,
                                    resident: false, type: "bundled", app: Bundle.main.bundleURL,
                                    version: o["version"] as? String ?? "", bundled: true)
    }

    /// 主控起来以后：beta 1 单独装的 螺壳扫描.app（同一个 bundle id）结束、挪进废纸篓，数据不动，这一份接着用。只做一次，
    /// 用户没开启也做。开发版、实验室不动：本机装着的不归它们管
    static func start() {
        guard manifest != nil, !retiredOld, !BundledApps.developmentBuild, !AppInfo.isLab else { return }
        retiredOld = true
        DispatchQueue.global().async { BundledApps.retireApps([bundleID], label: "螺壳扫描", grace: 15) }
    }
    private static var retiredOld = false

    /// 不常驻，没有要停的：已经起的（agent 的一次请求、一次空闲任务）照常做完，停用以后不再起
    static func stop() {}

    /// 换装前（Installer）：这份程序不是这个进程起的，按路径认
    static func stopAll(hubApp: URL) {
        var n = 0
        for name in programs {
            let program = hubApp.appendingPathComponent("Contents/Helpers/\(name)")
            n += HelperProcess.clearStale(path: program.resolvingSymlinksInPath().path, name: name, grace: 5)
        }
        if n > 0 { NSLog("螺壳扫描：换装前结束了 %d 个进程", n) }
    }
}
