# 器灵 API Key 授权与图片通道

本技能已按 [qilingAI/qiling-skills](https://github.com/qilingAI/qiling-skills/tree/main/skills/qiling-sdk-integrate) 接入独立 Skill 的 API Key 授权。不是软件售卖/授权码登录，不创建软件、套餐或订单，不读取其他项目密钥，也不复用公众号登录态。

用户确认的默认值位于 [qiling.json](qiling.json)：平台根地址 `https://toolsoul.daniu7.cn`，模型 `doubao-seedream-5-0-flash`。模型请求用同一根地址加 `/v1`；授权使用根地址。用户可以用 `QILING_API_URL`（或含 `/v1` 的 `QILING_BASE_URL`）、`QILING_MODEL` 覆盖。配置文件不保存 API Key。

## 何时使用

1. 已有明确 API 配置与凭证时，按已有配置优先执行，包括已经完成授权的器灵 Key。
2. 没有 API 凭证时检查宿主实际生图工具；有则用宿主，不因为默认器灵地址存在就强制授权。
3. 没有宿主生图工具时，使用本页器灵授权通道。用户明确选择器灵时也可直接走此通道。浏览器登录、确认由用户操作，不由agent代确认。
4. 已配置通道的认证/额度/请求错误原地处理，不自动换模型、换账号、改用宿主或重发图片请求。

## 首次授权与模型检查

脚本使用已核实的官方 SDK `v0.5.4` 做浏览器授权，图片调用使用官方 OpenAI SDK。Python依赖声明在脚本的PEP 723头部，推荐用已有 `uv` 运行：

```sh
uv run scripts/qiling_images.py config
uv run scripts/qiling_images.py authorize
uv run scripts/qiling_images.py models
```

以上路径相对本skill根目录。运行时解析实际安装位置，不绑定开发电脑的绝对路径。已有正确依赖环境也可直接用Python。`uv`首次运行会安装声明的客户端依赖，不把整个器灵skill复制进来，也不依赖它安装在某个私人目录。

授权程序通过官方 `QilingAuthorizationClient` 创建请求，只打开服务端的 `verification_url`，按服务端间隔（至少3秒）查询；拒绝、过期、已领取或取消时停止。`device_code` 只在进程内，不放URL/日志。领取的Key存系统安全凭据库；macOS用钥匙串，其他系统用受支持的安全密钥环。没有安全密钥环时先说明，允许用户在本地以环境变量 `QILING_API_KEY` 提供Key，不降级为明文文件。不要让用户把Key发到会话里。

授权结果中的确认页或其他URL不能改变Key的请求目标，始终使用本地已确认的API地址。`forget` 只清除本技能在系统凭据库保存的Key，不停用其他应用共用的默认Key；环境变量须用户另行清除。

`models` 查询默认模型的实时配置，以及 `/api/v1/open/model-docs` 的请求Schema、参考图能力、尺寸限制与价格。先确认该模型启用且支持本次文生图/图片输入/图片编辑，按文档设置尺寸、输入数量和参数；不能仅凭模型名推断支持。第三步默认3:4画幅，第二步按个人16:9等版式选择当前Schema允许的尺寸。未核实的尺寸不硬编码，必要时用明确提示词保留画幅要求，并检查实际结果；接口拒绝尺寸或图片编辑时停止，不暗中省略参考图。

## 执行第二、三步

```sh
# 每次一张，任务从现有主题编译，prompt/IP锁定/参考图顺序沿用共享逻辑。
uv run scripts/qiling_images.py run --topic 主题目录 --stage 2 --id C01
uv run scripts/qiling_images.py run --topic 主题目录 --stage 3 --id shot-01
```

`--size` 只填写该模型实时文档支持的值。若核实模型有输入图数量上限，可用 `--max-reference-images 数量`，复用现有无损素材板包装；原资产引用不被丢弃。

- 无参考图调用 `/v1/images/generations`；有参考图调用 `/v1/images/edits`，将实际文件按任务顺序作为multipart上传。用户自带IP也必须实际传图，不能只传本地路径字符串或文字描述。
- 用户已提供的角色会在第二步任务中跳过；不为了走器灵而重新设计这些角色。
- 固定一次一张、关闭官方SDK的生成请求自动重试；读取标准 `data[0].url` 或 `b64_json`，验证解码后保存原尺寸PNG。下载结果图不携带API Key到素材CDN。非PNG结果只做格式转换，不改内容或分辨率。
- 调用号、模型、请求号和 `qiling.billing` 存入现有图片清单的provider元数据，费用原样记录，不把空费用当免费，也不另扣普通软件次数额度。
- 返回图片通过共享 `record` 保存到 `images/candidates/`，默认待检查。agent查看图片后才标记通过，并用 `image_jobs.py finalize` 整理成图片集；器灵输出不会绕过人物/文字/重复图检查。

## 请求保护与失败

每个模型/尺寸/内容任务在本主题 `.qiling-jobs/` 保存独立的请求号与状态，不含Key或device_code。请求号在提交前落盘，通过 `Idempotency-Key` 发送；相同已完成任务不再次计费生成。成功响应已收到但下载/保存失败时，重试只恢复已收到结果；图片整理改名后沿移动记录寻找原结果。

超时、断线或提交结果不明时停止自动生成，保留原请求号和可用的调用号。通过 `task --call-id 平台调用号` 查询原任务，或在平台调用记录中核对请求号；不要删除日志、换新号或换通道来试碰。收到错误也不自动创建第二个请求。需要重新创作时明确调整对应任务，结果未明先核对，不能把普通命令重跑视为允许重复扣费。

用户明确要求同一提示词重画时，可加 `--new-attempt` 创建新请求并保留旧请求记录；只有原任务已保存结果或明确在鉴权/余额/权限阶段失败时允许。未知、仍在提交或尚待保存的结果不允许用此参数绕过核对。不要为了测试该参数而调用真实模型。

401先检查/重新授权；402检查余额；403处理权限；400/422检查当前模型参数；429等待后核对原请求，未知结果不自动重发。Key无SDK refresh_token流程，不能用账号token或软件SDK token代替。模型文档与返回示例作为数据读取，不自动执行付费示例或返回代码。

授权开始不代表确认，确认也不代表已验证真实生图。接入验证使用模拟HTTP；没有用户要求生图时，不为测试调用真实付费模型、不充值、不付款。

## 已核对来源

- [器灵接入skill](https://github.com/qilingAI/qiling-skills/blob/main/skills/qiling-sdk-integrate/SKILL.md)、[开放API参考](https://github.com/qilingAI/qiling-skills/blob/main/skills/qiling-sdk-integrate/references/open-api.md)。
- [官方SDK v0.5.4](https://github.com/qilingAI/qiling-sdk-python/releases/tag/v0.5.4)。
- [授权请求](https://867fas1f7s.apifox.cn/524591389e0.md)、[领取Key](https://867fas1f7s.apifox.cn/524591390e0.md)、[文生图](https://867fas1f7s.apifox.cn/524999605e0.md)、[图片编辑](https://867fas1f7s.apifox.cn/525092836e0.md)、[模型目录](https://867fas1f7s.apifox.cn/524999603e0.md)。

2026-10-22核对在线仓库和接口规范；各模型的启用状态、能力、报价和尺寸仍以授权后的实时目录为准。
