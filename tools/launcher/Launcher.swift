// Python 配件的启动器（编译版）：作为 .app 的进程一直在，自带的 Python 跑清单里的入口当子进程。
// 比 shell 启动器多做三件事（luoke-mail 2026-10-13 提的）：
//   1. 用户再次打开已在运行的 .app（LaunchServices 的"再次打开"事件）：再跑一次入口并加 --reopen；
//   2. 以 .app 的名义发通知：<app>/Contents/MacOS/<启动器> --notify <标题> <副标题> <正文> [--open <参数>]，点通知等于再次打开，
//      参数经环境变量 LUOKE_OPEN_ARG 交给 --reopen 那次入口；退出码说明有没有交给系统（见下）；--notify-status 只报授权状态；
//   3. 主控登录时拉起常驻配件会传 --resident，原样转给入口；用户自己打开则没有。
// 子进程退出，启动器也退出（主控据此知道常驻配件没了）。收到 SIGTERM 先转给子进程。
import AppKit
import Foundation
import UserNotifications

let fm = FileManager.default
let bundle = Bundle.main
let resources = bundle.resourceURL!
let python = resources.appendingPathComponent("python/bin/python3").path
let manifestURL = resources.appendingPathComponent("luoke-accessory.json")

func entryArgs() -> [String] {
    guard let data = try? Data(contentsOf: manifestURL),
          let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
          let entry = obj["entry"] as? [String] else {
        FileHandle.standardError.write("启动器：清单里没有 entry\n".data(using: .utf8)!)
        exit(70)
    }
    return entry
}

func env(extra: [String: String] = [:]) -> [String: String] {
    var e = ["PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "HOME": NSHomeDirectory(), "PYTHONDONTWRITEBYTECODE": "1",
             "PYTHONNOUSERSITE": "1", "LUOKE_LAUNCHER": bundle.executablePath ?? ""]
    for (key, v) in ProcessInfo.processInfo.environment where key.hasPrefix("LUOKE_") || key == "LANG" || key == "TMPDIR" {
        e[key] = v    // 所有 LUOKE_ 开头的原样转给入口（配件的数据目录之类；`open --env` 时能指到数据副本上试）
    }
    e.merge(extra) { $1 }
    return e
}

func runEntry(extraArgs: [String], extraEnv: [String: String] = [:], wait: Bool) -> Process {
    let p = Process()
    p.executableURL = URL(fileURLWithPath: python)
    p.arguments = ["-B"] + entryArgs() + extraArgs
    p.environment = env(extra: extraEnv)
    p.currentDirectoryURL = URL(fileURLWithPath: "/")
    do { try p.run() } catch {
        FileHandle.standardError.write("启动器：跑不起来 \(python)：\(error)\n".data(using: .utf8)!)
        exit(71)
    }
    if wait { p.waitUntilExit() }
    return p
}

// ---- --notify：以 .app 的名义发一条通知，然后退出 ------------------------------------------------------------------
// 退出码（10-20 应 luoke-mail：之前不管显示没显示都是 0，配件查不出是没发出去还是系统不让显示）：
//   0 交给系统了；3 用户没允许通知（stderr 写 authorizationStatus）；4 系统拒收这条（stderr 写错误）；5 10 秒内系统没答复；64 用法不对。
// --notify-status：只打印一行 JSON {"authorization", "alert"}，不发通知、不请求授权。
let argv = CommandLine.arguments

func authName(_ s: UNAuthorizationStatus) -> String {
    switch s {
    case .notDetermined: return "not_determined"
    case .denied: return "denied"
    case .authorized: return "authorized"
    case .provisional: return "provisional"
    case .ephemeral: return "ephemeral"
    @unknown default: return "unknown"
    }
}

func settingName(_ s: UNNotificationSetting) -> String {
    switch s {
    case .notSupported: return "not_supported"
    case .disabled: return "disabled"
    case .enabled: return "enabled"
    @unknown default: return "unknown"
    }
}

