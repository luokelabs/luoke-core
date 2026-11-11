// 升级螺壳从头到尾是同一张卡片（10-23：同一个流程只出现一个窗口，不要小窗、不要左栏）：
// 「检查更新」窗口里下载、核对 → 安装器换包时显示同样大小、同一位置的卡片（InstallerProgress）→ 新版起来后接着显示同样的卡片，
// 把推理引擎、模型补齐到这一版要的、在本机试跑一次，最后在卡片上写「已升级到 X」和这一版的更新说明。
// 三段分属三个进程（旧版、安装器、新版），卡片的样子和步骤是同一份（UpgradeCard、UpgradeSteps），看起来就是一个窗口。
// 出错时卡片停住，写清哪一步、为什么，给「先用着」「重试」（没升级成也就没有「退回上一版」）。
// 应用不在这里升级（早会 #13）：有新版的在「应用」窗口里单独升。应用的安装器顺带升级了螺壳的话，升级完到「应用」窗口那一行里接着装。
import AppKit
import Combine
import SwiftUI

/// 升级的五步：前两步在检查更新窗口里，第三步在安装器里，后两步在新版里
enum UpgradeSteps {
    static let names = ["下载安装包", "核对签名和公证", "换成新版螺壳", "补齐推理引擎和模型", "本机试跑"]
    /// 哪一步没成，卡片标题都是这一句；按钮都是「先用着」「重试」（10-23）
    static let failedTitle = "升级未完成"
}

/// 升级卡片（检查更新窗口也是它）：560×460，图标 + 标题和底栏不动，中间的正文（进度和步骤，或更新说明）放不下就滚。
/// 窗口不能调大小，升级完成页连关闭按钮都没有，所以按钮不能被正文挤出去
struct UpgradeCard<Content: View, Footer: View>: View {
    let title: String
    var sub: String = ""
    @ViewBuilder let content: () -> Content
    @ViewBuilder let footer: () -> Footer
    @Environment(\.lkHost) private var host
    @Environment(\.lkTopInset) private var topInset

    var body: some View {
        if host == .page {
            // 主窗口里的「检查更新」那一页：页标题、滚动是主窗口的；这里的标题是现在的状态（「已经是最新的」……），
            // 没有要说的状态（标题是空的）就不画这一行，下面的内容顶上来
            VStack(alignment: .leading, spacing: 0) {
                if !title.isEmpty { Text(title).font(.system(size: 17, weight: .semibold)) }
                if !sub.isEmpty { Text(sub).font(.system(size: 14)).foregroundStyle(Color.lkMuted).fixedSize(horizontal: false, vertical: true).padding(.top, title.isEmpty ? 0 : 4) }
                VStack(alignment: .leading, spacing: 0) { content() }.padding(.top, title.isEmpty && sub.isEmpty ? 0 : 16)
                HStack(spacing: 8) { footer() }.padding(.top, 20)
            }
            .frame(maxWidth: .infinity, alignment: .topLeading)
            .foregroundStyle(Color.lkForeground)
        } else {
            card
        }
    }

    private var card: some View {
        VStack(spacing: 0) {
            HStack(alignment: sub.isEmpty ? .center : .top, spacing: 14) {
                if let icon = LKAsset.image("app-core.png") { Image(nsImage: icon).resizable().frame(width: 40, height: 40) }
                VStack(alignment: .leading, spacing: 4) {
                    Text(title).font(.system(size: 20, weight: .semibold))
                    if !sub.isEmpty { Text(sub).font(.system(size: 14)).foregroundStyle(Color.lkMuted).fixedSize(horizontal: false, vertical: true) }
                }
                Spacer()
            }
            .padding(.horizontal, 32).padding(.top, max(28, topInset + 8)).padding(.bottom, 20)     // 占整个主窗口：图标在红黄绿下面
            ScrollView {
                VStack(alignment: .leading, spacing: 0) { content() }
                    .frame(maxWidth: .infinity, alignment: .topLeading)
                    .padding(.horizontal, 32).padding(.bottom, 12)
            }
            .scrollBounceBehavior(.basedOnSize)      // 放得下时不让进度页跟着手势晃
            HStack(spacing: 8) { footer() }
                .padding(.horizontal, 32).frame(height: 64)
                .overlay(alignment: .top) { Rectangle().fill(Color.lkBorder).frame(height: 1) }
        }
        .background(Color.lkBackground).foregroundStyle(Color.lkForeground)
        .lkWindowSize(width: 560, height: 460)
    }
}

