import AppKit
import Foundation

/// 放置、移走、删除 .app：LaunchServices 的登记只在这里管（Python 那边是 luoke_core/apps.py，同一套规矩）。
/// - LaunchServices 会跟着挪动更新它记的路径：挪之前注销旧路径，挪之后在新路径按要求登记或注销；
/// - 从 DMG（或任何别处）拷进来的那份源也注销：打开过它就被登记了，按 bundle id 打开可能落回 DMG 里；
/// - 回滚用的旧版、备份放进 *.noindex 并注销。
enum AppPlacement {
    static let lsregister = "/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister"

    private static func ls(_ flag: String, _ url: URL) { Installer.shell(lsregister, [flag, url.path]) }

    /// 只注销（例如更新时挂载的 DMG 里那份：打开过就被登记了）
    static func unregister(_ url: URL) { ls("-u", url) }

    /// 把 src 拷成 dst（ditto，dst 原来有就先删），去掉下载标记，注销 src，按要求登记 dst。拷失败返回 false，dst 不留半份。
    @discardableResult
    /// 拉起一个 app，经 /usr/bin/open，不用 NSWorkspace（10-24 开发机，luoke-top 用 lsappinfo 查出来、本机 macOS 27 复现过）：
    /// NSWorkspace / LSOpen 拉起的 app 把我们记成它的 parentASN；我们退出以后，只要它还活着，程序坞就一直挂着我们这个死掉的图标
    /// （"后台运行"）——升级时"主控起安装器、安装器起新主控"每一层都挂一个。经 open 拉起的没有 parentASN。
    /// newInstance = open -n（安装器和主控同一个 bundle id），background = open -g（不抢前台）。done 在主线程上回调，参数是成没成。
    static func open(_ app: URL, newInstance: Bool = false, background: Bool = false, args: [String] = [], done: ((Bool) -> Void)? = nil) {
        var a: [String] = []
        if newInstance { a.append("-n") }
        if background { a.append("-g") }
        a.append(app.path)
        if !args.isEmpty { a += ["--args"] + args }
        DispatchQueue.global().async {
            let ok = Installer.shell("/usr/bin/open", a) == 0
            if !ok { NSLog("拉起 %@ 失败", app.path) }
            DispatchQueue.main.async { done?(ok) }
        }
    }

    static func bundleID(_ app: URL) -> String? {
        // 直接读 Info.plist：Bundle(url:) 会缓存，同一进程里换过包还给旧的
        NSDictionary(contentsOf: app.appendingPathComponent("Contents/Info.plist"))?["CFBundleIdentifier"] as? String
    }

    static func copy(_ src: URL, to dst: URL, register: Bool) -> Bool {
        let fm = FileManager.default
        try? fm.createDirectory(at: dst.deletingLastPathComponent(), withIntermediateDirectories: true)
        if fm.fileExists(atPath: dst.path) {
            // 那里原来的只有和要拷的是同一个应用（bundle id 一样）才删；别的应用一律不碰（10-24：改名后和 agent 开发版同名）
            guard bundleID(dst) != nil, bundleID(dst) == bundleID(src) else { return false }
            ls("-u", dst); try? fm.removeItem(at: dst)
        }
        guard Installer.shell("/usr/bin/ditto", [src.path, dst.path]) == 0 else {
            try? fm.removeItem(at: dst)
            return false
        }
        Installer.shell("/usr/bin/xattr", ["-dr", "com.apple.quarantine", dst.path])   // 公证过的拷贝：去掉下载标记，免得被 App Translocation
        if src.standardizedFileURL.path != dst.standardizedFileURL.path { ls("-u", src) }
        ls(register ? "-f" : "-u", dst)
        return true
    }

    /// 把 src 挪到 dst（dst 不能已经存在）
    static func move(_ src: URL, to dst: URL, register: Bool) throws {
        ls("-u", src)
        try FileManager.default.moveItem(at: src, to: dst)
        ls(register ? "-f" : "-u", dst)
    }

    /// 挪进 dir（*.noindex），用 name 这个名字；那里原来的同名旧备份先删。返回新位置。
    @discardableResult
    static func retire(_ app: URL, into dir: URL, name: String? = nil) throws -> URL {
        let fm = FileManager.default
        try fm.createDirectory(at: dir, withIntermediateDirectories: true)
        let keep = dir.appendingPathComponent(name ?? app.lastPathComponent)
        if fm.fileExists(atPath: keep.path) { ls("-u", keep); try fm.removeItem(at: keep) }
        try move(app, to: keep, register: false)
        return keep
    }

    /// 两个位置对调（回滚：a 是现在用的，b 是 *.noindex 里的旧版；对调后 a 处登记、b 处注销）
    static func swap(_ a: URL, _ b: URL) throws {
        let fm = FileManager.default
        let tmp = b.deletingLastPathComponent().appendingPathComponent(".swap-" + a.lastPathComponent)
        try? fm.removeItem(at: tmp)
        ls("-u", a); ls("-u", b)
        do {
            try fm.moveItem(at: a, to: tmp)
            try fm.moveItem(at: b, to: a)
            try fm.moveItem(at: tmp, to: b)
        } catch {
            if !fm.fileExists(atPath: a.path), fm.fileExists(atPath: tmp.path) { try? fm.moveItem(at: tmp, to: a) }
            ls("-f", a)
            throw error
        }
        ls("-u", b)
        ls("-f", a)
    }

    /// 注销再放进废纸篓（用户能找回）
    static func trash(_ app: URL, completion: @escaping (Error?) -> Void) {
        ls("-u", app)
        NSWorkspace.shared.recycle([app]) { _, e in completion(e) }
    }
}
