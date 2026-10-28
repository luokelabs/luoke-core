import AppKit
import SwiftUI
import UserNotifications

/// 主控平时只在菜单栏（LSUIElement）。安装、升级、加装、卸载、检查更新这些窗口要用户盯好几分钟，切走以后没有回来的路
/// （luoke-core#20：⌘Tab 和程序坞里都没有）。所以这些窗口开着的时候临时当普通 App，全关了再改回只在菜单栏。
enum AppWindows {
    static func track(_ w: NSWindow) {
        NSApp.setActivationPolicy(.regular)
        var token: NSObjectProtocol?
        token = NotificationCenter.default.addObserver(forName: NSWindow.willCloseNotification, object: w, queue: .main) { _ in
            if let t = token { NotificationCenter.default.removeObserver(t) }     // 每个窗口只看一次关闭，看完就撤（之前从不移除）
            DispatchQueue.main.async { refresh() }     // willClose 时窗口还算开着：下一轮再看
        }
    }

    static func refresh() {
        NSApp.setActivationPolicy(visible().isEmpty ? .accessory : .regular)
    }

    private static func visible() -> [NSWindow] {
        NSApp.windows.filter { $0.isVisible && $0.styleMask.contains(.titled) }
    }

    /// 再次打开主控（访达、启动台、程序坞）时：有开着的窗口就叫到前面。叫回了答 true
    static func bringBack() -> Bool {
        guard let w = visible().first else { return false }
        Uninstall.bringFront(w)
        return true
    }
}

/// 再次打开主控、又没有开着的窗口时弹的小窗口（luoke-core#19）：菜单栏图标认不出、被挤掉或被关掉时，
/// 卸载、记忆、检查更新这些只在菜单里的功能也有一条路。
enum HomeWindow {
    static var window: NSWindow?

    static func show() {
        if let w = window, w.isVisible { Uninstall.bringFront(w); return }
        let host = NSHostingController(rootView: HomeView(model: HubModel.shared))
        let w = NSWindow(contentViewController: host)
        w.title = "螺壳"
        w.styleMask = [.titled, .closable]
        w.setContentSize(NSSize(width: 480, height: 520))
        w.center()
        w.isReleasedWhenClosed = false
        window = w
        AppWindows.track(w)
        Uninstall.bringFront(w)
    }

    static func openMenuBarSettings() {
        if let u = URL(string: "x-apple.systempreferences:com.apple.ControlCenter-Settings.extension") { NSWorkspace.shared.open(u) }
    }
}

struct HomeView: View {
    @ObservedObject var model: HubModel

    var body: some View {
        LKPage {
            HStack(spacing: 14) {
                LKAppIcon(accessory: "luoke-core", size: 48)
                VStack(alignment: .leading, spacing: 4) {
                    Text("螺壳").font(.system(size: 20, weight: .semibold))
                    HStack(spacing: 6) {
                        if model.status != nil { LKBadge(text: "在运行", kind: .success) }
                        else { LKBadge(text: model.hubError == nil ? "启动中" : "没连上", kind: model.hubError == nil ? .secondary : .warning) }
                    }
                }
            }
            .padding(.bottom, 16)
            LKNote(icon: "menubar.rectangle", text: "螺壳在菜单栏的海螺图标里。找不到的话：图标太多时系统会把放不下的藏起来，也可能在系统设置的菜单栏里被关掉了。")
            Button { HomeWindow.openMenuBarSettings() } label: { Label("打开菜单栏设置", systemImage: "arrow.up.right.square") }
                .buttonStyle(LKSecondaryButton()).padding(.top, 10)
            // 菜单栏图标找不到时，菜单里的事在这里也做得了（luoke-core#19；10-24 luoke-top 核出 #26 之后只剩说明）
            LKGroup {
                let rows: [(String, String, () -> Void)] = (Memory.bundled ? [("brain", "记忆", { Memory.open() })] : []) + [
                    ("square.grid.2x2", "应用", { AppsWindow.show() }),
                    ("arrow.triangle.2.circlepath", "检查更新", { Updater.show() }),
                    ("gearshape", "设置", { SettingsWindow.show() }),
                ]
                ForEach(Array(rows.enumerated()), id: \.offset) { i, r in
                    if i > 0 { Divider().overlay(Color.lkBorder) }
                    Button { HomeWindow.window?.close(); r.2() } label: {
                        LKSettingRow(icon: r.0, title: r.1) { Image(systemName: "chevron.right").font(.system(size: 12)).foregroundStyle(Color.lkMuted) }
                            .contentShape(Rectangle())
                    }
                    .buttonStyle(.plain)
                }
            }
            .padding(.top, 20)
        } footer: {
            Spacer()
            Button("完成") { HomeWindow.window?.close() }.buttonStyle(LKPrimaryButton())
        }
        .frame(width: 480, height: 520)
    }
}

/// 安装器换包期间的卡片：和检查更新窗口同样大小、同样的五步，做到第三步「换成新版螺壳」。
/// 换包没成也在这张卡片上说（10-23：同一个流程只出现一个窗口），按钮和别的失败一样：先用着（打开原来那份）、重试（再换一次）
final class InstallerProgressModel: ObservableObject {
    let title: String
    let text: String
    @Published var failed: String?
    @Published var askSettings = false      // 是「App 管理」权限挡住的：主按钮是「打开系统设置」
    @Published var openedSettings = false   // 开过设置了：主按钮换成「重试」
    var retry: () -> Void = {}
    var later: () -> Void = { NSApp.terminate(nil) }
    init(title: String, text: String) { self.title = title; self.text = text }
}