/// 进度条 + 五步（current 那一步在做；failed 有值就是那一步没成，后面的空着）
struct UpgradeProgress: View {
    let current: Int
    var failed: String? = nil
    var line: String = ""
    var fraction: Double? = nil
    var diagnose = true                  // 失败时提示「重试还不行就导出诊断信息」；系统权限挡住的不是重试能解决的，不提示

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            if failed == nil {
                HStack {
                    Text(line.isEmpty ? " " : line)
                    Spacer()
                    if let x = fraction { Text("\(Int(x * 100))%") }
                }
                .font(.system(size: 13)).foregroundStyle(Color.lkMuted).padding(.bottom, 6)
                Group {
                    if let x = fraction { ProgressView(value: x) } else { ProgressView() }
                }
                .progressViewStyle(.linear).tint(.lkBrand).padding(.bottom, 16)
            }
            VStack(alignment: .leading, spacing: 10) {
                ForEach(Array(UpgradeSteps.names.enumerated()), id: \.offset) { i, name in
                    HStack(alignment: .top, spacing: 10) {
                        LKStateIcon(state: i < current ? .done : (i == current ? (failed == nil ? .busy : .fail) : .wait))
                        VStack(alignment: .leading, spacing: 2) {
                            Text(name).font(.system(size: 14))
                            if i == current, let why = failed {
                                Text(why).font(.system(size: 12)).foregroundStyle(Color.lkMuted).fixedSize(horizontal: false, vertical: true)
                            }
                        }
                        Spacer()
                    }
                }
            }
            if failed != nil && diagnose {
                LKNote(icon: "triangle-alert", text: "重试还不行，就在「设置 › 关于」里导出诊断信息发给我们。", kind: .danger)
                    .padding(.top, 16)
            }
        }
    }
}

/// 新版起来后的那一段：补齐、试跑、完成
final class UpgradeFlow: ObservableObject {
    struct Failure { let step: Int; let title: String; let detail: String; let retry: Retry }
    enum Retry { case downloads, smoke, hub }

    let from: String
    let to: String
    private let then: String?                                  // 升级完要接着装的应用（安装器预选的）
    @Published var step = 3                                    // UpgradeSteps 里的第几步
    @Published var line = "正在看还要补什么…"
    @Published var fraction: Double?
    @Published var failure: Failure?
    @Published var done = false
    @Published var appsWithUpdates: [String] = []                // 做完时已装的应用里有新版本的（螺壳不顺带升级应用）
    let model: FirstRunModel
    private var timer: Timer?
    private var sub: AnyCancellable?

    init(from: String, to: String, then: String? = nil) {
        self.from = from
        self.to = to
        self.then = then
        model = FirstRunModel(preselect: nil, upgrade: (from, to))   // 它顺带去取这一版的更新说明（releaseNotes）
    }

    func start() {
        // 应用这次不换（各自在「应用」里升级）：常驻的马上拉起，不等补引擎、模型这几 GB 下完（luoke-top 10-24 核出：之前整段都不在）
        Accessories.launchResident()
        model.preflight()
        if let e = model.error { fail(Failure(step: 3, title: UpgradeSteps.failedTitle, detail: e, retry: .hub)); return }
        sub = model.$liveDownloads.receive(on: RunLoop.main).sink { [weak self] _ in self?.refresh() }
        model.fetch()
        timer = Timer.scheduledTimer(withTimeInterval: 1, repeats: true) { [weak self] _ in self?.refresh() }
    }

    private func label(_ name: String) -> String {
        guard let r = model.planLabels[name] else { return name }
        return r.name == "推理引擎" ? "推理引擎" : r.name + " " + r.detail
    }

