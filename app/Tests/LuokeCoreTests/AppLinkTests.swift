import XCTest
@testable import LuokeCore

/// luoke://app/<应用>[/<页>][?参数]：打开主窗口里某个应用的页（输入法菜单的「设置」「此应用的设置」用）
final class AppLinkTests: XCTestCase {
    private func parse(_ s: String) -> AppLink.Target? { AppLink.parse(URL(string: s)!) }

    func testAppOnly() {
        XCTAssertEqual(parse("luoke://app/luoke-ime"), .init(app: "luoke-ime", page: nil, args: [:]))
        XCTAssertEqual(parse("luoke://app/luoke-ime/"), .init(app: "luoke-ime", page: nil, args: [:]))
    }

    func testPageAndArgs() {
        XCTAssertEqual(parse("luoke://app/luoke-ime/settings"), .init(app: "luoke-ime", page: "settings", args: [:]))
        XCTAssertEqual(parse("luoke://app/luoke-ime/apps?app=com.apple.Safari"),
                       .init(app: "luoke-ime", page: "apps", args: ["app": "com.apple.Safari"]))
    }

    func testRejects() {
        for s in ["luoke://memory", "luoke://app", "luoke://app/", "luoke://app/a/b/c",
                  "luoke://app/Luoke-IME/settings", "luoke://app/-x/settings", "luoke://app/luoke-ime/set.tings",
                  "luoke://app/luoke-ime/apps?App=x", "luoke://app/luoke-ime/apps?app=a&app=b",
                  "luoke://app/luoke-ime/apps?app=%0A", "luoke://app/luoke-ime/apps?app=%E4%B8%AD",
                  "http://app/luoke-ime/settings"] {
            XCTAssertNil(parse(s), s)
        }
        XCTAssertNil(parse("luoke://app/luoke-ime/apps?app=" + String(repeating: "a", count: 257)))
        let many = (0..<9).map { "a\($0)=1" }.joined(separator: "&")
        XCTAssertNil(parse("luoke://app/luoke-ime/apps?" + many))
    }

    func testHandleClaimsOnlyAppLinks() {
        XCTAssertFalse(AppLink.handle(URL(string: "luoke://memory")!))
        XCTAssertTrue(AppLink.handle(URL(string: "luoke://app/Bad")!))     // 格式不对也接住（忽略），不交给别的
    }
}
