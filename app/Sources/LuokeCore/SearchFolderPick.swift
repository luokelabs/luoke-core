// 搜索页的「添加」：搜索的应用页由 agent 画，搜索自己没有界面。它经描述符 3 说 {"pick_folder": true}，本体弹系统的选文件夹框，
// 把选中的写进搜索的设置（它自己的 settings 命令，检查和开启时一样）。页面、搜索都不带路径：路径只来自用户在框里选的那一个。
// 框只紧跟用户在搜索的页上的点按（主窗口交给 agent 之前告诉本体，AgentCore.appPageTapped），一下只换一次（弹不弹都用掉），
// 而且这一下要在上一个框、上一张卡片关掉以后：搜索说的话不算数。点了螺壳里别的地方、在底下的输入框里打字按回车，
// 或者搜索连着说，都不弹（不然框会在用户打字时冒出来，回车就把个人文件夹加进去）。另外照旧要螺壳在最前、刚有过点按或按键。框挂在主窗口上（只有一个窗口），要说的话画成主窗口里的卡片。
// 读得了读不了在一个子进程里试（和常驻的搜索一样借螺壳的授权）：在框里选过的，本体这个进程读得了，不等于搜索读得了；
// 受保护的地方（桌面、文稿、下载、iCloud 云盘、外接盘）macOS 这时问螺壳，不让读就不加。
// 和设置里的某一个是同一个的不加；在已搜的文件夹里面的照样加成单独一项：搜索平常跳过的（音乐、代码项目、隐藏的……）本体不知道，
// 单独加了它就会读。已搜的不替用户去掉。
import AppKit
import SwiftUI

enum SearchFolderPick {
    static let hostID = "search-folder-pick"
    /// 框开着、或选完还在写设置：再来的不弹
    private(set) static var picking = false
    /// 上一个框、上一张卡片关掉的时候（开机以来的秒数）：要用户在这以后再点一下才弹
    private(set) static var lastClosed: TimeInterval?
    /// 用户最后一次在搜索的页上点按的时候（开机以来的秒数）；搜索说一次要选就用掉，弹不弹都算
    private(set) static var lastSearchTap: TimeInterval?
    /// 点了以后多久内搜索说的才算。点「添加」到搜索说要选正常不到 1 秒，但中间要经 agent、搜索一次只答一个调用，
    /// 搜索正忙（长的搜索、刚重启）时会慢；和 UserAction.recent 一样给 10 秒。挡的主要是「搜索页上的、只用一次的这一下」
    static let tapWait: TimeInterval = 10

    static func now() -> TimeInterval { ProcessInfo.processInfo.systemUptime }

    /// 主窗口：用户在搜索的页上点了一下，交给 agent 之前
    static func searchTapped(at time: TimeInterval = now()) { lastSearchTap = time }

    enum Outcome: Equatable {
        case added
        case already(String)        // 已经在搜：选的那个的名字
        case denied(String)         // macOS 没让螺壳读：选的那个的名字
        case failed(String)         // 搜索没加上：它说的原因
    }

    enum Refusal: Equatable {
        case picking        // 框开着、选完还在写设置
        case noTap          // 没刚在搜索的页上点过（或那一下已经弹过）
        case beforeClose    // 那一下在上一个框、卡片关掉之前
        case notFront       // 螺壳不在最前，或没刚有过点按、按键
    }

    /// 弹不弹：nil 是弹
    static func refusal(tapped: TimeInterval?, recent: Bool, picking: Bool = picking, closed: TimeInterval? = lastClosed,
                        now: TimeInterval = now()) -> Refusal? {
        if picking { return .picking }
        guard let tapped, now >= tapped, now - tapped < tapWait else { return .noTap }
        if let closed, tapped <= closed { return .beforeClose }
        return recent ? nil : .notFront
    }