    /// 每秒看一次：还在下载就写下载到哪一项；下完了看主控的试跑结果
    func refresh() {
        guard failure == nil, !done else { return }
        if model.step == .fetching {
            step = 3
            let dl = model.liveDownloads
            if let n = model.planNames.first(where: { (dl[$0]?["state"] as? String) != "done" }) {
                let d = dl[n] ?? [:]
                let got = (d["done"] as? NSNumber)?.doubleValue ?? 0, total = (d["total"] as? NSNumber)?.doubleValue ?? 0
                line = "正在更新\(label(n))"
                fraction = total > 0 ? got / total : nil
            }
            return
        }
        guard model.step == .smoke else { return }
        if !model.failed.isEmpty {
            let why = model.failed.count == 1 ? (model.failedReasons[model.failed[0]] ?? "") : ""
            fail(Failure(step: 3, title: UpgradeSteps.failedTitle, detail: model.failed.map(label).joined(separator: "、") + " 没更新上" + (why.isEmpty ? "" : "：" + why.trimmingCharacters(in: CharacterSet(charactersIn: "。"))) + "。没更新上的还是原来的，照常能用。", retry: .downloads))
            return
        }
        step = 4
        fraction = nil
        let hub = HubModel.shared
        guard let st = hub.status else {
            if hub.hubDown { fail(Failure(step: 4, title: UpgradeSteps.failedTitle, detail: "新版螺壳没能启动起来。", retry: .hub)) }
            return
        }
        if st.base == nil && st.engineReady { fail(Failure(step: 3, title: UpgradeSteps.failedTitle, detail: "推理引擎起来了，但没找到这一档的底座模型。", retry: .downloads)); return }
        if st.smokeOK == false { fail(Failure(step: 4, title: UpgradeSteps.failedTitle, detail: "东西都换好了，但模型没能在这台 Mac 上跑起来。", retry: .smoke)); return }
        if st.smokeOK == true || st.smokeDeferred { finish(); return }
        line = st.phase == "starting" ? "正在加载模型…" : "正在本机试跑一次…"
    }

    private func fail(_ f: Failure) {
        failure = f
        fraction = nil
        Accessories.noRestart.removeAll()
        Accessories.launchResident()
    }

    /// 做完：卡片上写「已升级到 X」和这一版的更新说明，用户点「完成」关掉（不发通知）
    private func finish() {
        timer?.invalidate()
        if then == nil && !FirstRunModel.previewing { appsWithUpdates = AppsWindow.updatable().map(\.name) }
        Accessories.noRestart.removeAll()
        Accessories.launchResident()
        done = true
    }

    /// going：接着去「添加应用」升级应用，主窗口留着
    func close(going: Bool = false) {
        let next = done ? then : nil
        UpgradeWindow.close(keepOpen: going || next != nil || MainWindow.openAfterSetup)
        if let id = next { AppsWindow.install(id) } else { MainWindow.showAfterSetup() }    // 升级走完再开主窗口（#112）
    }

    func retry() {
        guard let f = failure else { return }
        failure = nil
        switch f.retry {
        case .downloads: model.retryFailed()
        case .smoke: line = "正在重新试跑…"; model.rerunSmoke()
        case .hub: line = "正在重新启动螺壳…"; HubModel.shared.startHub()
        }
    }

    /// 先用着：关窗。下次主控启动再接着补（标记放回去）
    func later() {
        timer?.invalidate()
        Installer.writeUpgrade(from: from, to: to)
        UpgradeWindow.close(keepOpen: MainWindow.openAfterSetup)
        MainWindow.showAfterSetup()
    }
}

struct UpgradeView: View {
    @ObservedObject var f: UpgradeFlow
    @ObservedObject var notes: FirstRunModel

    init(f: UpgradeFlow) { self.f = f; notes = f.model }

