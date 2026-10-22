---
name: codex-api-imagegen
description: 使用 Codex 当前配置的 API 中转和密钥生成或编辑图片。适用于用户要求生图、修图、添加文字、使用参考图或缺少内置 image_gen 工具的场景；默认 gpt-6.1-sol 调用 gpt-image-2，支持 macOS 和 Windows。
---

# Codex API 生图与改图

使用本 skill 自带的 `scripts/imagegen.py`，直接复用这台电脑 Codex 的 API provider，不要求用户重新填写已有密钥。默认编排模型 `gpt-6.1-sol`，图片模型 `gpt-image-2`；不要因为对话正在使用别的模型而改变这两个默认值。用户明确指定模型时才用参数覆盖。

## 执行

1. 找到可用的 **Python 3.11+**。macOS 通常用 `python3`，Windows 通常用 `py -3.11` 或 `python`；先检查版本。Codex 提供 bundled Python 时可使用该运行时。脚本只依赖标准库，无需安装 OpenAI SDK。
2. 第一次运行先执行 `python "<本 skill 路径>/scripts/imagegen.py" inspect`。只显示 provider、endpoint、密钥来源，不显示密钥。默认读取 `CODEX_HOME`，否则读取本机用户目录下的 `.codex`；通过 `--codex-home` 或 `--profile` 选择用户明确要求的其它本地配置。
3. 用户的描述足够明确就执行，不因缺少内置 `image_gen` 停止。新图使用 `generate`；对已有图片加字、换背景或其它修改使用 `edit`，并传入原图作为 `--image`。最多三张参考图，按参数出现顺序解释各自用途。
4. 给出简短、具体的提示词。编辑时明确哪些内容保持不变；图片中文字必须逐字引用。长提示词或包含复杂引号时写入工作区的临时 UTF-8 文本，通过 `--prompt-file` 传入，避免跨平台 shell 引号问题。
5. 输出到当前工作区有意义的新文件名，默认 `output/imagegen/`。脚本不覆盖已有文件。一次请求一张，避免批量无提示复购。默认 `medium`，用户只要快速草图时可选 `low`；不保证中转遵守请求的尺寸或质量。

```sh
python "<本 skill 路径>/scripts/imagegen.py" generate --prompt "白底红苹果产品照片" --out "output/imagegen/apple.png"
python "<本 skill 路径>/scripts/imagegen.py" edit --image "door.png" --prompt "仅在门上添加贴着的白底黑字纸质门牌，文字为‘星河科技’，匹配透视和阴影，其余内容保持不变。" --out "output/imagegen/door-sign.png"
```

上述 `python` 代表第一步找到的运行时，不能假定所有系统的命令相同。

## 认证与接口

脚本读取活动 profile/provider 的 `base_url`，依次使用该 provider 的 `env_key`、`experimental_bearer_token`；官方 provider 或 `requires_openai_auth=true` 的 provider 也支持环境变量 `OPENAI_API_KEY` 和 `auth.json` 的 `OPENAI_API_KEY`。不把 ChatGPT OAuth 登录 token 当 API Key，不猜测其它服务的凭据，不修改 Codex 配置。

域名根地址自动补 `/v1/responses`；已有 `/v1` 或自定义 API 前缀则在其后加 `/responses`。脚本通过 Responses API 显式携带 `image_generation`、图片模型及 `tool_choice`，拒绝重定向。支持 SSE 和普通 JSON 返回；真正图片来自 `image_generation_call.result`，文字答复不算生图成功。

没有可用密钥时说明所缺的配置位置，不让用户把密钥粘贴到聊天。出站网络若被沙箱阻止，按当前环境规则申请必要的网络权限；不要建议关闭整个沙箱。API 会消耗所配置服务的额度。

## 检查与交付

- 成功后读取脚本输出的实际路径，用图片查看工具打开结果，检查内容、文字、透视及编辑保留项。展示真实输出图片，使用绝对路径。记录实际尺寸，不用放大插值冒充请求的分辨率。
- 只有脚本确认响应完成且保存了图片才能宣布成功。HTTP 200 或一段“已生成”的文字都不够。
- 请求超时、断流、5xx 或完成状态不明时，**不要自动重新提交**。保留已保存的图片和响应 ID（如果有），说明结果不确定，先核对中转记录，避免重复扣费。
- 明确的工具权限错误可能来自上游账号或分组调度。不能只凭一次失败断言模型不支持图片，也不能擅自换成更贵的模型、修改服务器账号或重试其它收费模型。
- 修改生成图时用新文件名保留原图。脚本参数 `--size`、`--quality`、`--output-format` 是请求值；实际成图可能不同，按检查结果如实说明。
