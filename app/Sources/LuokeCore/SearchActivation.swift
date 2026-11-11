// 开启螺壳搜索：首次运行的完成页、「应用」窗口、老用户升级后都走这里。
// 默认搜整个个人文件夹和 iCloud 云盘，不让用户一个个挑（11-10 定）。个人文件夹本身不受保护，里面的桌面、文稿、下载受保护。
// 系统权限在这里要，不等常驻程序读到再弹：先读一下这几个受保护的文件夹（第一次读时 macOS 问，这里等用户答完），照片要的话问照片，
// 要到至少一样才把选中的写进搜索的设置、记下开启、起程序。一样都没要到就开不了。螺壳有完全磁盘访问时读得了，macOS 不问。
// 搜索的设置里已有的（老用户、停用后重开）照它勾，取消勾选的从设置里去掉。老用户选过别的文件夹、没选个人文件夹的，个人文件夹不替他勾：
// 点了才换。换成个人文件夹时，设置里已有的、在个人文件夹里面的（以前的桌面、文稿、下载）去掉，都包含在里面了。
// 这几个询问算在螺壳头上，文字是 Info.plist 里那几条（build_app.sh）；以后加别的受保护文件夹时 macOS 还会问一次。
import AppKit
import Photos
import SwiftUI

enum SearchActivation {
    static var window: NSWindow?
    private static var model: SearchActivationModel?
    static let hostID = "search-activation"

    /// 「好」「以后再说」、开成了：关掉
    static func close() {
        MainWindow.dismiss(hostID)
        window?.close()
    }

    /// upgraded：老用户，旧的 螺壳搜索.app 刚挪走。done：开启成功以后（窗口关了不调）；窗口已经开着时也算上。
    /// 带主窗口的这一份是盖在主窗口上的一张卡片（和要权限的那张一样），问着系统的时候不让走开、主窗口也关不掉。
    /// 答开了没有：主窗口里另一张卡片正忙着时没开（答 false），done 不会来
    @discardableResult
    static func show(upgraded: Bool = false, done: (() -> Void)? = nil) -> Bool {
        if let model, MainWindow.showing(hostID) || window?.isVisible == true {
            if let done { model.phase == .partly ? done() : model.done.append(done) }     // 停在说明上时已经开了，那批回调调过了
            if let w = window, w.isVisible { Uninstall.bringFront(w) } else { MainWindow.show() }
            return true
        }
        let m = SearchActivationModel(upgraded: upgraded, done: done)
        switch MainWindow.present(hostID, title: "开启螺壳搜索", style: .card, canLeave: { m.phase != .working }, { SearchActivationView(model: m) }) {
        case .busy: return false
        case .shown: model = m; m.load(); return true
        case .noWindow: break
        }
        model = m
        m.load()
        let host = NSHostingController(rootView: SearchActivationView(model: m))
        // 不要用 .preferredContentSize：窗口高度在两个值之间来回跳，排版次数超限，AppKit 直接让进程崩掉。
        // 默认的 sizingOptions 跟着内容变高，设置里另有文件夹时列表更长也够用
        let w = NSWindow(contentViewController: host)
        w.title = "开启螺壳搜索"
        w.styleMask = [.titled, .closable]
        w.setContentSize(host.view.fittingSize)     // 先定大小再居中：还没排版时窗口只有 1 点宽，居中是歪的
        w.center()
        w.isReleasedWhenClosed = false
        window = w
        AppWindows.track(w)
        Uninstall.bringFront(w)
        return true
    }

    /// 问着的时候不能关：用户答了以后还要写设置、记开启，关了就成了以为取消、其实开了。
    /// 主窗口里的卡片由 canLeave 留住，主窗口的关闭按钮也锁上，问完再放开
    static func holdOpen(_ hold: Bool) {
        if hold { window?.styleMask.remove(.closable) } else { window?.styleMask.insert(.closable) }
        MainWindow.lockClose(hostID, hold)
    }

    /// 第一次读受保护的文件夹时 macOS 问用户，这个调用等到用户答完：不要在主线程上调
    static func canRead(_ folder: URL) -> Bool {
        (try? FileManager.default.contentsOfDirectory(atPath: folder.path)) != nil
    }