    var body: some View {
        if f.done {
            UpgradeCard(title: "已升级到 \(f.to)") {
                if notes.releaseNotes.isEmpty {
                    Text("这一版的更新说明没取到，可以在发布页看。").font(.system(size: 14)).foregroundStyle(Color.lkMuted)
                } else {
                    LKReleaseNotes(text: Updater.userNotes(notes.releaseNotes))
                }
                if !f.appsWithUpdates.isEmpty {
                    LKNote(icon: "circle-arrow-up", text: f.appsWithUpdates.joined(separator: "、") + " 有新版本，在「应用」里升级。", kind: .warning).padding(.top, 12)
                }
            } footer: {
                Spacer()
                if f.appsWithUpdates.isEmpty {
                    Button("完成") { f.close() }.buttonStyle(LKPrimaryButton()).keyboardShortcut(.defaultAction)
                } else {   // 10-24：完成页说了「在「应用」里升级」却没按钮，要回菜单找
                    Button("完成") { f.close() }.buttonStyle(LKSecondaryButton(ghost: true))
                    Button("去升级应用") { f.close(going: true); AppsWindow.show() }.buttonStyle(LKPrimaryButton()).keyboardShortcut(.defaultAction)
                }
            }
        } else if let e = f.failure {
            UpgradeCard(title: e.title) {
                UpgradeProgress(current: e.step, failed: e.detail)
            } footer: {
                Spacer()
                Button("先用着") { f.later() }.buttonStyle(LKSecondaryButton(ghost: true))
                Button { f.retry() } label: { LKLabel("重试", icon: "rotate-cw") }.buttonStyle(LKPrimaryButton()).keyboardShortcut(.defaultAction)
            }
        } else {
            UpgradeCard(title: "正在升级到 \(f.to)") {
                UpgradeProgress(current: f.step, line: f.line, fraction: f.fraction)
            } footer: {
                Spacer()
                Button("升级中") {}.buttonStyle(LKPrimaryButton()).disabled(true)
            }
        }
    }
}

/// 新版起来后接着显示的卡片：和检查更新窗口、安装器的卡片同样大小、同一个位置。
/// 带主窗口的这一份占整个主窗口，走完才露出「今天」；原来的窗口没有关闭按钮，主窗口这时也关不掉
enum UpgradeWindow {
    static var window: NSWindow?
    static var flow: UpgradeFlow?
    static let hostID = "upgrade"
    /// 升级卡片开着：主窗口里的，或者自己的窗口
    static var isOpen: Bool { MainWindow.showing(hostID) || window?.isVisible == true }

    /// then = 升级完要接着装的应用
    static func show(from: String, to: String, then: String? = nil) {
        if MainWindow.showing(hostID) { MainWindow.show(); return }
        if let w = window, w.isVisible { Uninstall.bringFront(w); return }
        let f = UpgradeFlow(from: from, to: to, then: then)
        if MainWindow.present(hostID, title: "检查更新", style: .takeover, lockClose: true, { UpgradeView(f: f) }) != .noWindow {   // 整窗不会是 busy
            flow = f
            f.start()
            return
        }
        let w = NSWindow(contentViewController: NSHostingController(rootView: UpgradeView(f: f)))
        w.title = "检查更新"
        w.styleMask = [.titled]
        w.setContentSize(NSSize(width: 560, height: 460))
        w.center()
        w.isReleasedWhenClosed = false
        window = w
        flow = f
        AppWindows.track(w)
        Uninstall.bringFront(w)
        f.start()
    }

    /// keepOpen：带主窗口时，主窗口留着（用户自己打开过螺壳，或者接着去「添加应用」装）；不然为升级开的主窗口跟着关
    static func close(keepOpen: Bool = false) {
        MainWindow.endSetup(hostID, keepOpen: keepOpen)
        window?.close(); window = nil; flow = nil
    }
}

/// 上一次换包没成（新版起来时发现跑的还是旧版，Swap.takeResult）：带主窗口时是盖在主窗口上的一张卡片，不弹框（11-10：只有一个窗口）
enum UpgradeNotDone {
    static let title = "升级没完成"
    static let hostID = "upgrade-not-done"

    /// 画进了主窗口答 true；这一份没带主窗口答 false，调用方照旧弹框。
    /// 螺壳刚起来时就调，这时没有正忙着的卡片（开启螺壳搜索问着系统），不会是 busy
    static func show(_ text: String) -> Bool {
        MainWindow.present(hostID, title: title, style: .card) { UpgradeNotDoneView(text: text) } != .noWindow
    }
}

struct UpgradeNotDoneView: View {
    let text: String

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            Text(text).font(.system(size: 13)).fixedSize(horizontal: false, vertical: true)     // 标题是主窗口的卡片画的
            HStack {
                Spacer()
                // 主窗口只是为这张卡片开的（用户没自己打开螺壳）：跟着关
                Button("好") { MainWindow.endSetup(UpgradeNotDone.hostID, keepOpen: false) }
                    .buttonStyle(LKPrimaryButton()).keyboardShortcut(.defaultAction)
            }
        }
        .lkCardBody()
    }
}
