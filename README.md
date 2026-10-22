# Codex API 生图 Skill

在 Codex 中生成、修改图片，复用本机已有的 API 中转配置和密钥。默认使用 **`gpt-6.1-sol` + `gpt-image-2`**，无需 OpenAI SDK，支持 Python 3.11+。

这是通过 API 调用图片工具的 skill，不会给客户端凭空添加内置 `image_gen` 工具。中转需要支持 Responses API 的 `image_generation` 工具，上游账号也需要相应权限。调用会消耗对应服务的额度。

## 安装

把下面这句话连同项目地址发给 Codex，即可让它按照本页说明安装：

> 帮我安装这个 Codex 生图 skill：https://github.com/hwx61514/codex-api-imagegen ，请阅读 README，将 skills/codex-api-imagegen 安装到我的用户级 skills 目录，检查 Python 3.11+ 和本机 API 配置。安装时不要提交付费生图请求，也不要输出我的 key 或私人端点。

公开仓库下载无需 GitHub 账号或作者授权。生图使用安装者自己的 API 配置和额度；需要接口支持默认模型及图片工具，安装成功不代表上游一定有生图权限。

### 给 AI 的安装步骤

1. 本仓库的 skill 位于 `skills/codex-api-imagegen`，不是仓库根目录。先阅读其中的 `SKILL.md`。有内置 `skill-installer` 时，使用其安装脚本并传入 `--repo hwx61514/codex-api-imagegen --path skills/codex-api-imagegen`。
2. 没有安装器时，下载或克隆公开仓库，将该子目录复制到本机 Codex 识别的用户级 skills 目录；本项目在 `$CODEX_HOME/skills`（未设置时 `~/.codex/skills`）验证过。不同客户端以其内置安装器和实际发现路径为准。已有同名目录先检查和备份，避免覆盖用户修改或形成重复嵌套。
3. 找到 Python 3.11+（可用客户端自带运行时）。执行已安装脚本的 `--help`，再执行 `inspect` 检查配置来源。只向用户报告检查状态，勿转发其中的私人 endpoint。缺少运行时或接口配置时说明缺项，不索取聊天中的 key，不改变聊天模型或接口配置。
4. 确认安装目录含 `SKILL.md`、`agents/openai.yaml`、`scripts/imagegen.py`。若客户端未发现 skill，新开聊天或重启客户端；明确调用 `$codex-api-imagegen`。仅安装时不进行付费生图测试。

### 手动安装

将本仓库的 `skills/codex-api-imagegen` 文件夹复制到本机 Codex 的 `skills` 目录。默认位置为用户目录下的 `.codex/skills`；设置过 `CODEX_HOME` 则使用该目录下的 `skills`。安装后新开一个 Codex 聊天，让它发现新 skill。

macOS / Linux，在本仓库目录运行：

```sh
mkdir -p "${CODEX_HOME:-$HOME/.codex}/skills"
cp -R skills/codex-api-imagegen "${CODEX_HOME:-$HOME/.codex}/skills/"
```

Windows PowerShell，在本仓库目录运行：

```powershell
$skillHome = if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $env:USERPROFILE '.codex' }
New-Item -ItemType Directory -Force (Join-Path $skillHome 'skills') | Out-Null
Copy-Item -Recurse '.\skills\codex-api-imagegen' (Join-Path $skillHome 'skills')
```

以上命令用于首次安装；已有同名 skill 时先检查或备份旧目录，不要把更新复制进重复的嵌套文件夹。下载到 Windows 的版本与 Mac 一样，不包含任何个人 key 或中转地址，运行时会读取 Windows 本机的配置。

## 在 Codex 里使用

直接请求生图，或明确调用：

> 使用 $codex-api-imagegen，生成一张白色背景上的红苹果产品照片。

> 使用 $codex-api-imagegen，给这张门的照片加上“星河科技”白底黑字贴纸门牌，保留原来的门和环境。

图片编辑须让 Codex 能访问原图文件。脚本支持一至三张独立参考图。默认不会改变你的聊天模型或服务器配置。

## 直接运行

先确认 Python 版本。下面使用 `python3`；Windows 可替换为 `py -3.11` 或已安装的 Python 3.11+ 命令。

```sh
python3 --version
python3 skills/codex-api-imagegen/scripts/imagegen.py inspect
python3 skills/codex-api-imagegen/scripts/imagegen.py generate --prompt "白底红苹果产品照片" --out output/imagegen/apple.png
python3 skills/codex-api-imagegen/scripts/imagegen.py edit --image photo.png --prompt "只把背景改为浅灰，主体不变" --out output/imagegen/edited.png
```

可选参数：`--prompt-file` 读取 UTF-8 提示词、`--image` 可重复三次、`--size`、`--quality low|medium|high|auto`、`--output-format png|jpeg|webp`、`--timeout`。`--model` 与 `--image-model` 仅在明确需要切换时使用。`--codex-home` / `--profile` 可选择其它本地 Codex 配置；它们必须来自用户可信配置。

