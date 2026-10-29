# 迅雷云盘转本地下载链路研究

> 历史验证 / 调研记录；机器编号及统计为当时测试数据。最终功能与已知限制以 [README](../README.md) 和 [云盘集成说明](cloud-integration.md) 为准。

> 2026-10-24 更新：已按用户提供的新版 CLI 及远端配套 PowerShell 模块集成网页功能，见 [云盘集成说明](cloud-integration.md)。采用“实际开始下载后移至已下载目录”，不直接删除云盘资源。以下为先前研究记录，其中“尚未实现”仅代表研究当日状态。

日期：2026-10-23。结论：目录创建、链接缓存、本地取回、云盘删除均有官方前端调用证据，具备继续验证和实现的基础。但尚未做端到端实测，尤其不能据此保证“本地一启动就删除云盘资源”后仍能完整下载和断点续传。本轮只上线任务查看；没有创建云盘目录、提交缓存、创建本地下载或删除云盘文件。

## 证据来源与边界

读取了四台当前 `25.1.12.1624` 迅雷的任务接口，以及 `https://pan.xunlei.com/` 实际加载的公开 JavaScript。桌面端 `out.asar` 与 `thunder-pan-plugin.asar` 使用加密封装，未成功提取可读 JavaScript；未将原始安装包、第三方脚本或任何登录凭据加入项目。

官方脚本的基地址为 `https://static-pan.xunlei.com/_nuxt/dist/client/`。以下名称和代码定位足以复核，本地下载副本仅用于临时分析：

| 脚本 | 代码定位 | 说明 |
|---|---|---|
| `utils-initial.894715d73b431775f92f.js` | 模块 10、13、2、241 | 文件 API、类型常量、请求包装、本地取回 HTTP 桥接 |
| `utils-async.4d0fb9672d43933f31db.js` | `uploadUrlV2`、`Se` | URL 缓存参数及 createFile 调用 |
| `app.b19cd05142b1bf225a17.js` | `addNewFolder`、`batchPollGetTaskInfo` | 创建目录、异步任务轮询 |
| `1.0962195b5964af12847f.js` | 模块 636，`callClientRetrieve` | 本地取回的 native 优先及 HTTP 回退逻辑 |
| `102.da28105a377d335294fe.js` | `download`、`callClientRetrieve` 调用处 | 云盘文件列表到本地取回参数的组装 |

核心脚本 SHA-256：

```
utils-initial: 942e3bfb69e7a8be2cac78f6e7358d92b1987c851dcdb47efbe08cf1d1094a10
utils-async:   65db9aa354eca748e42e466a5fb860f80def8a63fa9878a85b5bcb42a0e9eed8
1:             ab55e15dbc7e8400cdbb565340095007fbf413fa5c2b06b638fddf47a89a085a
102:           36dab1f159b267c7f73a40ea3271eb051ea0f5e19b027edab13aacfe5ff93230
app:           b6805aab458a0139811f2a05407fc3d45a47d86352c547fee2ed5f2b7ea8533d
```

## 1. 新建云盘目录

官网 `drive/addNewFolder` 调用 `createFile`。请求包装确认云盘 API 基地址为 `https://api-pan.xunlei.com/drive/v1/`：

```http
POST /drive/v1/files
```

常规云盘的主要参数：

```json
{"parent_id":"父目录 ID，根目录使用空字符串","name":"目录名","kind":"drive#folder","space":""}
```

官网将返回值 `response.file` 放入文件列表，后续应使用服务端返回的 `file.id`。请求带 `withCaptcha: true`；这不是匿名接口，需沿用有效迅雷云盘登录和校验流程。

## 2. 提交磁力链接缓存

官网 `uploadUrlV2` 的实际参数结构为：

```json
{
  "upload_type":"UPLOAD_TYPE_URL",
  "kind":"drive#file",
  "parent_id":"第一步创建的目录 ID",
  "name":"任务名称或链接",
  "hash":"",
  "size":0,
  "url":{"url":"magnet:?xt=urn:btih:...","files":[]},
  "unionId":"",
  "params":{"require_links":"false"}
}
```

此参数经 `Se` → `createFile` 提交到同一个 `POST /drive/v1/files`。`Se` 将结果封装为 `[error, response]`。这证明提交路径和参数，不能证明任意磁力链接都能缓存成功或立即完成。

必须识别响应中的文件/异步任务 ID，并等待资源达到可取回状态；失败、无空间、云添加次数不足应独立处理。官网存在 `GET /drive/v1/tasks/{task_id}` 及 `PHASE_TYPE_COMPLETE` 轮询逻辑，已明确追踪到批量删除使用该逻辑；URL 缓存具体响应及完成条件仍需实测确认，不能直接套用删除任务的响应结构。磁力任务生成多个文件或目录时，还需核对实际子文件、选中项与完成状态。