struct InstallerProgressView: View {
    @ObservedObject var m: InstallerProgressModel
    var body: some View {
        if let why = m.failed {
            UpgradeCard(title: UpgradeSteps.failedTitle) {
                UpgradeProgress(current: 2, failed: why, diagnose: !m.askSettings)
            } footer: {
                Spacer()
                Button("先用着") { m.later() }.buttonStyle(LKSecondaryButton(ghost: true))
                if m.askSettings && !m.openedSettings {
                    // 是系统设置的问题，不是重试的问题：先去开权限；开过设置回来再给「重试」
                    Button("打开系统设置") { InstallerProgress.openAppManagement(); m.openedSettings = true }
                        .buttonStyle(LKPrimaryButton()).keyboardShortcut(.defaultAction)
                } else {
                    Button { m.failed = nil; m.retry() } label: { Label("重试", systemImage: "arrow.clockwise") }
                        .buttonStyle(LKPrimaryButton()).keyboardShortcut(.defaultAction)
                }
            }
        } else {
            UpgradeCard(title: m.title) {
                UpgradeProgress(current: 2, line: m.text)
            } footer: {
                Spacer()
                Button("升级中") {}.buttonStyle(LKPrimaryButton()).disabled(true)
            }
        }
    }
}

/// 安装器换包期间的卡片（旧主控已退、新主控未起的那一分钟；10-21 负责人用「检查更新…」升级时屏幕上什么都没有）。
/// 交给新主控时安装器自己退出，窗口跟着没了；新主控在同一位置接着显示同样的卡片（UpgradeWindow）。
enum InstallerProgress {
    static var window: NSWindow?
    static var model: InstallerProgressModel?

    static func show(title: String, text: String) {
        let m = InstallerProgressModel(title: title, text: text)
        let host = NSHostingController(rootView: InstallerProgressView(m: m))
        let w = NSWindow(contentViewController: host)
        w.title = "检查更新"
        w.styleMask = [.titled]
        w.setContentSize(NSSize(width: 560, height: 460))
        w.center()
        w.isReleasedWhenClosed = false
        window = w
        model = m
        AppWindows.track(w)
        Uninstall.bringFront(w)
    }

    /// 换包没成：卡片停在第三步写原因，给「先用着」「重试」（或「打开系统设置」）。卡片没开着（不是从升级来的）就返回 false
    static func openAppManagement() {
        if let u = URL(string: "x-apple.systempreferences:com.apple.settings.PrivacySecurity.extension?Privacy_AppBundles") { NSWorkspace.shared.open(u) }
    }

    static func fail(_ why: String, askSettings: Bool = false) -> Bool {
        guard let m = model, let w = window else { return false }
        m.failed = why
        m.askSettings = askSettings
        m.openedSettings = false
        Uninstall.bringFront(w)
        return true
    }

    static func close() { window?.close(); window = nil }
}

/// 点了主控以前发的通知（10-23 起首次安装、加装、升级都不再发通知，结果只在窗口里）：有开着的窗口就叫回来，没有就开主控小窗口
enum DoneAttention {
    static func clicked(_ info: [AnyHashable: Any]) {
        if let w = FirstRun.window, w.isVisible { Uninstall.bringFront(w); return }
        if let tag = info["notes"] as? String { ReleaseNotesWindow.show(tag: tag); return }
        if !AppWindows.bringBack() { HomeWindow.show() }
    }
}

/// 「这一版有什么不同」的小窗口：点了升级通知、完成页已经关了时打开（和升级页、检查更新窗口用同一份发布页的更新说明）
enum ReleaseNotesWindow {
    static var window: NSWindow?

    static func show(tag: String) {
        window?.close()
        let host = NSHostingController(rootView: ReleaseNotesView(tag: tag))
        let w = NSWindow(contentViewController: host)
        w.title = "螺壳 \(tag)"
        w.styleMask = [.titled, .closable]
        w.setContentSize(NSSize(width: 480, height: 420))
        w.center()
        w.isReleasedWhenClosed = false
        window = w
        AppWindows.track(w)
        Uninstall.bringFront(w)
    }
}

struct ReleaseNotesView: View {
    let tag: String
    @State private var notes: String?

    var body: some View {
        LKPage {
            HStack(spacing: 14) {
                LKAppIcon(accessory: "luoke-core", size: 48)
                VStack(alignment: .leading, spacing: 4) {
                    Text("螺壳已升级到 \(tag)").font(.system(size: 20, weight: .semibold))
                }
            }
            .padding(.bottom, 16)
            if let n = notes {
                if n.isEmpty { LKNote(icon: "wifi.exclamationmark", text: "这一版的更新说明没取到（连不上发布页）。可以稍后在菜单的「检查更新」里再看。") }
                else { LKReleaseNotes(text: n, maxHeight: 240) }
            } else {
                ProgressView().controlSize(.small)
            }
        } footer: {
            Spacer()
            Button("完成") { ReleaseNotesWindow.window?.close() }.buttonStyle(LKPrimaryButton())
        }
        .frame(width: 480, height: 420)
        .onAppear {
            DispatchQueue.global().async {
                let n = Updater.notes(for: tag)
                DispatchQueue.main.async { notes = n }
            }
        }
    }
}