    /// 选了个人文件夹以后，设置里已有的哪些可以去掉：在个人文件夹里面的。`~/Library` 里的（iCloud 云盘）不算：搜索走个人文件夹时不进资源库。
    /// 路径先跟到链接指的地方再比（设置里的写法不一定和这里一样）
    static func covered(_ saved: [String], home: String) -> [String] {
        func real(_ path: String) -> String { URL(fileURLWithPath: path).resolvingSymlinksInPath().path }
        let h = real(home)
        return saved.filter {
            let r = real($0)
            return r != h && r.hasPrefix(h + "/") && !r.hasPrefix(h + "/Library/")
        }
    }

    /// 同上。和搜索读照片用的是同一档权限（读写），只选了部分照片也算允许
    static func askPhotos() -> Bool {
        let done = DispatchSemaphore(value: 0)
        PHPhotoLibrary.requestAuthorization(for: .readWrite) { _ in done.signal() }
        done.wait()
        return [.authorized, .limited].contains(PHPhotoLibrary.authorizationStatus(for: .readWrite))
    }
}

final class SearchActivationModel: ObservableObject {
    enum Answer { case notAsked, asking, allowed, partly, denied }
    enum Phase: Equatable { case idle, working, refused, partly, failed(String) }

    /// 点「开启」后要读一下、让 macOS 问的一个受保护的文件夹
    struct Probe {
        let title: String
        let folder: URL
    }

    struct Place: Identifiable {
        let id: String          // 个人文件夹是 home，照片是 photos，别的文件夹是路径
        let title: String
        let folder: URL?        // 照片没有
        var probes: [Probe]     // 一般就是它自己；个人文件夹是里面的桌面、文稿、下载
        var chosen: Bool
        var saved: String?      // 搜索的设置里已有：它在设置里的写法（照片是 photos）
        var answer = Answer.notAsked
        var denied: [String] = []   // 个人文件夹只允许了一部分时，没允许的是哪几个
        var isHome: Bool { id == SearchActivationModel.homeID }
    }

    static let homeID = "home"

    @Published var places: [Place]
    @Published var phase = Phase.idle { didSet { SearchActivation.holdOpen(phase == .working) } }
    @Published var loaded = false
    let upgraded: Bool
    var done: [() -> Void]

    init(upgraded: Bool, done: (() -> Void)?) {
        self.upgraded = upgraded
        self.done = done.map { [$0] } ?? []
        let home = FileManager.default.homeDirectoryForCurrentUser
        let inside = [("桌面", "Desktop"), ("文稿", "Documents"), ("下载", "Downloads")].map {
            Probe(title: $0.0, folder: home.appendingPathComponent($0.1))
        }
        places = [Place(id: Self.homeID, title: "个人文件夹", folder: home, probes: inside, chosen: true)]
        // iCloud 云盘在资源库里，搜索走个人文件夹时不进去，另算一项。写整个 Mobile Documents，不只 CloudDocs：访达里的「iCloud 云盘」
        // 还有各应用的文件夹（Pages、Numbers、Obsidian 的库……），都在 Mobile Documents/<应用>/Documents 下面。
        // 没开 iCloud 云盘的 Mac 上没有 CloudDocs：不列
        let icloud = home.appendingPathComponent("Library/Mobile Documents")
        if FileManager.default.fileExists(atPath: icloud.appendingPathComponent("com~apple~CloudDocs").path) {
            places.append(Place(id: icloud.path, title: Self.iCloudTitle, folder: icloud,
                                probes: [Probe(title: Self.iCloudTitle, folder: icloud)], chosen: true))
        }
        places.append(Place(id: "photos", title: "照片图库", folder: nil, probes: [], chosen: false))
    }

    static let iCloudTitle = "iCloud 云盘"
    var hasICloud: Bool { places.contains { $0.title == Self.iCloudTitle } }

    /// 卡片上那句说明：没开 iCloud 云盘的 Mac 上不列这一项，这句也不提它
    static func intro(iCloud: Bool) -> String {
        let places = iCloud ? "个人文件夹和 iCloud 云盘" : "个人文件夹"
        let asks = iCloud ? "桌面、文稿、下载和 iCloud 云盘" : "桌面、文稿和下载"
        return "默认搜你的\(places)里的文档、图片、视频；隐藏的文件夹、资源库、代码项目不读。点「开启」后，macOS 会逐个问你让不让螺壳读\(asks)。"
    }

    var anyChosen: Bool { places.contains { $0.chosen } }
    var deniedAny: Bool { places.contains { $0.answer == .denied || $0.answer == .partly } }
    var homeChosen: Bool { places.contains { $0.isHome && $0.chosen } }
    /// 没允许的：个人文件夹只允许了一部分的，说里面是哪几个
    var deniedTitles: [String] {
        places.flatMap { $0.answer == .partly ? $0.denied : $0.answer == .denied ? [$0.title] : [] }
    }

