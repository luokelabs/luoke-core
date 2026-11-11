// 螺壳邮件助手收进本体（ADR 0022）：代码在自带 Python 的 site-packages，清单在 Resources/mail，没有自己的 .app，也不放 Contents/Helpers
// （.py 放那里外层签名过不了）。本体用自带的 Python 起 `-m luoke_mail --resident --parent-pid`，它常驻，后台整理开不开是它自己的设置。
// 它不能自己发通知（没有自己的 bundle），也不知道打开页面的时机：两边经管道说话，一行一个 JSON（HelperProcess.onMessage）。
//   它发 {"op":"ready"}（页面起来了）、{"op":"not-starting"}（起不来，它自己在告诉用户）、{"id":n,"op":"notify","title","subtitle","body"}、{"id":n,"op":"notify-status"}
//   本体发 {"op":"open"}（打开页面）、{"id":n,...}（答）
import AppKit
import Foundation
import UserNotifications

enum MailHelper: BundledApp {
    static let accessory = "luoke-mail"
    static let bundleID = "ai.luoke.mail"
    private static let oldLauncherID = "local.luoke-mail.launcher"     // 从源码运行时代的 AppleScript 启动器
    private static let oldLoginItem = "local.luoke-mail"
    private static let clickKey = "luoke-accessory"
    private static var manifestURL: URL { AppInfo.resources.appendingPathComponent("mail/luoke-accessory.json") }

    private static var object: [String: Any]? {
        guard let data = try? Data(contentsOf: manifestURL) else { return nil }
        return try? JSONSerialization.jsonObject(with: data) as? [String: Any]
    }

    /// 清单。type 写 bundled：别处按 type == "app" 当 .app 处理的路子（打开、升级、通知）都走不到它
    static var manifest: Accessories.Manifest? {
        guard let o = object, Manifests.macOSSatisfies(o["min_macos"] as? String) else { return nil }
        return Accessories.Manifest(accessory: accessory, bundleID: bundleID, name: o["name"] as? String ?? accessory, resident: true,
                                    type: "bundled", app: Bundle.main.bundleURL, version: o["version"] as? String ?? "", bundled: true)
    }

    /// 装好的那份螺壳（和主控 accessories._is_installed_hub 同一个判断）用真实的 ~/.luoke-mail，别的副本用自己的数据目录，
    /// 主控的空闲任务和卸载命令也是这么算的：两边不一致，删的就不是这里用的那份
    private static var installedCopy: Bool {
        let home = FileManager.default.homeDirectoryForCurrentUser.path
        let me = Bundle.main.bundleURL.resolvingSymlinksInPath().path
        return ["/Applications", home + "/Applications"].contains { dir in
            ["螺壳.app", "螺壳主控.app"].contains { URL(fileURLWithPath: dir + "/" + $0).resolvingSymlinksInPath().path == me }
        }
    }

    /// 它自己的日志：起来以后说的话都进这里（luoke_mail 的 log_to_file），logs/mail.log 只有起来之前的
    static var serviceLog: URL {
        let home = FileManager.default.homeDirectoryForCurrentUser
        return (installedCopy ? home.appendingPathComponent(".luoke-mail") : home.appendingPathComponent("Library/Application Support/luoke-mail"))
            .appendingPathComponent("service.log")
    }

    private static func wanted() -> Bool {
        guard BundledApps.developmentBuild || !AppInfo.isLab else { return false }     // 正式包设了 LUOKE_LAB：不碰真实数据
        return manifest != nil && enabled
    }

    private static let process: HelperProcess = {
        var env = ["PATH": "/usr/bin:/bin", "HOME": NSHomeDirectory(), "PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1",
                   "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1", "LUOKE_HUB_SOCKET": AppInfo.socketPath]
        if let lh = ProcessInfo.processInfo.environment["LUOKE_HOME"] { env["LUOKE_HOME"] = lh }      // 和主控、空闲任务同一份记忆
        if !installedCopy { env["LUOKE_MAIL_HOME"] = NSHomeDirectory() + "/Library/Application Support/luoke-mail" }
        let p = HelperProcess(program: URL(fileURLWithPath: AppInfo.python), arguments: ["-B", "-m", "luoke_mail", "--resident", "--parent-pid", String(getpid())],
                              environment: env, logFile: AppInfo.envDir.appendingPathComponent("logs/mail.log"))
        p.marker = "luoke_mail"                  // 自带的 Python 主控和 agent 核心也在用
        p.options.graceSeconds = 8
        p.wanted = wanted
        p.onMessage = handle
        p.onExit = { ready = false }
        return p
    }()

    static func start() {
        guard manifest != nil else { return }
        if oldAppGone { process.start(); return }
        guard !retiring else { return }
        retiring = true
        DispatchQueue.global().async {
            retireOldApp()
            DispatchQueue.main.async {
                oldAppGone = true
                retiring = false
                process.start()
            }
        }
    }

    private static var retiring = false
    private static var oldAppGone = false

