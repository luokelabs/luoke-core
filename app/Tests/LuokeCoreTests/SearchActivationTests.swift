import XCTest
@testable import LuokeCore

/// 开启螺壳搜索默认搜整个个人文件夹（11-10 定）：换成个人文件夹时，设置里已有的哪些去掉。只用临时文件夹，不碰真的个人文件夹
final class SearchActivationTests: XCTestCase {
    private var home: URL!

    override func setUpWithError() throws {
        home = FileManager.default.temporaryDirectory.appendingPathComponent("sa-home-\(UUID().uuidString)")
        for sub in ["Desktop", "Documents/工作", "Library/Mobile Documents/com~apple~CloudDocs"] {
            try FileManager.default.createDirectory(at: home.appendingPathComponent(sub), withIntermediateDirectories: true)
        }
    }

    override func tearDownWithError() throws {
        try? FileManager.default.removeItem(at: home)
    }

    /// 以前的桌面、文稿和里面自己加的文件夹包含在个人文件夹里，去掉；iCloud 云盘在资源库里，搜索走个人文件夹时不进去，留着；
    /// 个人文件夹本身、外接盘、别处的也留着
    func testCoveredByHome() {
        let h = home.path
        let saved = [h, h + "/Desktop", h + "/Documents/工作", h + "/Library/Mobile Documents/com~apple~CloudDocs",
                     "/Volumes/盘/照片", h + "x/Documents"]
        XCTAssertEqual(SearchActivation.covered(saved, home: h), [h + "/Desktop", h + "/Documents/工作"])
    }

    /// 设置里的写法和这里不一样（/tmp 对 /private/tmp 这类链接）也认得出
    func testCoveredFollowsLinks() throws {
        let link = FileManager.default.temporaryDirectory.appendingPathComponent("sa-link-\(UUID().uuidString)")
        try FileManager.default.createSymbolicLink(at: link, withDestinationURL: home)
        defer { try? FileManager.default.removeItem(at: link) }
        XCTAssertEqual(SearchActivation.covered([link.path + "/Desktop"], home: home.path), [link.path + "/Desktop"])
        XCTAssertEqual(SearchActivation.covered([home.path + "/Desktop"], home: link.path), [home.path + "/Desktop"])
    }

    /// 没开 iCloud 云盘的 Mac 上不列这一项，卡片那句说明也不提它
    func testIntroMentionsICloudOnlyWhenListed() {
        let with = SearchActivationModel.intro(iCloud: true), without = SearchActivationModel.intro(iCloud: false)
        XCTAssertTrue(with.hasPrefix("默认搜你的个人文件夹和 iCloud 云盘里的"))
        XCTAssertTrue(with.hasSuffix("读桌面、文稿、下载和 iCloud 云盘。"))
        XCTAssertFalse(without.contains("iCloud"))
        XCTAssertTrue(without.hasPrefix("默认搜你的个人文件夹里的"))
        XCTAssertTrue(without.hasSuffix("读桌面、文稿和下载。"))
    }
}
