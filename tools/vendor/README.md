# 内置的第三方 Python 包（只给出版工具用，不进主控 .app）

| 包 | 版本 | 许可 | 来源 | 用途 |
|---|---|---|---|---|
| dmgbuild | 1.6.1 | MIT | PyPI | 主控 DMG 的「拖进应用程序」窗口（tools/dmg/layout.py） |
| ds_store | 1.3.1 | MIT | PyPI | dmgbuild 写 Finder 的 .DS_Store |
| mac_alias | 2.2.2 | MIT | PyPI | dmgbuild 写背景图的书签 |

原样从 PyPI 的源码包拷来（`src/<包名>`），各目录里的 LICENSE 是原文件；没有改动。只用标准库和 macOS 自带的 hdiutil、SetFile、tiffutil。
