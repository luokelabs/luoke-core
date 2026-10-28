# 参与螺壳

螺壳现在处于封闭测试（beta1），欢迎提 issue：用起来哪里不对、哪里看不懂、哪台 Mac 上跑不动。报问题时写上螺壳的版本、Mac 的芯片和内存、macOS 版本；「设置 › 关于」里能导出诊断信息（只有机型、版本、状态和错误码，不含你的文字），附上能帮我们看清楚。

- **改动先开 issue**：想改代码或文档，先开 issue 说明要解决什么、打算怎么改，商量好再提 PR。设计上的规则以 [docs/DESIGN.md](docs/DESIGN.md) 为准，v1 的范围以 [docs/V1.md](docs/V1.md) 为准。
- **配方的改动**（新的应用或任务、换模型、开一档）要附评测证据，并由另一位成员 approve。
- **提交**：提交信息用英文，说清改了什么、为什么；文档用简体中文，代码标识、路径、API 名保留英文。
- **README 有两份**：`README.md`（中文）和 `README.en.md`（英文全文），改一份就在同一次提交里改另一份。
- **测试**：提 PR 前跑一遍 `python3 -m unittest discover -s tests`，动了 `app/` 的再跑一遍 `sh app/build_app.sh`。
- **安全问题**不要开公开 issue，见 [SECURITY.md](SECURITY.md)。

请友善、就事论事地交流。

---

Luoke is in closed beta. Issues are welcome; please open an issue before sending a pull request. The README comes in two copies, `README.md` (Chinese) and `README.en.md` (English); change both in the same commit. Commit messages in English; run `python3 -m unittest discover -s tests` before submitting. Be kind.
