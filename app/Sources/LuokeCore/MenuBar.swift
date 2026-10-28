import AppKit
import Combine
import SwiftUI

/// 菜单栏：单色海螺图标 + 菜单（luoke-brand ui/DESIGN.md §12，luoke-core#15）。
/// 用 NSStatusItem 而不是 SwiftUI MenuBarExtra：菜单要左边写是什么、右边右对齐写现在的值，MenuBarExtra 的菜单样式做不到。
/// 图标五种、都不动（10-23）：见 HubModel.lightState。
final class MenuBar: NSObject, NSMenuDelegate {
    static let shared = MenuBar()
    private var item: NSStatusItem?
    private let menu = NSMenu()
    private var subs: Set<AnyCancellable> = []
    private var shownIcon = ""
    private var menuOpen = false
    private var model: HubModel { HubModel.shared }

    func install() {
        let it = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        it.button?.toolTip = "螺壳"
        menu.delegate = self
        menu.autoenablesItems = false
        it.menu = menu
        item = it
        model.$status.combineLatest(model.$hubError, model.$updateFound).receive(on: RunLoop.main)
            .sink { [weak self] _ in self?.statusChanged() }.store(in: &subs)
        statusChanged()
    }

    private func statusChanged() {
        setIcon(model.lightState)
        if menuOpen { rebuild() }
    }

    // MARK: 图标


    private func setIcon(_ state: String) {
        guard state != shownIcon else { return }
        shownIcon = state
        item?.button?.image = Self.icon(state)
    }

    private static var iconCache: [String: NSImage] = [:]

    /// 18×18 pt 单色模板图，1x 和 @2x 两张都放进去，系统随菜单栏深浅染色
    static func icon(_ name: String) -> NSImage? {
        if let i = iconCache[name] { return i }
        let img = NSImage(size: NSSize(width: 18, height: 18))
        for suffix in ["", "@2x"] {
            if let rep = LKAsset.image("menubar/\(name)\(suffix).png")?.representations.first {
                rep.size = NSSize(width: 18, height: 18)
                img.addRepresentation(rep)
            }
        }
        guard !img.representations.isEmpty else { return NSImage(systemSymbolName: "circle", accessibilityDescription: "螺壳") }
        img.isTemplate = true
        img.accessibilityDescription = "螺壳"
        iconCache[name] = img
        return img
    }

    // MARK: 菜单

    func menuNeedsUpdate(_ menu: NSMenu) { rebuild() }
    func menuWillOpen(_ menu: NSMenu) { menuOpen = true }
    func menuDidClose(_ menu: NSMenu) { menuOpen = false }

    private func rebuild() {
        menu.removeAllItems()
        let m = model
        // 出错时写原因，给重启和导出两条路
        if m.unreachable {
            add(text("出错了"))
            add(text(m.hubExit.map { "螺壳进程退出了（\($0)）" } ?? m.hubError ?? "连不上螺壳进程"))
            errorActions()
        } else if let st = m.status {
            if st.state == .error {
                add(text("出错了"))
                add(text(st.breaker ?? "引擎没在运行"))
                errorActions()
            } else {
                add(text(st.state == .starting ? st.stateValue : "\(st.stateValue) · \(ModelName.display(st.loaded ?? st.base ?? "—"))"))
                if let p = st.backgroundPaused { add(text(p)) }
            }
        } else {
            add(text("启动中"))
        }
        menu.addItem(.separator())
        if Memory.bundled { add(action("记忆") { Memory.open() }) }
        if AppsWindow.updatable().isEmpty {
            add(action("应用") { AppsWindow.show() })
        } else {
            add(row("应用", "有新版本", bold: true) { AppsWindow.show() })
        }
        add(action("设置") { SettingsWindow.show() })
        if let f = m.updateFound {
            add(row("检查更新", "有新版本", bold: true) { Updater.show(found: f) })
        } else {
            add(action("检查更新") { Updater.show() })
        }
        menu.addItem(.separator())
        let quit = action("退出螺壳") { NSApp.terminate(nil) }
        quit.keyEquivalent = "q"
        add(quit)
    }

    private func add(_ i: NSMenuItem) { menu.addItem(i) }

    private func errorActions() {
        add(action("重新启动螺壳") { HubModel.shared.restartHub() })
        add(action("导出诊断信息") { Diagnostics.export(model: HubModel.shared) })
    }

    /// 左边是什么、右边右对齐的值（灰；有新版本时加粗、不用红色）
    /// 「名称：值」左对齐（10-22 定，brand DESIGN.md §12：右对齐的值停在系统给子菜单箭头留的那一列左边，和箭头之间空一大截）。
    /// 名称正常颜色，值灰色；只有「有新版本」加粗、用正常颜色
    private func row(_ label: String, _ value: String, bold: Bool = false, run: (() -> Void)? = nil) -> NSMenuItem {
        let i = run.map { action(label, $0) } ?? NSMenuItem(title: label, action: nil, keyEquivalent: "")
        let font = NSFont.menuFont(ofSize: 0)
        let s = NSMutableAttributedString(string: label + "：", attributes: [.font: font])
        s.append(NSAttributedString(string: value, attributes: [
            .font: bold ? NSFont.boldSystemFont(ofSize: font.pointSize) : font,
            .foregroundColor: bold ? NSColor.labelColor : NSColor.secondaryLabelColor,
        ]))
        i.attributedTitle = s
        i.isEnabled = true          // 只读的行也不灰：值要看得清；没有 action，点了什么都不做
        return i
    }

    /// 只读的一行：系统的不可点样式（灰），点不了就该看起来点不了
    private func text(_ t: String) -> NSMenuItem {
        let i = NSMenuItem(title: t, action: nil, keyEquivalent: "")
        i.isEnabled = false
        return i
    }

    private func action(_ title: String, _ run: @escaping () -> Void) -> NSMenuItem {
        let i = NSMenuItem(title: title, action: #selector(Handler.fire(_:)), keyEquivalent: "")
        let h = Handler(run)
        i.target = h
        i.representedObject = h        // 菜单项持有 handler（target 是弱引用）
        return i
    }

    private final class Handler: NSObject {
        let run: () -> Void
        init(_ run: @escaping () -> Void) { self.run = run }
        @objc func fire(_ sender: Any?) { run() }
    }
}

/// 设置窗口：菜单栏不再是 SwiftUI 场景，SettingsLink 用不了，自己开一个窗口放 SettingsView
enum SettingsWindow {
    static var window: NSWindow?

    static func show() {
        if let w = window, w.isVisible { Uninstall.bringFront(w); return }
        let host = NSHostingController(rootView: SettingsView().environmentObject(HubModel.shared))
        let w = NSWindow(contentViewController: host)
        w.title = "螺壳设置"
        w.styleMask = [.titled, .closable]
        w.center()
        w.isReleasedWhenClosed = false
        window = w
        AppWindows.track(w)
        Uninstall.bringFront(w)
    }
}