    /// 收进来以前的 螺壳邮件助手.app，和更早留下的登录项、通知用的小 App：都走掉才起这一份，不然两份抢同一个数据目录。
    /// ~/.luoke-mail 里的数据和钥匙串里的授权码不动，这一份接着用。登录项最先：它带 KeepAlive，进程结束了 launchd 还会拉起来。
    /// 用户停用了也做（旧的那份不该留着跑）
    private static func retireOldApp() {
        guard !BundledApps.developmentBuild, !AppInfo.isLab else { return }
        let fm = FileManager.default, home = fm.homeDirectoryForCurrentUser
        let plist = home.appendingPathComponent("Library/LaunchAgents/\(oldLoginItem).plist")
        if fm.fileExists(atPath: plist.path) {
            Installer.shell("/bin/launchctl", ["bootout", "gui/\(getuid())/\(oldLoginItem)"])
            try? fm.removeItem(at: plist)
        }
        BundledApps.retireApps([bundleID, oldLauncherID], label: "螺壳邮件助手", grace: 15)
        let notifier = home.appendingPathComponent(".luoke-mail/notifier")
        for app in (try? fm.contentsOfDirectory(at: notifier, includingPropertiesForKeys: nil)) ?? [] { AppPlacement.unregister(app) }
        try? fm.removeItem(at: notifier)
    }

    static func stopAll(hubApp: URL) {
        let python = hubApp.appendingPathComponent("Contents/Resources/python/bin/python3").resolvingSymlinksInPath()
        let n = HelperProcess.clearStale(path: python.path, name: python.lastPathComponent, marker: "luoke_mail", grace: 8)
        if n > 0 { NSLog("螺壳邮件助手：换装前结束了 %d 个进程", n) }
    }

    static func stop() {
        guard object != nil else { return }
        process.stop()
    }

    // ---- 打开页面：「应用」窗口、菜单栏、点了通知。令牌和端口只在它那边，这里只说一声 ----
    static var opens: Bool { true }
    private static var ready = false
    private static var opening = false

    /// 带主窗口的这一份翻到主窗口里邮件助手的页，不开浏览器（11-10：只有一个窗口）
    static func open() {
        guard manifest != nil, enabled else { AppsWindow.show(); return }
        if MainWindow.presenter != nil { MainWindow.openApp(accessory); return }
        if ready { process.send(["op": "open"]); return }
        opening = true
        start()
        DispatchQueue.main.asyncAfter(deadline: .now() + 30) {
            guard opening else { return }
            opening = false
            let a = NSAlert()
            a.messageText = "螺壳邮件助手没有打开"
            a.informativeText = "请稍后再试。"
            a.runModal()
        }
    }

    /// 点了的通知是它的：打开页面，答 true；不是的答 false
    static func notificationClicked(_ info: [AnyHashable: Any]) -> Bool {
        guard info[clickKey] as? String == accessory else { return false }
        open()
        return true
    }

    // ---- 它说的话 ----
    private static func handle(_ m: [String: Any]) {
        let id = m["id"] as? Int
        func answer(_ o: [String: Any]) {
            guard let id else { return }
            DispatchQueue.main.async { process.send(o.merging(["id": id]) { a, _ in a }) }
        }
        switch m["op"] as? String {
        case "ready":
            ready = true
            if opening { opening = false; process.send(["op": "open"]) }
        case "not-starting":                     // 它弹着框说明原因，用户再看到「没有打开，请稍后再试」就是误导
            opening = false
        case "notify":
            post(title: m["title"] as? String ?? "", subtitle: m["subtitle"] as? String ?? "", body: m["body"] as? String ?? "") { answer(["result": $0]) }
        case "notify-status":
            UNUserNotificationCenter.current().getNotificationSettings { s in
                answer(["authorization": authorization(s.authorizationStatus), "alert": alert(s.alertSetting)])
            }
        default:
            break
        }
    }

    /// 以螺壳的名义发。邮件是后台发的，这里不问授权：还没问过、没让，都答 not allowed
    private static func post(title: String, subtitle: String, body: String, done: @escaping (String) -> Void) {
        let center = UNUserNotificationCenter.current()
        center.getNotificationSettings { s in
            guard [.authorized, .provisional].contains(s.authorizationStatus) else { done("not allowed"); return }
            let c = UNMutableNotificationContent()
            c.title = title
            c.subtitle = subtitle
            c.body = body
            c.userInfo = [clickKey: accessory]
            center.add(UNNotificationRequest(identifier: UUID().uuidString, content: c, trigger: nil)) { done($0 == nil ? "posted" : "failed") }
        }
    }

    private static func authorization(_ s: UNAuthorizationStatus) -> String {
        switch s {
        case .notDetermined: return "not_determined"
        case .denied: return "denied"
        case .authorized: return "authorized"
        case .provisional: return "provisional"
        case .ephemeral: return "ephemeral"
        @unknown default: return "not_determined"
        }
    }

    private static func alert(_ s: UNNotificationSetting) -> String {
        switch s {
        case .enabled: return "enabled"
        case .disabled: return "disabled"
        default: return "not_supported"
        }
    }
}