    /// 读搜索的设置，有的话照它勾。读完才能点「开启」
    func load() {
        DispatchQueue.global(qos: .userInitiated).async { [self] in
            let have = SearchHelper.current()
            DispatchQueue.main.async {
                if let have { self.apply(have) }
                self.loaded = true
            }
        }
    }

    func apply(_ have: SearchHelper.Chosen) {
        guard !have.folders.isEmpty || have.photos else { return }
        func real(_ path: String) -> String { URL(fileURLWithPath: path).resolvingSymlinksInPath().path }   // 设置里的写法不一定和这里一样
        for i in places.indices {
            let saved = places[i].folder.map { f in have.folders.first { real($0) == real(f.path) } } ?? (have.photos ? "photos" : nil)
            places[i].chosen = saved != nil       // 老用户没选过个人文件夹的不替他勾：点了才换
            places[i].saved = saved
        }
        let known = Set(places.compactMap { $0.saved })
        let others = have.folders.filter { !known.contains($0) }.map {
            let folder = URL(fileURLWithPath: $0)
            let title = FileManager.default.displayName(atPath: $0)     // 老用户以前选的桌面、文稿、下载照访达的叫法，不写成 Desktop
            return Place(id: $0, title: title, folder: folder, probes: [Probe(title: title, folder: folder)], chosen: true, saved: $0)
        }
        places.insert(contentsOf: others, at: places.count - 1)     // 照片留在最后
    }

    func start() {
        guard phase != .working else { return }
        phase = .working
        for i in places.indices where !places[i].chosen { places[i].answer = .notAsked; places[i].denied = [] }
        // 选了个人文件夹：设置里已有的、在里面的那几项包含在里面了，不另问，从设置里去掉
        let home = places.first { $0.isHome }?.folder?.path ?? NSHomeDirectory()
        let inHome = homeChosen ? Set(SearchActivation.covered(places.compactMap { $0.isHome ? nil : $0.saved }, home: home)) : []
        func covered(_ p: Place) -> Bool { p.saved.map(inHome.contains) ?? false }
        let chosen = places.filter { $0.chosen && !covered($0) }
        let dropped = places.filter { $0.saved != nil && (!$0.chosen || covered($0)) }
        DispatchQueue.global(qos: .userInitiated).async { [self] in
            var folders: [String] = [], photos = false
            for p in chosen {
                set(p.id, .asking)
                guard let folder = p.folder else {
                    let ok = SearchActivation.askPhotos()
                    set(p.id, ok ? .allowed : .denied)
                    photos = photos || ok
                    continue
                }
                // 逐个读一下受保护的，macOS 一个个问。个人文件夹本身读得了就算要到了：里面没允许的那几个，搜索跳过、出「需要权限」的卡片
                let denied = p.probes.filter { !SearchActivation.canRead($0.folder) }.map(\.title)
                let ok = p.isHome ? SearchActivation.canRead(folder) : denied.isEmpty
                set(p.id, !ok ? .denied : denied.isEmpty ? .allowed : .partly, denied: p.isHome ? denied : [])
                if ok { folders.append(folder.path) }
            }
            guard !folders.isEmpty || photos else {
                DispatchQueue.main.async { self.phase = .refused }
                return
            }
            // 没要到的不动：已在设置里的留着，用户在系统设置里允许以后接着搜
            let photosChange: Bool? = photos ? true : dropped.contains { $0.folder == nil } ? false : nil
            if let why = SearchHelper.choose(folders: folders, remove: dropped.filter { $0.folder != nil }.compactMap(\.saved), photos: photosChange) {
                DispatchQueue.main.async { self.phase = .failed(why) }
                return
            }
            // 勾了照片就是用户同意了，系统没给也算：用户去系统设置给了以后接着用。取消勾选的不再放行
            let wantsPhotos = chosen.contains { $0.folder == nil }
            if wantsPhotos || dropped.contains(where: { $0.folder == nil }) {
                // 记下了再起搜索：它起的时候才拿到这一项
                let recorded = DispatchSemaphore(value: 0)
                var failed: String?
                BundledApps.record(SearchHelper.accessory, "photos", on: wantsPhotos) { why in failed = why; recorded.signal() }
                recorded.wait()
                if let failed {
                    DispatchQueue.main.async { self.phase = .failed(failed) }
                    return
                }
            }
            DispatchQueue.main.async {
                SearchHelper.enable { why in
                    if let why { self.phase = .failed(why); return }
                    self.done.forEach { $0() }
                    if self.deniedAny { self.phase = .partly; return }      // 开了，但有的没要到：说一声再关
                    self.phase = .idle
                    SearchActivation.close()
                }
            }
        }
    }

