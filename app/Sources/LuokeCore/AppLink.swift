import Foundation

/// luoke://app/<应用>[/<页>][?<参数>=<值>…]：别的进程打开主窗口里某个应用自己的页（例如输入法菜单的「设置」）。
/// 通用的地址，不针对哪个应用：主控只打开主窗口、翻到那个应用的那一页，参数原样交给那一页，含义由应用自己定。
/// 应用要是侧栏里有的（主窗口自己核）；应用名、页名、参数只认短的 ASCII，不合的整条忽略。
enum AppLink {
    struct Target: Equatable {
        let app: String
        let page: String?
        let args: [String: String]
    }

    static let maxArgs = 8
    static let maxValue = 256

    static func parse(_ url: URL) -> Target? {
        guard url.scheme == "luoke", url.host == "app",
              let c = URLComponents(url: url, resolvingAgainstBaseURL: false) else { return nil }
        let parts = c.path.split(separator: "/", omittingEmptySubsequences: true).map(String.init)
        guard (1...2).contains(parts.count), ident(parts[0], max: 64, dots: true) else { return nil }
        let page = parts.count == 2 ? parts[1] : nil
        if let p = page, !ident(p, max: 32, dots: false) { return nil }
        var args: [String: String] = [:]
        let items = c.queryItems ?? []
        guard items.count <= maxArgs else { return nil }
        for q in items {
            let v = q.value ?? ""
            guard ident(q.name, max: 32, dots: false), args[q.name] == nil,
                  v.count <= maxValue, v.unicodeScalars.allSatisfy({ $0.isASCII && $0.value >= 0x20 && $0.value != 0x7f }) else { return nil }
            args[q.name] = v
        }
        return Target(app: parts[0], page: page, args: args)
    }

    /// 是 luoke://app 的链接就接住（答 true），带主窗口的这一份翻到那一页；没带主窗口的什么都不开
    static func handle(_ url: URL) -> Bool {
        guard url.scheme == "luoke", url.host == "app" else { return false }
        guard let t = parse(url) else { NSLog("luoke://app：链接不合格式，忽略"); return true }
        MainWindow.openToolPage(app: t.app, page: t.page, args: t.args)
        return true
    }

    /// 小写字母、数字、`-`、`_`（应用名另认 `.`），开头是字母或数字
    private static func ident(_ s: String, max: Int, dots: Bool) -> Bool {
        guard !s.isEmpty, s.count <= max, let first = s.unicodeScalars.first,
              CharacterSet.lowercaseLetters.contains(first) || CharacterSet.decimalDigits.contains(first) else { return false }
        return s.unicodeScalars.allSatisfy { u in
            u.isASCII && ((u.value >= 0x61 && u.value <= 0x7a) || (u.value >= 0x30 && u.value <= 0x39) || u == "-" || u == "_" || (dots && u == "."))
        }
    }
}