func err(_ s: String) { FileHandle.standardError.write((s + "\n").data(using: .utf8)!) }

if argv.count >= 2 && argv[1] == "--notify-status" {
    let sem = DispatchSemaphore(value: 0)
    UNUserNotificationCenter.current().getNotificationSettings { st in
        print("{\"authorization\": \"\(authName(st.authorizationStatus))\", \"alert\": \"\(settingName(st.alertSetting))\"}")
        sem.signal()
    }
    if sem.wait(timeout: .now() + 10) == .timedOut { err("通知：10 秒内系统没答复"); exit(5) }
    exit(0)
}

if argv.count >= 2 && argv[1] == "--notify" {
    let args = Array(argv.dropFirst(2))
    guard args.count >= 3 else {
        err("用法：--notify <标题> <副标题> <正文> [--open <参数>]")
        exit(64)
    }
    var openArg = ""
    if let i = args.firstIndex(of: "--open"), i + 1 < args.count { openArg = args[i + 1] }
    let center = UNUserNotificationCenter.current()
    let sem = DispatchSemaphore(value: 0)
    var code: Int32 = 0
    center.requestAuthorization(options: [.alert, .sound]) { granted, _ in
        guard granted else {
            center.getNotificationSettings { st in
                err("通知：没允许（authorization \(authName(st.authorizationStatus))，alert \(settingName(st.alertSetting))）")
                code = 3
                sem.signal()
            }
            return
        }
        let content = UNMutableNotificationContent()
        content.title = args[0]
        content.subtitle = args[1]
        content.body = args[2]
        content.userInfo = ["open": openArg]
        let req = UNNotificationRequest(identifier: UUID().uuidString, content: content, trigger: nil)
        center.add(req) { e in
            if let e = e { err("通知：系统没收下（\(e.localizedDescription)）"); code = 4 }
            sem.signal()
        }
    }
    if sem.wait(timeout: .now() + 10) == .timedOut { err("通知：10 秒内系统没答复"); exit(5) }
    exit(code)
}

// ---- 正常启动：当 .app 的进程，入口当子进程 -----------------------------------------------------------------------------
final class Delegate: NSObject, NSApplicationDelegate, UNUserNotificationCenterDelegate {
    var child: Process?

    func applicationDidFinishLaunching(_ notification: Notification) {
        UNUserNotificationCenter.current().delegate = self
        let resident = argv.contains("--resident")
        let p = runEntry(extraArgs: resident ? ["--resident"] : [], wait: false)
        child = p
        p.terminationHandler = { proc in
            DispatchQueue.main.async { exit(proc.terminationStatus) }
        }
        signal(SIGTERM) { _ in
            // 先让子进程退，再自己退
            if let c = delegate.child, c.isRunning { c.terminate() }
            exit(0)
        }
    }

    /// 用户又打开了一次（程序坞、Finder、点通知）：再跑一次入口，加 --reopen。
    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        _ = runEntry(extraArgs: ["--reopen"], wait: false)
        return false
    }

    func userNotificationCenter(_ center: UNUserNotificationCenter, didReceive response: UNNotificationResponse,
                                withCompletionHandler completionHandler: @escaping () -> Void) {
        let arg = response.notification.request.content.userInfo["open"] as? String ?? ""
        _ = runEntry(extraArgs: ["--reopen"], extraEnv: arg.isEmpty ? [:] : ["LUOKE_OPEN_ARG": arg], wait: false)
        completionHandler()
    }

    func applicationWillTerminate(_ notification: Notification) {
        if let c = child, c.isRunning { c.terminate() }
    }
}

let app = NSApplication.shared
let delegate = Delegate()
app.delegate = delegate
app.setActivationPolicy(.prohibited)   // 没有界面，不进程序坞（LSUIElement 也在 plist 里）
app.run()