    /// 搜索要选文件夹（主线程上，HelperProcess.onHostMessage）。答弹了没有
    @discardableResult
    static func show(tapped: () -> TimeInterval? = { lastSearchTap }, recent: () -> Bool = { UserAction.recent() }) -> Bool {
        let tap = tapped()
        lastSearchTap = nil
        if let why = refusal(tapped: tap, recent: recent()) {
            let reason = switch why {
            case .picking: "框开着或还在写设置"
            case .noTap: "用户没刚在搜索的页上点过"
            case .beforeClose: "那一下在上次关掉之前"
            case .notFront: "螺壳不在最前或没刚点过"
            }
            NSLog("螺壳搜索：要选文件夹，%@，不弹", reason)
            return false
        }
        picking = true
        let panel = NSOpenPanel()
        panel.canChooseDirectories = true
        panel.canChooseFiles = false
        panel.allowsMultipleSelection = false
        panel.canCreateDirectories = false
        panel.prompt = "搜这个文件夹"
        panel.directoryURL = FileManager.default.homeDirectoryForCurrentUser
        let chosen: (NSApplication.ModalResponse) -> Void = { r in
            lastClosed = now()
            guard r == .OK, let url = panel.url else {
                picking = false         // 取消：什么都不做
                return
            }
            // 试读要等用户答 macOS 的询问，不在主线程上
            DispatchQueue.global(qos: .userInitiated).async {
                let outcome = add(url)
                DispatchQueue.main.async {
                    picking = false
                    tell(outcome)
                }
            }
        }
        if let w = MainWindow.sheetParent() {
            panel.beginSheetModal(for: w, completionHandler: chosen)
        } else {
            chosen(panel.runModal())        // 这一份没带主窗口
        }
        return true
    }

    /// 把选中的加进搜索的设置。不在主线程上调：试读时 macOS 可能问用户，这里等他答完
    static func add(_ url: URL, saved: () -> [String]? = { SearchHelper.current()?.folders },
                    canRead: (URL) -> Bool = readable,
                    choose: ([String]) -> String? = { SearchHelper.choose(folders: $0, remove: [], photos: nil) }) -> Outcome {
        let path = url.standardizedFileURL.path
        let name = FileManager.default.displayName(atPath: path)
        guard let have = saved() else { return .failed("读不了螺壳搜索现在的设置，过一会儿再试。") }
        if same(path, have) { return .already(name) }
        guard canRead(URL(fileURLWithPath: path)) else { return .denied(name) }
        if let why = choose([path]) { return .failed(why) }
        return .added
    }

    /// 和设置里的某一个是同一个。路径先跟到链接指的地方再比（设置里的写法不一定和这里一样）
    static func same(_ path: String, _ saved: [String]) -> Bool {
        func real(_ p: String) -> String { URL(fileURLWithPath: p).resolvingSymlinksInPath().path }
        let r = real(path)
        return saved.contains { real($0) == r }
    }

    /// 在子进程里列一下这个文件夹：和常驻的搜索一样借螺壳的授权（Spawn.Access.luoke），不带本体在框里得到的那一份。
    /// 受保护的地方 macOS 这时问螺壳，等用户答完
    static func readable(_ folder: URL) -> Bool {
        guard let r = try? Spawn.capture("/bin/ls", [folder.path], env: ["PATH=/usr/bin:/bin"], capture: 1, access: .luoke, check: { _ in nil })
        else { return false }
        return r.status == 0
    }

    /// 加上了不说（页上的范围里有了它）；别的画成主窗口里的一张卡片
    static func text(_ outcome: Outcome) -> (text: String, settings: Bool)? {
        switch outcome {
        case .added: return nil
        case .already(let name): return ("「\(name)」已经在搜了。", false)
        case .denied(let name): return ("macOS 没让螺壳读「\(name)」，没加上。在 系统设置 › 隐私与安全性 里允许螺壳，再添加一次。", true)
        case .failed(let why): return ("没加上：\(why)", false)
        }
    }

    private static func tell(_ outcome: Outcome) {
        guard let (text, settings) = text(outcome) else { return }
        switch MainWindow.present(hostID, title: "添加要搜的文件夹", style: .card, closed: { lastClosed = now() },
                                  { SearchFolderPickView(text: text, settings: settings) }) {
        case .shown, .busy: return          // 忙着的那一块（正问着系统、正在升级）要紧，这句不另弹
        case .noWindow:
            let a = NSAlert()       // 这一份没带主窗口
            a.messageText = "添加要搜的文件夹"
            a.informativeText = text
            a.runModal()
            lastClosed = now()
        }
    }

    static func close() {
        lastClosed = now()
        MainWindow.dismiss(hostID)
    }
}

struct SearchFolderPickView: View {
    let text: String
    let settings: Bool

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            Text(text).font(.system(size: 13)).fixedSize(horizontal: false, vertical: true)
            HStack {
                if settings {
                    Button("打开系统设置") {
                        if let u = URL(string: "x-apple.systempreferences:com.apple.settings.PrivacySecurity.extension") { NSWorkspace.shared.open(u) }
                    }
                    .buttonStyle(LKSecondaryButton(ghost: true))
                }
                Spacer()
                Button("好") { SearchFolderPick.close() }.buttonStyle(LKPrimaryButton())
            }
        }
        .lkCardBody()
    }
}
