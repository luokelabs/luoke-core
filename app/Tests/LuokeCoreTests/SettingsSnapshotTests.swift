import XCTest
@testable import LuokeCore

/// 「设置」给主窗口的数据（SettingsSnapshot）：两个版本的状态、模型清单、型号里认参数量和量化
final class SettingsSnapshotTests: XCTestCase {
    private let recipe: [String: Any] = [
        "models": [
            "qwen3.5-9b-4bit": ["dir": "Qwen3.5-9B-4bit", "kind": "base", "size_gb": 5.8],
            "gemma-4-26b-8bit": ["dir": "gemma-4-26b-a4b-it-8bit", "kind": "base", "size_gb": 26.0],
            "gemma-4-e4b-4bit": ["dir": "gemma-4-e4b-it-4bit", "kind": "base", "size_gb": 4.8],
            "qwen3.5-0.8b-base-4bit": ["dir": "Qwen3.5-0.8B-Base-4bit", "kind": "special", "size_gb": 0.5],
        ],
        "assignment": [
            "32": ["base": "qwen3.5-9b-4bit", "special": ["luoke-ime.rerank": "qwen3.5-0.8b-base-4bit", "luoke-ime.predict": "qwen3.5-0.8b-base-4bit"]],
            "128": ["base": "gemma-4-26b-8bit", "special": ["luoke-ime.rerank": "qwen3.5-0.8b-base-4bit"]],
        ],
    ]
    private let choices = [
        Manifests.TierChoice(tier: "128", label: "完整", base: "gemma-4-26b-a4b-it-8bit", sizeGB: 26, note: ""),
        Manifests.TierChoice(tier: "32", label: "省空间", base: "Qwen3.5-9B-4bit", sizeGB: 5.8, note: ""),
    ]

    private func tiers(current: String = "128", pending: String? = nil, downloads: [String: [String: Any]] = [:], needed: [String] = [],
                       tierError: (tier: String, text: String)? = nil, onDisk: Set<String> = ["gemma-4-26b-8bit", "qwen3.5-0.8b-base-4bit"]) -> [TierEntry.State] {
        SettingsSnapshot.tiers(choices: choices, recipe: recipe, current: current, pending: pending, downloads: downloads, needed: needed,
                               tierError: tierError, installed: { onDisk.contains($0) }, sizeGB: { SettingsSnapshot.sizeGB(self.recipe, $0) })
            .map(\.state)
    }

    func testTierStates() {
        // 平常：完整在用，省空间要下 9B（0.8B 已在磁盘上，不算）
        XCTAssertEqual(tiers(), [.inUse, .available(needGB: 5.8)])
        // 都在磁盘上：点了马上换
        XCTAssertEqual(tiers(onDisk: ["gemma-4-26b-8bit", "qwen3.5-9b-4bit", "qwen3.5-0.8b-base-4bit"]), [.inUse, .available(needGB: 0)])
        // 换去省空间，下载中
        XCTAssertEqual(tiers(pending: "32", downloads: ["qwen3.5-9b-4bit": ["state": "downloading", "done": 1, "total": 4]]),
                       [.inUse, .switching(progress: 0.25)])
        // 排队，还不知道多大
        XCTAssertEqual(tiers(pending: "32", downloads: ["qwen3.5-9b-4bit": ["state": "queued"]]), [.inUse, .switching(progress: nil)])
        // 没下下来
        XCTAssertEqual(tiers(pending: "32", downloads: ["qwen3.5-9b-4bit": ["state": "failed", "error": "连不上"]]),
                       [.inUse, .failed(reason: "下载失败：连不上")])
        // 换档前就挡下：磁盘不够
        XCTAssertEqual(tiers(tierError: ("32", "磁盘空间不够")), [.inUse, .failed(reason: "磁盘空间不够")])
        // 正在用的这一档还缺模型、没在下：出错了，能重试
        XCTAssertEqual(tiers(needed: ["qwen3.5-0.8b-base-4bit"]), [.failed(reason: "缺少 Qwen3.5-0.8B-Base"), .available(needGB: 5.8)])
        // 正在用的这一档在补：显示进度
        XCTAssertEqual(tiers(downloads: ["qwen3.5-0.8b-base-4bit": ["state": "downloading", "done": 1, "total": 2]]),
                       [.switching(progress: 0.5), .available(needGB: 5.8)])
        // 档名不同、底座相同也算在用（16 和 32 都是 9B）
        XCTAssertEqual(SettingsSnapshot.tiers(choices: [choices[1]], recipe: ["assignment": ["16": ["base": "qwen3.5-9b-4bit"], "32": ["base": "qwen3.5-9b-4bit"]]],
                                              current: "16", pending: nil, downloads: [:], needed: [], tierError: nil,
                                              installed: { _ in true }, sizeGB: { _ in 0 }).map(\.state), [.inUse])
    }

    func testKind() {
        // 这台 Mac 跑得动的最大的底座是 full，更小的是 lighter（和状态无关）
        let k = SettingsSnapshot.tiers(choices: choices, recipe: recipe, current: "32", pending: nil, downloads: [:], needed: [], tierError: nil,
                                       installed: { _ in true }, sizeGB: { _ in 0 }).map(\.kind)
        XCTAssertEqual(k, [.full, .lighter])
    }

    func testCatalog() {
        let c = SettingsSnapshot.catalog(choices: choices, recipe: recipe)
        XCTAssertEqual(c, [
            CatalogEntry(name: "Gemma-4-26B", base: .full, params: "26B", quant: "8-bit", sizeGB: 26),
            CatalogEntry(name: "Qwen3.5-9B", base: .lighter, params: "9B", quant: "4-bit", sizeGB: 5.8),
            CatalogEntry(name: "Qwen3.5-0.8B-Base", accessories: ["luoke-ime"], params: "0.8B", quant: "4-bit", sizeGB: 0.5),
        ])     // gemma-4-e4b 没分到任何一档：不列
        // 既是底座又被两个配件专用：两个字段都填，配件去重
        let both = SettingsSnapshot.catalog(choices: [choices[1]], recipe: ["models": recipe["models"]!, "assignment": [
            "32": ["base": "qwen3.5-9b-4bit", "special": ["luoke-mail.triage": "qwen3.5-9b-4bit", "luoke-mem.typing": "qwen3.5-9b-4bit", "luoke-mail.clean": "qwen3.5-9b-4bit"]]]])
        XCTAssertEqual(both.map(\.base), [.full])      // 只有这一档能选：它就是最大的
        XCTAssertEqual(both.map(\.accessories), [["luoke-mail", "luoke-mem"]])
    }

    func testNameParsing() {
        XCTAssertEqual(SettingsSnapshot.params("gemma-4-e4b-it-4bit"), "E4B")
        XCTAssertEqual(SettingsSnapshot.params("gemma-4-26b-a4b-it-8bit"), "26B")
        XCTAssertEqual(SettingsSnapshot.params("Qwen3.5-0.8B-Base-4bit"), "0.8B")
        XCTAssertEqual(SettingsSnapshot.params("mystery"), "—")
        XCTAssertEqual(SettingsSnapshot.quant("Qwen3.5-9B-4bit"), "4-bit")
        XCTAssertEqual(SettingsSnapshot.quant("foo-bf16"), "BF16")
        XCTAssertEqual(SettingsSnapshot.versionText("local-1113-1"), "local-1113-1（本机版，没上传）")
        XCTAssertEqual(SettingsSnapshot.versionText("internal-1113"), "internal-1113")
    }
}