    private func set(_ id: String, _ answer: Answer, denied: [String] = []) {
        DispatchQueue.main.async {
            if let i = self.places.firstIndex(where: { $0.id == id }) { self.places[i].answer = answer; self.places[i].denied = denied }
        }
    }

    func openSystemSettings() {
        if let u = URL(string: "x-apple.systempreferences:com.apple.settings.PrivacySecurity.extension") { NSWorkspace.shared.open(u) }
    }
}

struct SearchActivationView: View {
    @ObservedObject var model: SearchActivationModel
    @Environment(\.lkHost) private var host

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            HStack(spacing: 12) {
                LKAppIcon(accessory: SearchHelper.accessory, size: 36)
                VStack(alignment: .leading, spacing: 2) {
                    if host != .card { Text("开启螺壳搜索").font(.system(size: 18, weight: .semibold)) }     // 主窗口的卡片自己有标题
                    Text(model.upgraded ? "搜索收进了螺壳，要在螺壳名下重新允许一次。建好的索引都在。" : "按内容找到你的文件。索引只在这台 Mac 上。")
                        .font(.system(size: 13)).foregroundStyle(Color.lkMuted).fixedSize(horizontal: false, vertical: true)
                }
            }
            Text(SearchActivationModel.intro(iCloud: model.hasICloud))
                .font(.system(size: 13)).fixedSize(horizontal: false, vertical: true)
            LKGroup {
                ForEach(Array(model.places.indices), id: \.self) { i in
                    if i > 0 { Divider().overlay(Color.lkBorder) }
                    HStack(spacing: 10) {
                        Toggle(model.places[i].title, isOn: $model.places[i].chosen)
                            .toggleStyle(.checkbox).disabled(model.phase == .working || model.phase == .partly)
                        Spacer()
                        badge(model.places[i].answer)
                    }
                    .padding(.horizontal, 16).padding(.vertical, 10)
                }
            }
            message
            Spacer(minLength: 0)
            HStack {
                if model.deniedAny && model.phase != .working {
                    Button("打开系统设置") { model.openSystemSettings() }.buttonStyle(LKSecondaryButton(ghost: true))
                }
                Spacer()
                if model.phase == .partly {
                    Button("好") { SearchActivation.close() }.buttonStyle(LKPrimaryButton())
                } else {
                    Button("以后再说") { SearchActivation.close() }.buttonStyle(LKSecondaryButton()).disabled(model.phase == .working)
                    Button([.idle, .working].contains(model.phase) ? "开启" : "再试一次") { model.start() }
                        .buttonStyle(LKPrimaryButton()).disabled(model.phase == .working || !model.anyChosen || !model.loaded)
                }
            }
        }
        .lkCardBody()
    }

    @ViewBuilder private func badge(_ a: SearchActivationModel.Answer) -> some View {
        switch a {
        case .notAsked: EmptyView()
        case .asking: ProgressView().controlSize(.small)
        case .allowed: LKBadge(text: "已允许", kind: .success)
        case .partly: LKBadge(text: "部分允许", kind: .warning)
        case .denied: LKBadge(text: "没允许", kind: .warning)
        }
    }

    @ViewBuilder private var message: some View {
        switch model.phase {
        case .refused:
            Text("没有要到权限，搜索没有开启。macOS 不会再问拒绝过的项：在 系统设置 › 隐私与安全性 里允许螺壳，再点「再试一次」。")
                .font(.system(size: 13)).foregroundStyle(Color.lkDanger).fixedSize(horizontal: false, vertical: true)
        case .partly:
            Text("已开启。\(model.deniedTitles.joined(separator: "、"))没允许，这部分搜不到。要搜的话，在 系统设置 › 隐私与安全性 里允许螺壳，再跟螺壳说一声。")
                .font(.system(size: 13)).fixedSize(horizontal: false, vertical: true)
        case .failed(let why):
            Text("没能开启：\(why)").font(.system(size: 13)).foregroundStyle(Color.lkDanger).fixedSize(horizontal: false, vertical: true)
        default: EmptyView()
        }
    }
}
