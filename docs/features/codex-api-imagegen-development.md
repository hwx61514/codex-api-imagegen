# Codex API 生图 skill 开发与验证

## 范围

创建可安装到 macOS / Windows Codex 的独立 skill。默认使用 `gpt-6.1-sol` 编排、`gpt-image-2` 绘图，通过用户现有 Codex API provider 调用 `/v1/responses`。支持文字生图和本地参考图编辑；不改 Codex 模型、不修改中转服务器、不保存或上传凭据。

正常入口：用户在 Codex 请求生图/改图，自动选中 skill，或明确输入 `$codex-api-imagegen`。命令行入口为 skill 内 `scripts/imagegen.py`。

影响层：skill 指令与元数据、本地 provider/认证读取、HTTP/SSE 图片结果解析、本地文件保存、安装说明、跨平台 CI、GitHub 发布及个人复用台账。

## 验收与配对验证

- [x] 默认模型与实际图片能力：CLI 默认 `gpt-6.1-sol` + `gpt-image-2`；使用现有 API 完成一次真实请求并检查图片文件，不将文字回复算成功。
- [x] 复用本机配置：读取 `CODEX_HOME` / `~/.codex`、活动 profile/provider、环境变量密钥、provider bearer token 或 API-key 登录；以隔离配置测试来源和 profile 优先级，缺失密钥时失败且不输出秘密。
- [x] 生成与参考图编辑：文字、中文提示和重复 `--image` 形成正确请求；隔离 HTTP 服务验证输入图的 Base64 和多参考顺序，再完成一次真实参考图修改。
- [x] 可靠输出：支持 SSE / JSON，识别完成的真实图片；无图片、失败事件、断流、HTTP 错误不能报成功；本地 HTTP 测试涵盖这些情况。
- [x] 凭据和费用边界：拒绝携带认证信息的重定向、拒绝非本地明文 HTTP、无自动重购；错误和 inspect 不显示密钥；测试验证每次仅提交一次、脱敏、输出文件防覆盖和响应大小限制。
- [x] 可发现与可安装：完成 SKILL.md、agents/openai.yaml、Mac/Windows 安装及使用说明；运行官方 skill validator，安装本机并核对安装副本。
- [x] 跨平台检查：Python 3.11+、纯标准库运行；本机完整测试通过，GitHub Actions 的 Windows/Linux 测试通过；未运行的平台实测明确记录。
- [x] 发布完整性：仅将本 skill 的源码和说明发布到用户 GitHub 的独立私有仓库；提交前检查无个人凭据/地址/图片；核对远端提交、CI 结果及更新个人复用台账。

## 现有能力复用

复用现有 Responses + `image_generation` 契约。已有业务项目中的 Go 客户端依赖项目配置和参考图业务约束，不适合直接安装进跨平台 Codex；内置 imagegen CLI 依赖独立环境变量与 OpenAI SDK，不自动读取活动 Codex provider。本 skill 只封装本机配置适配和同一 API 契约，使用 Python 标准库，不复制业务服务或修改内置 skill。

## 验证证据

2026-10-23：

- 本机 Python 3.12 执行 26 项测试全部通过。覆盖隔离配置、profile、环境 key、OAuth 排除、SSE/JSON、参考图顺序、断流保留图片、HTTP/重定向/畸形结果、单次提交、防覆盖、脱敏、响应限额，以及 Windows BOM/中文路径/旧管道编码。
- 新 CLI 使用默认模型真实生成白底红苹果，状态 completed，PNG 已打开检查，实际 1254×1254；请求 1024×1024，说明当前中转未严格遵守尺寸。未把实际尺寸误报为请求尺寸。
- 官方 quick_validate.py 返回 Skill is valid；UI 元数据已提供名称、说明和明确的 skill 调用提示。
- 本机 skills 目录安装三份源文件并逐份 SHA-256 核对；安装副本 inspect 成功读取当前 provider 和凭据来源，无密钥输出。当前会话不会动态重载 skill 列表，自动发现需新开聊天；Windows 客户端实机发现尚未验证。
- 源码扫描未发现当前凭据、中转端点、个人地址或测试图片。真实改图 completed，已查看浅蓝背景结果，苹果主体保留、尺寸1254×1254。GitHub 私有仓库已创建，远端初始提交00a3c0f已核对；已登记本机个人复用台账。

- 首轮 CI：Linux Python3.11/3.13通过；Windows两版均因测试直接比较JSON路径文本失败（反斜杠转义及临时目录短名展开），图片已保存且脚本按预期返回失败。修正为解析JSON并比较规范化路径；模拟服务的预期限额断连不再输出无关服务器回溯。修正后运行39823936315四组检查全部通过：Windows/Linux各Python3.11、3.13，每组26项。

- 发布核验：GitHub `hwx61514/codex-api-imagegen` 为 PRIVATE、默认main；已核对远端源码提交d50d33a。最终证据补充提交仅修改本文档，使用skip-ci避免重复运行未变化代码。跟踪文件再次扫描无当前凭据/端点及图片；安装副本仍与已上传skill源码逐文件一致。个人服务接口台账已更新能力位置、契约、边界、依赖和凭据引用。Windows客户端实机交互未执行，不将CI等同于Windows客户端全流程验收。