## 3. 从云盘创建本地下载

官网 `download` 将所选文件整理为：

```json
{
  "files":[{"id":"云盘文件 ID","kind":"drive#file","name":"名称","size":"服务端返回大小","icon_link":"服务端返回图标"}],
  "userId":"当前迅雷云盘账号 ID",
  "fromShare":false,
  "selectNav":"pan-plugin-view",
  "from":"web_yunpan",
  "safeToken":"",
  "isInSafe":false
}
```

`callClientRetrieve` 首先检测 native 方法 `CreatePanExternalFetchBackTask`，支持时调用 `window.native.CallNativeFunction` 并传入上述对象的 JSON 字符串。否则将该对象交给模块 241 的 HTTP 包装：

```http
POST http://127.0.0.1:28317/yun_fetch_back/
Content-Type: application/x-www-form-urlencoded
```

该包装把对象作为 axios 的 `data` 传入，超时 3 秒，调用者检查 `response.data.ret === 0`。精确线上表单编码、完整返回结构仍须验证。native 分支返回成功不能当作磁盘已开始接收数据的证明。

**只读实机核对：101、102、103、104 均监听 `127.0.0.1:28317`。** 这与平台对外访问地址无冲突：用户继续访问平台服务器 IP + 8765；将来由平台经现有 SSH 通道，在选定 Windows 机器本机调用桥接即可。

尚未验证：此入口是否弹出保存目录/文件选择窗口、是否支持后台无交互创建、是否接受指定本地路径、返回值能否关联本地 taskId、当前客户端账号与云盘 userId 是否一致。因此目前不能承诺已能自动完成远程取回。代码中尚无经过验证的 `TaskManager...` 云盘建单方法，不应凭名称猜测调用。

官方还存在 `GET files/{id}`（默认 `usage=DISPLAY`）以及预览使用 `usage=CONSUME` 获取 `web_content_link` 的路径；这只是预览链接证据，不能作为已验证的云盘下载替代方案。

## 4. 启动后删除云盘资源

官方文件 API 已明确区分：

| 意图 | 接口 |
|---|---|
| 移入云盘回收站 | `PATCH /drive/v1/files/{id}/trash` |
| 还原 | `PATCH /drive/v1/files/{id}/untrash` |
| 彻底删除单文件 | `DELETE /drive/v1/files/{id}?space=` |
| 批量彻底删除 | `POST /drive/v1/files:batchDelete`，主体 `{ids:[], space:""}` |

批量删除通过返回的任务 ID 轮询完成。云盘回收站与当前看板展示的 Windows 迅雷**下载任务回收站**是两个独立概念。

目前没有找到足够证据证明文件刚进入本地下载状态便可删除其云端资源。需要分别验证：

1. 本地 taskId 创建成功、确属云盘取回，并实际有下载字节增长，而非仅显示“创建成功”。
2. 删除后当前连接能否完成；连接断开、暂停恢复、客户端重启后是否还能获取有效下载来源。
3. 移入回收站和彻底删除是否有不同效果；下载地址过期或需要刷新鉴权时的表现。
4. 多文件任务是否所有子文件均已取得可用来源，目录级删除是否影响未启动的子文件。

因此，“开始后删除”应在后续授权的小样本验证中单独作为待证假设。完整性和可恢复性未通过前，建议保留临时云盘资源直到本地完成。预期加速收益取决于云端资源命中、账号权益、网络和文件可用性，本轮未测速度。

## 平台化实施建议（尚未实现）

平台账号与迅雷云盘账号是两套身份。未来需为每个用户、每台目标机器明确绑定授权及迅雷 userId，不能复用管理员云盘会话给新用户。现有回收站数据已按平台用户隔离，并展示原始 URL，供后续选择磁力任务。

推荐作业状态：待授权 → 创建专用临时目录 → 提交缓存 → 等待缓存完成 → 本机取回 → 校验实际本地任务及字节增长 → 按经验证的策略清理云盘 → 完成/需人工处理。应保存文件 ID、云任务 ID、本地任务 ID 的映射；只清理本次作业创建的云盘资源，失败与超时不盲目重提或删除。原下载任务及回收站记录的处理应由独立、明确的产品规则决定，不能与新作业暗中联动。

下一步如用户决定实施，可先实现并验证前三步，之后用一个小型资源验证删除时机、断点续传和重启恢复；本轮未向任何云盘或本地建单接口发送操作请求。