### 国内与海外 DNS

默认 `--dns auto`：先沿用系统 DNS 和代理。仅当直接连接公网 API 域名时系统解析失败，才并行查询阿里 DNS（国内）和 Cloudflare（海外）的加密 DNS，采用先返回有效结果的一路。系统正常时不增加查询，不按用户所在地固定 DNS。备用解析约有 4 秒预算，之后连接备用 IP 也有限时；不会一直等待不可用的来源。

| 参数 | 系统解析失败后的行为 |
| --- | --- |
| `--dns auto` | 国内、海外来源并行兜底，默认 |
| `--dns cn` | 只用阿里 DNS 兜底 |
| `--dns global` | 只用 Cloudflare 兜底 |
| `--dns system` | 仅使用系统解析，关闭第三方 DNS 查询 |

备用 DNS 仅接收待解析的 API 域名，不发送 API key、提示词或图片。本机地址、IP 字面量及常见内部/保留后缀不外查，只接受公网 IP 结果；使用内部自定义域名或不希望域名交给第三方时选 `--dns system`。HTTPS 继续使用原 API 域名校验证书、发送 SNI 和 Host；不修改系统 DNS、API 地址或证书设置。已有代理及 `NO_PROXY` 沿用，代理主机解析失败时不会绕过代理直连。

这是连接前的解析兜底，不会重发已提交的生图请求。网络权限限制、代理故障、证书错误、服务端故障及区域访问限制仍需处理，不能保证任意地区永不报错。国内 DNS 不等于国内可访问任意海外 API。

## 读取哪些配置

1. 从 `CODEX_HOME/config.toml` 或 `~/.codex/config.toml` 找到活动 profile 和 `model_provider`。
2. 使用对应 provider 的 `base_url` 和认证信息，支持 `env_key`、`experimental_bearer_token`。官方 provider 或 `requires_openai_auth=true` 的 provider 还支持 `OPENAI_API_KEY` 环境变量及 `auth.json` 中的 API Key。
3. 支持普通自定义 `http_headers` / `env_http_headers`；保留的认证及 HTTP 结构头必须使用正式配置字段，不能用重复 header 覆盖。
4. 生图请求只发到该 provider。域名根地址补 `/v1/responses`，已有 API 前缀后追加 `/responses`，禁止重定向。HTTPS 为默认要求，仅本机回环测试允许 HTTP；仅在系统解析失败后才可能发出上述备用 DNS 查询。

不读取或复用 ChatGPT 登录的 OAuth token，不执行配置里的外部取 key 命令，不把 key 写进源码、参数或输出。`inspect` 只显示来源，不显示值。`inspect` 结果中的自定义 endpoint 可能是私人地址，请勿把个人运行输出上传到公开仓库。

## 失败与实际尺寸

在出站网络已知受限的 Codex 环境中，AI 应在第一次生成时申请所需网络权限，直接复用可用的 Python 3.11+，避免先失败再重复诊断。生成期间等待同一个进程，不重跑生成请求。

脚本会区分连接前的 DNS 失败、网络权限阻止、连接被拒和 TLS 证书错误。明确提示未发送生图请求时，修复对应原因后可再提交；DNS 失败既可能是沙箱限制，也可能是真实 DNS/地址配置问题。证书错误应修复证书或信任配置，不能关闭校验。普通连接错误、超时及响应中断仍视为结果不确定。

脚本只提交一次请求，不自动重试。没有真实图片、上游失败或流中断都会返回非零状态。若流中断前已返回图片，图片会先保存并输出路径；需先核对服务端记录，不要直接重跑。

输出不会覆盖旧文件。响应重复出现同一图片时只保存一份；如果另一个进程在请求期间占用了文件名，结果另存为带 `recovered` 的文件。

中转可能不遵守 `size` 或 `quality`。输出包含请求尺寸与 PNG 实际尺寸，交付以实际文件为准。测试成功只证明该次路由可用，不能保证混合账号池里每个账号都支持生图。权限错误应检查账号/分组调度，不能把文字回复当成图片。

## 开发检查

```sh
python3 -m unittest discover -s tests -v
```

测试通过隔离配置和本机 HTTP 服务覆盖认证选择、实际请求、SSE / JSON、参考图顺序、无图及断流、拒绝重定向、防覆盖、脱敏和单次提交；另模拟 DNS 竞速、失败期限、公网 IP 过滤、HTTPS 原域名及代理保留，不会使用个人 key 或访问付费接口。GitHub Actions 在 Windows 和 Linux 的 Python 3.11 / 3.13 上执行相同测试。

协议参考：[官方图片生成指南](https://developers.openai.com/api/docs/guides/image-generation)。
