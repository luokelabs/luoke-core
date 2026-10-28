import AppKit
import SwiftUI

/// 设置 → 高级：给高级用户的本机接口（顶层 ADR 0014 修订，2026-10-19 定）。默认关，不当普通用户功能宣传。
/// 让 pi 这类工具改 base_url 和 key 就能用螺壳的引擎，不再单独跑一个 oMLX。
/// 10-23 精简：只留开关、地址和密钥（复制、重新生成）、能用的模型；不再在界面里登记本机模型、改密钥、看统计、装命令行工具。
struct EndpointView: View {
    @State private var info: [String: Any] = [:]
    @State private var error: String?
    @State private var busy = false
    @State private var copied: String?
    private let client = HubModel.shared.client

    private var enabled: Bool { info["enabled"] as? Bool ?? false }
    private var port: Int? { info["port"] as? Int }
    private var key: String { info["key"] as? String ?? "" }
    private var models: [String] { info["models"] as? [String] ?? [] }

    var body: some View {
        LKGroup {
            LKSettingRow(icon: "point.3.connected.trianglepath.dotted", title: "本机接口",
                         detail: "让其他 AI 工具用螺壳的模型，只在这台 Mac 上能连。") {
                Toggle("", isOn: Binding(get: { enabled }, set: { set(enabled: $0) }))
                    .toggleStyle(.switch).controlSize(.small).tint(.lkPrimary).labelsHidden().disabled(busy)
            }
            if enabled, let port {
                divider
                valueRow("地址", "http://127.0.0.1:\(port)/v1") { copyButton("http://127.0.0.1:\(port)/v1") }
                if !key.isEmpty {
                    divider
                    valueRow("密钥", String(repeating: "•", count: 12)) {
                        copyButton(key)
                        Button("重新生成") { rotate() }.buttonStyle(LKSecondaryButton()).disabled(busy)
                    }
                }
                if !models.isEmpty {
                    divider
                    valueRow("模型", models.joined(separator: "\n")) { EmptyView() }
                }
            }
        }
        if let error { Text(error).font(.system(size: 12)).foregroundStyle(Color.lkDanger).padding(.top, 6) }
        Color.clear.frame(height: 0).onAppear { if !FirstRunModel.previewing { reload() } }   // 截图时不去问螺壳进程
    }

    private var divider: some View { Divider().overlay(Color.lkBorder) }

    private func valueRow<C: View>(_ label: String, _ value: String, @ViewBuilder trailing: () -> C) -> some View {
        HStack(alignment: .center, spacing: 12) {
            Text(label).font(.system(size: 14)).frame(width: 44, alignment: .leading)
            Text(value).font(.system(size: 13, design: .monospaced)).foregroundStyle(Color.lkMuted)
                .lineLimit(nil).fixedSize(horizontal: false, vertical: true)
            Spacer(minLength: 8)
            trailing()
        }
        .padding(.horizontal, 16).padding(.vertical, 10)
    }

    private func copyButton(_ text: String) -> some View {
        Button(copied == text ? "已复制" : "复制") {
            NSPasteboard.general.clearContents(); NSPasteboard.general.setString(text, forType: .string)
            copied = text
            DispatchQueue.main.asyncAfter(deadline: .now() + 1.5) { if copied == text { copied = nil } }
        }
        .buttonStyle(LKSecondaryButton())
    }

    private func run(_ work: @escaping () throws -> Void) {
        busy = true
        DispatchQueue.global().async {
            var err: String?
            do { try work() } catch { err = (error as? HubError)?.message ?? error.localizedDescription }
            let fresh = try? client.request("GET", "/endpoint").1
            DispatchQueue.main.async {
                if let fresh { info = fresh }
                error = err
                busy = false
            }
        }
    }

    private func reload() { run {} }
    private func set(enabled: Bool) { run { _ = try client.request("PUT", "/settings", body: ["endpoint_enabled": enabled]) } }
    private func rotate() { run { _ = try client.request("POST", "/endpoint/key") } }
}
