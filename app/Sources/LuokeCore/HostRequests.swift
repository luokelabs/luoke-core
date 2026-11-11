// 工具经描述符 3 请本体代做的「叫到前面」的事（ScanElevate.listen 收，扫描仓库 docs/host-interface.md §3a）。
// macOS 只让当前在前面的应用把别的应用叫到前面：用户在螺壳里点了「在访达里显示」「打开设置」，在前面的是螺壳，
// 工具是后台程序，自己跑 `open -R`、`open <地址>` 能开出窗口，但访达、系统设置不一定到前面，会被螺壳挡住。所以由本体来做：
// - `reveal`：`{"path": "/绝对路径"}`，在访达里选中它；
// - `open_settings`：`{"pane": "Privacy_AllFiles"}`，打开系统设置的那一页。只认下面登记的几页，地址由本体拼，不收工具给的地址。
// 都只紧跟在用户的点按后面做（UserAction.recent：螺壳在最前、刚点过）；不是就答 failed，工具自己退回 `open`。
// 答 `{"status": "done"}` 或 `{"status": "failed", "why": "…"}`。
import AppKit
import Foundation

enum HostRequests {
    /// 系统设置的页：工具说页名，本体拼地址（和扫描原来自己 open 的地址一样）
    static let settingsPanes: [String: String] = {
        var m: [String: String] = [:]
        // Privacy：隐私与安全性本身（没有自己那一页的权限，如蓝牙、App 管理，从这里点进去）
        for p in ["Privacy_AllFiles", "Privacy_Accessibility", "Privacy_ScreenCapture", "Privacy_ListenEvent", "Privacy_FilesAndFolders",
                  "Privacy_Camera", "Privacy_Microphone", "Privacy_Automation", "Privacy_Contacts", "Privacy_Calendars",
                  "Privacy_Reminders", "Privacy_Photos", "Privacy_SpeechRecognition", "Privacy", "FDE"] {
            m[p] = "x-apple.systempreferences:com.apple.preference.security?" + p
        }
        m["LoginItems"] = "x-apple.systempreferences:com.apple.LoginItems-Settings.extension"
        return m
    }()

    /// 真去做的那几样；测试换成假的，不开访达、不开系统设置
    struct Env {
        var recent: () -> Bool
        var exists: (String) -> Bool
        var reveal: (URL) -> Void
        var open: (URL) -> Bool
        /// 在主线程上做，最多等这么久（主线程忙着就答 failed，不让工具一直等）
        var wait: TimeInterval

        static let live = Env(
            recent: { UserAction.recent() },
            exists: { FileManager.default.fileExists(atPath: $0) },
            reveal: { NSWorkspace.shared.activateFileViewerSelecting([$0]) },
            open: { NSWorkspace.shared.open($0) },
            wait: 10)
    }

    static func failed(_ why: String) -> [String: Any] { ["status": "failed", "why": why] }
    static let done: [String: Any] = ["status": "done"]

    /// 要选中的路径：存在的绝对路径，不带换行和 NUL，不超过 PATH_MAX
    static func revealPath(_ params: [String: Any], exists: (String) -> Bool) -> String? {
        guard let path = params["path"] as? String, path.hasPrefix("/"), path.utf8.count < Int(PATH_MAX),
              !path.contains("\n"), !path.contains("\r"), !path.contains("\0"), exists(path) else { return nil }
        return path
    }

    static func reveal(_ params: [String: Any], env: Env = .live) -> [String: Any] {
        guard let path = revealPath(params, exists: env.exists) else { return failed("这个位置不在了，或者不是绝对路径") }
        return onMain(env) {
            guard env.recent() else { return failed("螺壳不在最前，或者用户没刚点过") }
            env.reveal(URL(fileURLWithPath: path))
            return done
        }
    }

    static func openSettings(_ params: [String: Any], env: Env = .live) -> [String: Any] {
        guard let pane = params["pane"] as? String, let s = settingsPanes[pane], let url = URL(string: s) else {
            return failed("不认识这个设置页")
        }
        return onMain(env) {
            guard env.recent() else { return failed("螺壳不在最前，或者用户没刚点过") }
            return env.open(url) ? done : failed("系统设置没打开")
        }
    }

    /// 在主线程上做完再答；监听描述符 3 的是后台线程，主线程上没有等它的地方，但还是限时
    private static func onMain(_ env: Env, _ work: @escaping () -> [String: Any]) -> [String: Any] {
        if Thread.isMainThread { return work() }
        let sem = DispatchSemaphore(value: 0)
        var result: [String: Any]?
        DispatchQueue.main.async { result = work(); sem.signal() }
        guard sem.wait(timeout: .now() + env.wait) == .success, let result else { return failed("螺壳这会儿忙，没来得及做") }
        return result
    }
}
